import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def database_url() -> str:
    url = os.environ.get("CAPTURE_DATABASE_URL", "")
    if not url:
        raise RuntimeError("CAPTURE_DATABASE_URL is not set")
    return url


def admin_url() -> str:
    return os.environ.get("CAPTURE_DATABASE_ADMIN_URL") or database_url()


def configured() -> bool:
    return bool(os.environ.get("CAPTURE_DATABASE_URL"))


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    database_url(),
                    min_size=1,
                    max_size=int(os.environ.get("CAPTURE_DB_POOL_MAX", "10")),
                    # Fail fast when the database is down. The default 30s wait
                    # would queue ingests behind an outage until they time out
                    # client-side; a prompt 503 lets the recorder retry instead.
                    timeout=float(os.environ.get("CAPTURE_DB_WAIT_S", "5")),
                    open=True,
                )
    return _pool


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


@contextmanager
def connection(project_id=None, admin: bool = False):
    """A transaction scoped to one project. Every RLS-protected table is invisible
    unless project_id matches or admin is set, so a query that forgets its WHERE
    clause returns nothing instead of another tenant's rows."""
    if not admin and project_id is None:
        raise ValueError("connection() needs a project_id, or admin=True")
    with pool().connection() as conn:
        with conn.cursor() as cur:
            if admin:
                cur.execute("SELECT set_config('app.admin', 'on', true)")
            else:
                cur.execute("SELECT set_config('app.project_id', %s, true)", (str(project_id),))
        yield conn


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
            "UPDATE cohorts SET fields = %s::jsonb"
            " WHERE project_id = %s AND cohort_id = %s",
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
