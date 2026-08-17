"""Postgres connections, in two flavours for two kinds of caller.

The server is an asyncio process, so its database work goes through
`AsyncConnectionPool` and `async with connection(...)`. A coroutine that awaits
a query hands control back to the event loop for the duration of the round
trip, which is what lets one process serve other tenants while this one waits.
That is the primary path and nearly all traffic uses it.

The sync pool exists for callers that have no event loop and cannot get one:
the `capture` CLI, `migrate()`, and the retention sweep when it is run as a
script. Those are batch jobs where blocking is the correct behaviour — there is
nothing else for the thread to do.

Both pools point at the same database with the same RLS setup; they differ only
in how the caller waits. Do not reach for the sync pool from inside a coroutine:
it blocks the event loop, which is the bug this split exists to make impossible
to write by accident. `tests/server/test_event_loop_contract.py` enforces it.

**Sizing.** `CAPTURE_DB_POOL_MAX` caps *each* pool, not the process, and a
server opens both: the async one for requests, the sync one for the project
registry (`projects.py`, which the CLI shares and which caches for
CAPTURE_PROJECT_CACHE_TTL_S, so it is a rare reader rather than a hot path).
Budget `2 * CAPTURE_DB_POOL_MAX` per process against Postgres's
`max_connections`, and remember `min_size=1` means each pool holds one backend
open even while idle.
"""

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

# The async pool is created inside the running loop and belongs to it. A pool
# bound to a loop that has closed is useless, and reusing one across loops (as
# tests create and discard loops freely) is a source of "attached to a different
# loop" errors, so the loop identity is part of the cache key.
_apool: AsyncConnectionPool | None = None
_apool_loop: asyncio.AbstractEventLoop | None = None
# Guards the create-and-open sequence. `open()` awaits, so without this every
# coroutine that arrives during a cold start gets past the None check and builds
# its own pool; the losers are then dropped on the floor still holding
# connections. A threading.Lock would not help — these race on one thread, at an
# await point — and the lock itself is created per loop for the same reason the
# pool is.
_apool_lock: asyncio.Lock | None = None
_apool_lock_loop: asyncio.AbstractEventLoop | None = None


def _loop_lock(loop) -> asyncio.Lock:
    global _apool_lock, _apool_lock_loop
    if _apool_lock is None or _apool_lock_loop is not loop:
        _apool_lock = asyncio.Lock()
        _apool_lock_loop = loop
    return _apool_lock


def database_url() -> str:
    url = os.environ.get("CAPTURE_DATABASE_URL", "")
    if not url:
        raise RuntimeError("CAPTURE_DATABASE_URL is not set")
    return url


def admin_url() -> str:
    return os.environ.get("CAPTURE_DATABASE_ADMIN_URL") or database_url()


def configured() -> bool:
    return bool(os.environ.get("CAPTURE_DATABASE_URL"))


def pool_max() -> int:
    return int(os.environ.get("CAPTURE_DB_POOL_MAX", "10"))


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    database_url(),
                    min_size=1,
                    max_size=pool_max(),
                    # Fail fast when the database is down. The default 30s wait
                    # would queue ingests behind an outage until they time out
                    # client-side; a prompt 503 lets the recorder retry instead.
                    timeout=float(os.environ.get("CAPTURE_DB_WAIT_S", "5")),
                    open=True,
                )
    return _pool


def pool_wait_s() -> float:
    return float(os.environ.get("CAPTURE_DB_WAIT_S", "5"))


async def apool() -> AsyncConnectionPool:
    """The async pool, opened on first use and bound to the running loop."""
    global _apool, _apool_loop
    loop = asyncio.get_running_loop()
    if _apool is not None and _apool_loop is loop:
        return _apool
    async with _loop_lock(loop):
        # Re-checked inside the lock: several coroutines can queue here on a
        # cold start, and only the first should build anything.
        if _apool is not None and _apool_loop is loop:
            return _apool
        created = AsyncConnectionPool(
            database_url(),
            min_size=1,
            max_size=pool_max(),
            # Same reasoning as the sync pool: fail fast rather than queue
            # every request behind an outage.
            timeout=pool_wait_s(),
            open=False,
        )
        await created.open()
        # Published only once it is open and usable. A pool from a previous
        # loop cannot be closed from this one, so the reference is simply
        # replaced and its connections lapse with the loop that owned them.
        _apool, _apool_loop = created, loop
        return _apool


