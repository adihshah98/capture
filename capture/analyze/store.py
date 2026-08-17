"""Cohort state, in Postgres.

Ported from the SQLite version, keeping its data model and its public
signatures so analyze.py is unchanged: membership is its own table, the
columns worth filtering on are promoted out of the cohort dict, and everything
else rides in `fields` so the sampling vocabulary can change without a
migration.

Three things the move simplifies, all of which were SQLite constraints rather
than model ones:

  * `BEGIN IMMEDIATE` is gone. It existed because a deferred transaction takes
    its write lock at the first write, so two writers could both read and the
    second clobber the first. Postgres takes row locks on UPDATE and gives
    readers a consistent snapshot regardless.
  * The per-connection lock is gone. It guarded a `sqlite3.Connection`'s single
    transaction state, not the data; a pooled connection per transaction has no
    such thing to protect.
  * Reentrancy is gone. A nested call used to join the outer transaction via a
    thread-local, because SQLite's single write lock meant a nested acquire
    would deadlock a caller against itself. Postgres has no such lock, and no
    caller nests: every function here opens one transaction and passes `conn`
    to its helpers. If two writes ever need to commit together, thread `conn`
    through explicitly rather than reviving ambient state — the signature
    should say which transaction a call runs in.

`connect()` returns a handle rather than a connection. Callers only ever pass
it back into this module, and each entry point opens a pooled connection scoped
to one project_id, so row level security applies to cohorts exactly as it does to
sessions.
"""

import json
import time
from contextlib import asynccontextmanager

from psycopg.rows import dict_row

from .. import db

# Columns promoted out of the cohort blob. Everything else in a cohort dict
# (a_n, b_n, slice, selection, url_contains, ...) rides in `fields`.
COHORT_COLUMNS = ("id", "label", "status", "created_at", "updated_at", "batch")
SESSION_COLUMNS = ("session_id", "status", "analysis", "error")


class Store:
    """Stands in for the connection analyze.py used to pass around."""

    @asynccontextmanager
    async def tx(self, project_id: str):
        """A transaction scoped to one project: a pooled connection with
        `app.project_id` already set, so row level security applies.

        Yields `(conn, project_id)` as a pair so a function body cannot query
        under a project the connection was not stamped with.
        """
        async with db.connection(project_id) as conn:
            yield (conn, project_id)

    @asynccontextmanager
    async def admin_tx(self):
        """Across every project — only for resumable_cohorts, which has to look
        at all of them on the startup path."""
        async with db.connection(admin=True) as conn:
            yield conn


_store = Store()


def connect() -> Store:
    """The handle analyze threads through. Cohorts are addressed by project_id."""
    return _store


def _rows(conn):
    return conn.cursor(row_factory=dict_row)


async def _fetch_all(conn, sql: str, params=()) -> list:
    """Run a query and return dict rows.

    A helper rather than the chained `cursor.execute(...).fetchall()` the sync
    driver allowed: on an async connection both halves are coroutines, so the
    chain has to be broken up. Doing it in one place keeps the call sites
    reading the way they did before.
    """
    async with _rows(conn) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def _fetch_one(conn, sql: str, params=()):
    """Single dict row, or None. See `_fetch_all`."""
    async with _rows(conn) as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def _tuples(conn, sql: str, params=()) -> list:
    """Rows as plain tuples, for the queries that unpack positionally."""
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def _tuple(conn, sql: str, params=()):
    """Single tuple row, or None. See `_tuples`."""
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


def _row_to_cohort(row: dict, sessions: list) -> dict:
    out = dict(row.get("fields") or {})
    out["id"] = row["cohort_id"]
    out["status"] = row["status"]
    out["created_at"] = row["created_at"]
    out["updated_at"] = row["updated_at"]
    if row.get("label") is not None:
        out["label"] = row["label"]
    if row.get("batch"):
        out["batch"] = row["batch"]
    else:
        # Absent rather than null: callers test `cohort.get("batch")` and
        # `cohort.pop("batch", None)` to mean "no job in flight".
        out.pop("batch", None)
    out["sessions"] = sessions
    return out


def _row_to_session(row: dict) -> dict:
    out = dict(row.get("fields") or {})
    out["session_id"] = row["session_id"]
    for col in ("status", "analysis", "error"):
        if row.get(col) is not None:
            out[col] = row[col]
        else:
            out.pop(col, None)
    return out


def _split_cohort(cohort: dict) -> tuple:
    """Cohort dict → (column values, leftover fields, session rows)."""
    fields = {
        k: v
        for k, v in cohort.items()
        if k not in COHORT_COLUMNS and k not in ("sessions", "progress")
    }
    batch = cohort.get("batch")
    cols = {
        "id": str(cohort["id"]),
        "label": cohort.get("label"),
        "status": str(cohort.get("status") or "idle"),
        "created_at": int(cohort.get("created_at") or 0),
        "batch": json.dumps(batch) if batch else None,
    }
    return cols, fields, list(cohort.get("sessions") or [])


def _split_session(entry: dict) -> tuple:
    fields = {k: v for k, v in entry.items() if k not in SESSION_COLUMNS}
    return (
        str(entry.get("session_id") or ""),
        entry.get("status"),
        entry.get("analysis"),
        entry.get("error"),
        fields,
    )


def _now() -> int:
    return int(time.time() * 1000)


async def read_cohort(store: Store, project_id: str, cohort_id: str) -> dict | None:
    async with store.tx(project_id) as (conn, project_id):
        row = await _fetch_one(
            conn,
            "SELECT * FROM cohorts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        )
        if row is None:
            return None
        return _row_to_cohort(row, await _read_sessions(conn, project_id, cohort_id))


async def _read_sessions(conn, project_id, cohort_id: str) -> list:
    rows = await _fetch_all(
        conn,
        "SELECT * FROM cohort_sessions WHERE project_id = %s AND cohort_id = %s"
        " ORDER BY position",
        (project_id, cohort_id),
    )
    return [_row_to_session(r) for r in rows]


async def write_cohort(store: Store, project_id: str, cohort: dict) -> dict:
    """Create (or fully replace) a cohort and its membership. Returns the stored dict.

    This is the sampling write: a new frozen set of members. Sessions are
    replaced wholesale because the caller hands back a full list. The delete
    and insert share a transaction, so a reader never sees the cohort without
    its members.

    A later status, batch, or per-session mark must not go through here.
    DELETE+INSERT of membership races `update_session_status`: a snapshot
    reinserts stale queued rows over in-flight running/done marks. Use
    `set_cohort_status`, `persist_cohort_refresh`, or `update_session_status`.
    """
    cols, fields, sessions = _split_cohort(cohort)
    updated = _now()
    async with store.tx(project_id) as (conn, project_id):
        await conn.execute(
            """
            INSERT INTO cohorts
                (project_id, cohort_id, label, status, created_at, updated_at, batch, fields)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (project_id, cohort_id) DO UPDATE SET
                label = excluded.label,
                status = excluded.status,
                created_at = CASE WHEN excluded.created_at > 0
                                  THEN excluded.created_at ELSE cohorts.created_at END,
                updated_at = excluded.updated_at,
                batch = excluded.batch,
                fields = excluded.fields
            """,
            (
                project_id,
                cols["id"],
                cols["label"],
                cols["status"],
                cols["created_at"],
                updated,
                cols["batch"],
                json.dumps(fields),
            ),
        )
        await conn.execute(
            "DELETE FROM cohort_sessions WHERE project_id = %s AND cohort_id = %s",
            (project_id, cols["id"]),
        )
        rows = [
            (project_id, cols["id"], sid, i, status, analysis, error, json.dumps(extra))
            for i, (sid, status, analysis, error, extra) in (
                (i, _split_session(e)) for i, e in enumerate(sessions)
            )
        ]
        if rows:
            await conn.cursor().executemany(
                """
                INSERT INTO cohort_sessions
                    (project_id, cohort_id, session_id, position, status, analysis, error, fields)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                rows,
            )
    return {**cohort, "updated_at": updated}


async def update_session_status(
    store: Store,
    project_id: str,
    cohort_id: str,
    session_id: str,
    *,
    status: str,
    analysis: str | None = None,
    error: str | None = None,
) -> bool:
    """Mark one session in one cohort.

    The point of the table: per-session updates touch one row instead of
    rewriting every other session's state alongside it. `fields` is left alone
    — the sampling tag belongs to the membership, not to this run's progress.
    Returns whether a row matched, so a mark aimed at a session that is no
    longer a member is visible rather than silently dropped.
    """
    async with store.tx(project_id) as (conn, project_id):
        cur = await conn.execute(
            "UPDATE cohort_sessions SET status = %s, analysis = %s, error = %s"
            " WHERE project_id = %s AND cohort_id = %s AND session_id = %s",
            (status, analysis, error, project_id, cohort_id, session_id),
        )
        matched = cur.rowcount > 0
        if matched:
            await conn.execute(
                "UPDATE cohorts SET updated_at = %s WHERE project_id = %s AND cohort_id = %s",
                (_now(), project_id, cohort_id),
            )
    return matched


async def persist_cohort_refresh(
    store: Store,
    project_id: str,
    cohort_id: str,
    *,
    status: str | None = None,
    sessions: list | None = None,
) -> None:
    """Write status changes without replacing membership.

    `sessions` is `(session_id, status, analysis, error)` for rows that
    actually changed. Unlisted members are left alone — a GET poll must not
    reinsert a stale snapshot over in-flight marks from `update_session_status`.
    """
    rows = list(sessions or [])
    if status is None and not rows:
        return
    now = _now()
    async with store.tx(project_id) as (conn, project_id):
        if status is not None:
            await conn.execute(
                "UPDATE cohorts SET status = %s, updated_at = %s"
                " WHERE project_id = %s AND cohort_id = %s",
                (status, now, project_id, cohort_id),
            )
        else:
            await conn.execute(
                "UPDATE cohorts SET updated_at = %s WHERE project_id = %s AND cohort_id = %s",
                (now, project_id, cohort_id),
            )
        if rows:
            await conn.cursor().executemany(
                """
                UPDATE cohort_sessions SET status = %s, analysis = %s, error = %s
                 WHERE project_id = %s AND cohort_id = %s AND session_id = %s
                """,
                [
                    (sess_status, analysis, error, project_id, cohort_id, sid)
                    for sid, sess_status, analysis, error in rows
                ],
            )


async def set_cohort_label(store: Store, project_id: str, cohort_id: str, label: str) -> bool:
    """Set a cohort's display name without rewriting its membership."""
    async with store.tx(project_id) as (conn, project_id):
        cur = await conn.execute(
            "UPDATE cohorts SET label = %s, updated_at = %s"
            " WHERE project_id = %s AND cohort_id = %s",
            (label, _now(), project_id, cohort_id),
        )
        return cur.rowcount > 0


async def set_cohort_status(
    store: Store,
    project_id: str,
    cohort_id: str,
    status: str,
    *,
    batch: dict | None = None,
    clear_batch: bool = False,
) -> None:
    """Move a cohort's own status (and batch handle) without rewriting membership."""
    async with store.tx(project_id) as (conn, project_id):
        if clear_batch:
            await conn.execute(
                "UPDATE cohorts SET status = %s, batch = NULL, updated_at = %s"
                " WHERE project_id = %s AND cohort_id = %s",
                (status, _now(), project_id, cohort_id),
            )
        elif batch is not None:
            await conn.execute(
                "UPDATE cohorts SET status = %s, batch = %s::jsonb, updated_at = %s"
                " WHERE project_id = %s AND cohort_id = %s",
                (status, json.dumps(batch), _now(), project_id, cohort_id),
            )
        else:
            await conn.execute(
                "UPDATE cohorts SET status = %s, updated_at = %s"
                " WHERE project_id = %s AND cohort_id = %s",
                (status, _now(), project_id, cohort_id),
            )


async def list_cohorts(store: Store, project_id: str) -> list:
    """Every cohort in this project, newest first, with membership.

    Two queries regardless of how many cohorts come back. This used to read the
    membership per cohort, so a project with fifty of them issued fifty-one
    queries on a dashboard load; the rows are now fetched in one pass and
    grouped in Python.
    """
    async with store.tx(project_id) as (conn, project_id):
        rows = await _fetch_all(
            conn,
            "SELECT * FROM cohorts WHERE project_id = %s"
            " ORDER BY created_at DESC, cohort_id DESC",
            (project_id,),
        )
        if not rows:
            return []
        member_rows = await _fetch_all(
            conn,
            "SELECT * FROM cohort_sessions WHERE project_id = %s ORDER BY cohort_id, position",
            (project_id,),
        )
        by_cohort: dict = {}
        for member in member_rows:
            by_cohort.setdefault(member["cohort_id"], []).append(_row_to_session(member))
        return [_row_to_cohort(r, by_cohort.get(r["cohort_id"], [])) for r in rows]


async def resumable_cohorts(store: Store) -> list:
    """(project_id, cohort) for every cohort with an in-flight batch job name.

    Spans projects, so it runs with admin scope — it is the startup path
    deciding what to re-attach to, before any request has named a project.
    Callers that need a live Project (startup resume) resolve the id once.
    """
    out = []
    async with store.admin_tx() as conn:
        rows = await _fetch_all(
            conn, "SELECT * FROM cohorts WHERE status = 'running' AND batch IS NOT NULL"
        )
        for row in rows:
            if not (row.get("batch") or {}).get("name"):
                continue
            pid = str(row["project_id"])
            sessions = await _read_sessions(conn, row["project_id"], row["cohort_id"])
            out.append((pid, _row_to_cohort(row, sessions)))
    return out


async def delete_cohort(store: Store, project_id: str, cohort_id: str) -> bool:
    async with store.tx(project_id) as (conn, project_id):
        cur = await conn.execute(
            "DELETE FROM cohorts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        )
        found = cur.rowcount > 0
        # Membership cascades from the cohort row; cuts have no foreign key,
        # so they go explicitly.
        await conn.execute(
            "DELETE FROM cohort_cuts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        )
    return found


async def forget_session(store: Store, project_id: str, session_id: str) -> int:
    """Drop a deleted session from every cohort that held it.

    A cohort member is a reference to a recording, not a copy of it. Once the
    recording is gone the row cannot be substantiated, and leaving it behind is
    worse than dropping it — enrich_cohort_status falls through to the stored
    status when it finds nothing, so a deleted session keeps being counted as a
    completed analysis and the cohort reports a result it cannot back up.

    Returns the number of memberships removed. Cohorts shrink rather than
    error: a frozen sample whose evidence was deleted is smaller, not broken.
    Cached cuts are deliberately left alone — they record what the model said
    when it ran, and rewriting them to match would be a second fabrication.
    """
    async with store.tx(project_id) as (conn, project_id):
        owners = await _tuples(
            conn,
            "SELECT DISTINCT cohort_id FROM cohort_sessions"
            " WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )
        if not owners:
            return 0
        cur = await conn.execute(
            "DELETE FROM cohort_sessions WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )
        removed = cur.rowcount
        # Bump the owners so a poller sees the membership change.
        await conn.cursor().executemany(
            "UPDATE cohorts SET updated_at = %s WHERE project_id = %s AND cohort_id = %s",
            [(_now(), project_id, r[0]) for r in owners],
        )
    return removed


async def forget_pool(store: Store, project_id: str) -> int:
    """Drop every cohort in a project. For a whole-project reset."""
    async with store.tx(project_id) as (conn, project_id):
        cur = await conn.execute("DELETE FROM cohort_sessions WHERE project_id = %s", (project_id,))
        removed = cur.rowcount
        await conn.execute("DELETE FROM cohort_cuts WHERE project_id = %s", (project_id,))
        await conn.execute("DELETE FROM cohorts WHERE project_id = %s", (project_id,))
    return removed


async def read_cuts(store: Store, project_id: str, cohort_id: str) -> dict:
    async with store.tx(project_id) as (conn, project_id):
        rows = await _tuples(
            conn,
            "SELECT cut_key, cut FROM cohort_cuts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        )
    return {key: cut for key, cut in rows if isinstance(cut, dict)}


async def read_cut(store: Store, project_id: str, cohort_id: str, key: str) -> dict | None:
    async with store.tx(project_id) as (conn, project_id):
        row = await _tuple(
            conn,
            "SELECT cut FROM cohort_cuts"
            " WHERE project_id = %s AND cohort_id = %s AND cut_key = %s",
            (project_id, cohort_id, key),
        )
    if row is None:
        return None
    return row[0] if isinstance(row[0], dict) else None


async def write_cut(store: Store, project_id: str, cohort_id: str, key: str, cut: dict) -> dict:
    """Cache one cut. One row, so concurrent cuts of one cohort cannot drop
    each other the way the read-mutate-write of the whole map did."""
    async with store.tx(project_id) as (conn, project_id):
        await conn.execute(
            "INSERT INTO cohort_cuts (project_id, cohort_id, cut_key, cut)"
            " VALUES (%s, %s, %s, %s::jsonb)"
            " ON CONFLICT (project_id, cohort_id, cut_key) DO UPDATE SET cut = excluded.cut",
            (project_id, cohort_id, key, json.dumps(cut)),
        )
    return cut


async def clear_cut(store: Store, project_id: str, cohort_id: str, key: str) -> bool:
    async with store.tx(project_id) as (conn, project_id):
        cur = await conn.execute(
            "DELETE FROM cohort_cuts" " WHERE project_id = %s AND cohort_id = %s AND cut_key = %s",
            (project_id, cohort_id, key),
        )
    return cur.rowcount > 0


# --- per-session analysis results and job status ------------------------------
# These were analysis.json and analysis.status.json beside the recording. The
# write-up is the durable row; extractor frames are local scratch, copied to
# S3 next to the chunks so a later box can skip Playwright.


def _id_list(session_ids) -> list:
    return [str(sid) for sid in session_ids if sid]


async def read_analysis(store: Store, project_id: str, session_id: str) -> dict | None:
    async with store.tx(project_id) as (conn, project_id):
        row = await _tuple(
            conn,
            "SELECT body FROM analyses WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )
    if row is None:
        return None
    return row[0] if isinstance(row[0], dict) else None


async def read_analyses(store: Store, project_id: str, session_ids) -> dict:
    """session_id → write-up for the ids that have one. Missing ids are omitted."""
    ids = _id_list(session_ids)
    if not ids:
        return {}
    placeholders = ", ".join("%s" for _ in ids)
    async with store.tx(project_id) as (conn, project_id):
        rows = await _tuples(
            conn,
            f"SELECT session_id, body FROM analyses"
            f" WHERE project_id = %s AND session_id IN ({placeholders})",
            (project_id, *ids),
        )
    return {sid: body for sid, body in rows if isinstance(body, dict)}


async def write_analysis(store: Store, project_id: str, session_id: str, result: dict) -> dict:
    async with store.tx(project_id) as (conn, project_id):
        await conn.execute(
            "INSERT INTO analyses (project_id, session_id, body, created_at)"
            " VALUES (%s, %s, %s::jsonb, %s)"
            " ON CONFLICT (project_id, session_id) DO UPDATE SET"
            " body = excluded.body, created_at = excluded.created_at",
            (
                project_id,
                session_id,
                json.dumps(result),
                int(result.get("created_at") or _now()),
            ),
        )
    return result


async def clear_analysis(store: Store, project_id: str, session_id: str) -> None:
    async with store.tx(project_id) as (conn, project_id):
        await conn.execute(
            "DELETE FROM analyses WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )


async def forget_analyses(store: Store, project_id: str) -> int:
    """Drop every write-up and job in a project. The analysis half of a reset,
    alongside forget_pool for cohorts."""
    async with store.tx(project_id) as (conn, project_id):
        cur = await conn.execute("DELETE FROM analyses WHERE project_id = %s", (project_id,))
        removed = cur.rowcount
        await conn.execute("DELETE FROM analysis_jobs WHERE project_id = %s", (project_id,))
    return removed


async def read_job(store: Store, project_id: str, session_id: str) -> dict | None:
    async with store.tx(project_id) as (conn, project_id):
        row = await _fetch_one(
            conn,
            "SELECT status, error, updated_at FROM analysis_jobs"
            " WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )
    if row is None:
        return None
    job = {"status": row["status"], "updated_at": row["updated_at"]}
    if row["error"]:
        job["error"] = row["error"]
    return job


async def read_jobs(store: Store, project_id: str, session_ids) -> dict:
    """session_id → job row for the ids that have one. Missing ids are omitted."""
    ids = _id_list(session_ids)
    if not ids:
        return {}
    placeholders = ", ".join("%s" for _ in ids)
    async with store.tx(project_id) as (conn, project_id):
        rows = await _fetch_all(
            conn,
            f"SELECT session_id, status, error, updated_at FROM analysis_jobs"
            f" WHERE project_id = %s AND session_id IN ({placeholders})",
            (project_id, *ids),
        )
    out = {}
    for row in rows:
        job = {"status": row["status"], "updated_at": row["updated_at"]}
        if row["error"]:
            job["error"] = row["error"]
        out[row["session_id"]] = job
    return out


async def write_job(store: Store, project_id: str, session_id: str, job: dict) -> dict:
    async with store.tx(project_id) as (conn, project_id):
        await conn.execute(
            "INSERT INTO analysis_jobs (project_id, session_id, status, error, updated_at)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (project_id, session_id) DO UPDATE SET"
            " status = excluded.status, error = excluded.error,"
            " updated_at = excluded.updated_at",
            (project_id, session_id, job["status"], job.get("error"), job["updated_at"]),
        )
    return job


async def clear_job(store: Store, project_id: str, session_id: str) -> None:
    async with store.tx(project_id) as (conn, project_id):
        await conn.execute(
            "DELETE FROM analysis_jobs WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )


async def session_facts(store: Store, project_id: str, session_id: str) -> tuple:
    """(entry url, order) for one session — what visitor.json and order.json held.

    The entry URL keeps its query string: it is the only place the UTMs
    survive, since page_path() drops them and the digest's URLs are
    post-navigation.
    """
    async with store.tx(project_id) as (conn, project_id):
        row = await _fetch_one(
            conn,
            "SELECT s.url, o.checkout_started_at, o.purchased_at, o.order_id,"
            " o.total, o.currency"
            " FROM sessions s"
            " LEFT JOIN orders o"
            "   ON o.project_id = s.project_id AND o.session_id = s.session_id"
            " WHERE s.project_id = %s AND s.session_id = %s",
            (project_id, session_id),
        )
    if row is None:
        return None, None
    order = {
        k: row[k]
        for k in ("checkout_started_at", "purchased_at", "order_id", "total", "currency")
        if row[k] is not None
    }
    return row["url"], (order or None)