def close_pool(timeout: float = 3.0) -> None:
    """Bounded on purpose: with the database unreachable the pool's workers are
    mid-retry, and an unbounded close makes the process impossible to stop
    cleanly — which is how a deploy turns into a SIGKILL."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close(timeout=timeout)
            except Exception as exc:
                print(f"db: pool did not close cleanly: {exc}")
            _pool = None


def forget_apool() -> None:
    """Drop the async pool reference without awaiting a close.

    For teardown paths that have no loop to await on — chiefly tests, where each
    case runs on its own `asyncio.run` loop and a pool held over from a closed
    one is unusable. The connections are released when the loop that owned them
    is collected; there is nothing to wait for here.
    """
    global _apool, _apool_loop
    _apool = None
    _apool_loop = None


async def close_apool(timeout: float = 3.0) -> None:
    """Close the async pool. Bounded for the same reason as `close_pool`."""
    global _apool, _apool_loop
    if _apool is not None:
        try:
            await asyncio.wait_for(_apool.close(), timeout=timeout)
        except Exception as exc:
            # TimeoutError included: a pool that will not drain must not stop
            # the process from exiting.
            print(f"db: async pool did not close cleanly: {exc}")
        _apool = None
        _apool_loop = None


def _scope_sql(project_id, admin: bool) -> tuple:
    """The RLS statement that scopes a connection, as (sql, params).

    Shared by both connection helpers so the sync and async paths cannot drift
    on the one thing that keeps tenants apart.
    """
    if admin:
        return "SELECT set_config('app.admin', 'on', true)", ()
    return "SELECT set_config('app.project_id', %s, true)", (str(project_id),)


@asynccontextmanager
async def connection(project_id=None, admin: bool = False):
    """A transaction scoped to one project, awaited rather than blocking.

    Every RLS-protected table is invisible unless project_id matches or admin is
    set, so a query that forgets its WHERE clause returns nothing instead of
    another tenant's rows.

    This is the path the server uses. Awaiting the round trip hands the event
    loop back, so other requests run while this one waits on Postgres.
    """
    if not admin and project_id is None:
        raise ValueError("connection() needs a project_id, or admin=True")
    sql, params = _scope_sql(project_id, admin)
    pool_ = await apool()
    async with pool_.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            # `set_config` is a SELECT, so it returns a row. Drain it before
            # handing the connection on: psycopg refuses a second statement
            # while a result set is still pending ("another command is already
            # in progress"), which turned every scoped query into a 500.
            await cur.fetchall()
        yield conn


@contextmanager
def sync_connection(project_id=None, admin: bool = False):
    """The blocking equivalent, for callers with no event loop.

    The CLI, `migrate()`, and script-mode retention run here: batch work where
    there is nothing else for the thread to be doing. Never call this from a
    coroutine — it blocks the loop, and the async `connection()` above exists
    precisely so you do not have to.
    """
    if not admin and project_id is None:
        raise ValueError("sync_connection() needs a project_id, or admin=True")
    sql, params = _scope_sql(project_id, admin)
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        yield conn


async def fetchone(conn, sql: str, params=()):
    """Run a query and return its first row, or None.

    The sync driver allowed `conn.execute(...).fetchone()`; on an async
    connection both halves are coroutines, and `await conn.execute(...).fetchone()`
    binds the await to the whole expression — reading `.fetchone` off a
    coroutine object rather than a cursor. That fails at runtime, not import,
    so these two helpers exist to keep the mistake unwritable at call sites.
    """
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def fetchall(conn, sql: str, params=()) -> list:
    """Run a query and return every row. See `fetchone`."""
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


def applied_versions(conn) -> set:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def migration_files() -> list:
    """SQL files to apply, in filename order.

    Names start with a digit so a macOS AppleDouble fork (._0001_init.sql)
    is not treated as SQL. Those match *.sql, sort first, and are binary."""
    return sorted(MIGRATIONS_DIR.glob("[0-9]*.sql"))


def migrate(url: str | None = None) -> list:
    """Apply pending migrations in filename order. Each runs in its own
    transaction so a failure leaves the ones before it committed."""
    target = url or admin_url()
    applied = []
    with psycopg.connect(target) as conn:
        conn.autocommit = True
        done = applied_versions(conn)
    for path in migration_files():
        version = path.stem
        if version in done:
            continue
        with psycopg.connect(target) as conn:
            with conn.transaction():
                role = os.environ.get("CAPTURE_DB_APP_ROLE")
                if role:
                    conn.execute("SELECT set_config('capture.app_role', %s, true)", (role,))
                conn.execute(path.read_text())
                apply_python_migration(conn, version)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


def apply_python_migration(conn, version: str) -> None:
    """Work SQL cannot share with the Python that already owns the mapping."""
    if version == "0009_recompute_page":
        recompute_session_pages(conn)
    elif version == "0010_cohort_groups":
        rewrite_legacy_cohort_fields(conn)


def recompute_session_pages(conn) -> None:
    """Set sessions.page from sessions.url with the same parser ingest uses."""
    from capture.classify import page_path

    rows = conn.execute("SELECT project_id, session_id, url FROM sessions").fetchall()
    if not rows:
        return
    # Connection.execute exists; executemany is on the cursor (psycopg 3).
    conn.cursor().executemany(
        "UPDATE sessions SET page = %s WHERE project_id = %s AND session_id = %s",
        [(page_path(url), project_id, session_id) for project_id, session_id, url in rows],
    )


def _int_field(fields: dict, key: str) -> int:
    try:
        return int(fields.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def groups_fields_from_legacy(fields: dict) -> dict:
    """Rewrite a stored cohort `fields` blob from purchased/all/any to A/B.

    The purchase split owns `outcome`, the same way the old sampler did.
    `selection` is left as the historical sentence. New writes never set
    the three keys; this is only for rows sampled before groups existed.
    """
    out = dict(fields)
    purchased_n = _int_field(out, "purchased_n")
    all_n = _int_field(out, "all_n")
    any_n = _int_field(out, "any_n")
    a_n = _int_field(out, "a_n")
    b_n = _int_field(out, "b_n")

    if a_n == 0 and b_n == 0:
        if any_n > 0:
            out["a_n"] = any_n
        else:
            base = dict(out.get("slice") or {}) if isinstance(out.get("slice"), dict) else {}
            base.pop("outcome", None)
            if purchased_n > 0 and all_n > 0:
                out["a_n"] = purchased_n
                out["b_n"] = all_n
                out["slice"] = {**base, "outcome": ["purchased"]}
                out["slice_b"] = {**base, "outcome": ["checkout", "rest"]}
            elif purchased_n > 0:
                out["a_n"] = purchased_n
                out["slice"] = {**base, "outcome": ["purchased"]}
                out.pop("slice_b", None)
            elif all_n > 0:
                out["a_n"] = all_n
                out["slice"] = {**base, "outcome": ["checkout", "rest"]}
                out.pop("slice_b", None)

    for key in ("purchased_n", "all_n", "any_n"):
        out.pop(key, None)
    return out


_FILTER_TAG = {"purchased": "a", "all": "b"}


def rewrite_legacy_cohort_fields(conn) -> None:
    """One-shot: stored sampling rules and session tags become A/B groups."""
    rows = conn.execute("SELECT project_id, cohort_id, fields FROM cohorts").fetchall()
    updates = []
    for project_id, cohort_id, fields in rows:
        current = dict(fields or {})
        rewritten = groups_fields_from_legacy(current)
        if rewritten != current:
            updates.append((json.dumps(rewritten), project_id, cohort_id))
    if updates:
        conn.cursor().executemany(
            "UPDATE cohorts SET fields = %s::jsonb" " WHERE project_id = %s AND cohort_id = %s",
            updates,
        )

    session_rows = conn.execute(
        "SELECT project_id, cohort_id, session_id, fields FROM cohort_sessions"
    ).fetchall()
    session_updates = []
    for project_id, cohort_id, session_id, fields in session_rows:
        current = dict(fields or {})
        mapped = _FILTER_TAG.get(current.get("filter"))
        if mapped is None:
            continue
        current["filter"] = mapped
        session_updates.append((json.dumps(current), project_id, cohort_id, session_id))
    if session_updates:
        conn.cursor().executemany(
            "UPDATE cohort_sessions SET fields = %s::jsonb"
            " WHERE project_id = %s AND cohort_id = %s AND session_id = %s",
            session_updates,
        )


if __name__ == "__main__":
    for version in migrate():
        print(f"applied {version}")
