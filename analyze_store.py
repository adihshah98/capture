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
  * The transaction is still reentrant, because migrate_json_cohorts calls
    write_cohort inside its own and the inner call must join it rather than
    deadlock waiting on a row its own caller holds.

`connect()` returns a handle rather than a connection. Callers only ever pass
it back into this module, and each entry point opens a pooled connection scoped
to one project, so row level security applies to cohorts exactly as it does to
sessions.
"""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from psycopg.rows import dict_row

import db
import projects

# Columns promoted out of the cohort blob. Everything else in a cohort dict
# (purchased_n, all_n, slice, selection, url_contains, ...) rides in `fields`.
COHORT_COLUMNS = ("id", "label", "status", "created_at", "updated_at", "batch")
SESSION_COLUMNS = ("session_id", "status", "analysis", "error")


class Store:
    """Stands in for the connection analyze.py used to pass around."""

    def __init__(self):
        self._local = threading.local()

    @contextmanager
    def tx(self, pool_key: str):
        """A transaction scoped to the project owning this pool. Reentrant: a
        nested call joins the outer transaction instead of waiting on a row the
        outer one already holds."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            yield existing
            return
        project = projects.by_handle(pool_key)
        if project is None:
            raise LookupError(f"no project for pool {pool_key!r}")
        with db.connection(project.id) as conn:
            self._local.conn = (conn, project.id)
            try:
                yield (conn, project.id)
            finally:
                self._local.conn = None

    @contextmanager
    def admin_tx(self):
        """Across every project — only for resumable_cohorts, which has to look
        at all of them on the startup path."""
        with db.connection(admin=True) as conn:
            yield conn


_store = Store()


def connect(data_root: Path = None) -> Store:
    """The handle analyze.py threads through. data_root is accepted and ignored:
    cohorts are no longer addressed by where the pool sits on disk."""
    return _store


def close_all() -> None:
    """The connection pool is db.py's; nothing per-store to close."""


def _rows(conn):
    return conn.cursor(row_factory=dict_row)


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


def read_cohort(store: Store, pool_key: str, cohort_id: str) -> Optional[dict]:
    with store.tx(pool_key) as (conn, project_id):
        row = _rows(conn).execute(
            "SELECT * FROM cohorts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        ).fetchone()
        if row is None:
            return None
        return _row_to_cohort(row, _read_sessions(conn, project_id, cohort_id))


def _read_sessions(conn, project_id, cohort_id: str) -> list:
    rows = _rows(conn).execute(
        "SELECT * FROM cohort_sessions WHERE project_id = %s AND cohort_id = %s"
        " ORDER BY position",
        (project_id, cohort_id),
    ).fetchall()
    return [_row_to_session(r) for r in rows]


def write_cohort(store: Store, pool_key: str, cohort: dict) -> dict:
    """Upsert a cohort and its membership. Returns the stored dict.

    Sessions are replaced wholesale because callers hand back a full list. The
    delete and insert share a transaction, so a reader never sees the cohort
    without its members.
    """
    cols, fields, sessions = _split_cohort(cohort)
    updated = _now()
    with store.tx(pool_key) as (conn, project_id):
        conn.execute(
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
                project_id, cols["id"], cols["label"], cols["status"],
                cols["created_at"], updated, cols["batch"], json.dumps(fields),
            ),
        )
        conn.execute(
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
            conn.cursor().executemany(
                """
                INSERT INTO cohort_sessions
                    (project_id, cohort_id, session_id, position, status, analysis, error, fields)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                rows,
            )
    return {**cohort, "updated_at": updated}


def update_session_status(
    store: Store, pool_key: str, cohort_id: str, session_id: str, *,
    status: str, analysis: Optional[str] = None, error: Optional[str] = None,
) -> bool:
    """Mark one session in one cohort.

    The point of the table: per-session updates touch one row instead of
    rewriting every other session's state alongside it. `fields` is left alone
    — the sampling tag belongs to the membership, not to this run's progress.
    Returns whether a row matched, so a mark aimed at a session that is no
    longer a member is visible rather than silently dropped.
    """
    with store.tx(pool_key) as (conn, project_id):
        cur = conn.execute(
            "UPDATE cohort_sessions SET status = %s, analysis = %s, error = %s"
            " WHERE project_id = %s AND cohort_id = %s AND session_id = %s",
            (status, analysis, error, project_id, cohort_id, session_id),
        )
        matched = cur.rowcount > 0
        if matched:
            conn.execute(
                "UPDATE cohorts SET updated_at = %s WHERE project_id = %s AND cohort_id = %s",
                (_now(), project_id, cohort_id),
            )
    return matched


def set_cohort_status(
    store: Store, pool_key: str, cohort_id: str, status: str, *,
    batch: Optional[dict] = None, clear_batch: bool = False,
) -> None:
    """Move a cohort's own status without rewriting its membership."""
    with store.tx(pool_key) as (conn, project_id):
        if clear_batch:
            conn.execute(
                "UPDATE cohorts SET status = %s, batch = NULL, updated_at = %s"
                " WHERE project_id = %s AND cohort_id = %s",
                (status, _now(), project_id, cohort_id),
            )
        elif batch is not None:
            conn.execute(
                "UPDATE cohorts SET status = %s, batch = %s::jsonb, updated_at = %s"
                " WHERE project_id = %s AND cohort_id = %s",
                (status, json.dumps(batch), _now(), project_id, cohort_id),
            )
        else:
            conn.execute(
                "UPDATE cohorts SET status = %s, updated_at = %s"
                " WHERE project_id = %s AND cohort_id = %s",
                (status, _now(), project_id, cohort_id),
            )


def list_cohorts(store: Store, pool_key: str) -> list:
    """Every cohort in this pool, newest first, with membership."""
    with store.tx(pool_key) as (conn, project_id):
        rows = _rows(conn).execute(
            "SELECT * FROM cohorts WHERE project_id = %s"
            " ORDER BY created_at DESC, cohort_id DESC",
            (project_id,),
        ).fetchall()
        return [
            _row_to_cohort(r, _read_sessions(conn, project_id, r["cohort_id"])) for r in rows
        ]


def resumable_cohorts(store: Store) -> list:
    """(pool_key, cohort) for every cohort with an in-flight batch job name.

    Spans projects, so it runs with admin scope — it is the startup path
    deciding what to re-attach to, before any request has named a project.
    """
    handles = {p.id: p.handle for p in projects.all_projects(fresh=True)}
    out = []
    with store.admin_tx() as conn:
        rows = _rows(conn).execute(
            "SELECT * FROM cohorts WHERE status = 'running' AND batch IS NOT NULL"
        ).fetchall()
        for row in rows:
            if not (row.get("batch") or {}).get("name"):
                continue
            handle = handles.get(str(row["project_id"]))
            if handle is None:
                continue
            sessions = _read_sessions(conn, row["project_id"], row["cohort_id"])
            out.append((handle, _row_to_cohort(row, sessions)))
    return out


def delete_cohort(store: Store, pool_key: str, cohort_id: str) -> bool:
    with store.tx(pool_key) as (conn, project_id):
        cur = conn.execute(
            "DELETE FROM cohorts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        )
        found = cur.rowcount > 0
        # Membership cascades from the cohort row; cuts have no foreign key,
        # so they go explicitly.
        conn.execute(
            "DELETE FROM cohort_cuts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        )
    return found


def forget_session(store: Store, pool_key: str, session_id: str) -> int:
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
    with store.tx(pool_key) as (conn, project_id):
        owners = conn.execute(
            "SELECT DISTINCT cohort_id FROM cohort_sessions"
            " WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        ).fetchall()
        if not owners:
            return 0
        cur = conn.execute(
            "DELETE FROM cohort_sessions WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )
        removed = cur.rowcount
        # Bump the owners so a poller sees the membership change.
        conn.cursor().executemany(
            "UPDATE cohorts SET updated_at = %s WHERE project_id = %s AND cohort_id = %s",
            [(_now(), project_id, r[0]) for r in owners],
        )
    return removed


def forget_pool(store: Store, pool_key: str) -> int:
    """Drop every cohort in a pool. For a whole-pool reset."""
    with store.tx(pool_key) as (conn, project_id):
        removed = conn.execute(
            "DELETE FROM cohort_sessions WHERE project_id = %s", (project_id,)
        ).rowcount
        conn.execute("DELETE FROM cohort_cuts WHERE project_id = %s", (project_id,))
        conn.execute("DELETE FROM cohorts WHERE project_id = %s", (project_id,))
    return removed


def read_cuts(store: Store, pool_key: str, cohort_id: str) -> dict:
    with store.tx(pool_key) as (conn, project_id):
        rows = conn.execute(
            "SELECT cut_key, cut FROM cohort_cuts WHERE project_id = %s AND cohort_id = %s",
            (project_id, cohort_id),
        ).fetchall()
    return {key: cut for key, cut in rows if isinstance(cut, dict)}


def read_cut(store: Store, pool_key: str, cohort_id: str, key: str) -> Optional[dict]:
    with store.tx(pool_key) as (conn, project_id):
        row = conn.execute(
            "SELECT cut FROM cohort_cuts"
            " WHERE project_id = %s AND cohort_id = %s AND cut_key = %s",
            (project_id, cohort_id, key),
        ).fetchone()
    if row is None:
        return None
    return row[0] if isinstance(row[0], dict) else None


def write_cut(store: Store, pool_key: str, cohort_id: str, key: str, cut: dict) -> dict:
    """Cache one cut. One row, so concurrent cuts of one cohort cannot drop
    each other the way the read-mutate-write of the whole map did."""
    with store.tx(pool_key) as (conn, project_id):
        conn.execute(
            "INSERT INTO cohort_cuts (project_id, cohort_id, cut_key, cut)"
            " VALUES (%s, %s, %s, %s::jsonb)"
            " ON CONFLICT (project_id, cohort_id, cut_key) DO UPDATE SET cut = excluded.cut",
            (project_id, cohort_id, key, json.dumps(cut)),
        )
    return cut


def clear_cut(store: Store, pool_key: str, cohort_id: str, key: str) -> bool:
    with store.tx(pool_key) as (conn, project_id):
        cur = conn.execute(
            "DELETE FROM cohort_cuts"
            " WHERE project_id = %s AND cohort_id = %s AND cut_key = %s",
            (project_id, cohort_id, key),
        )
    return cur.rowcount > 0


def migrate_json_cohorts(
    store: Store, pool_key: str, cohorts_dir: Path, cuts_suffix: str
) -> int:
    """Import cohorts/*.json and *.cuts.json, then delete them.

    Migrate once and leave nothing behind that a later reader could mistake for
    live state. A cohort already stored wins — a re-run must not roll back
    newer state from a file that was never cleaned up.
    """
    directory = Path(cohorts_dir)
    if not directory.is_dir():
        return 0

    # One transaction for the whole import, so two servers starting together
    # cannot both read "not present" and double-write. The nested write_cohort
    # and write_cut calls join it rather than opening their own.
    with store.tx(pool_key):
        moved = _import_dir(store, pool_key, directory, cuts_suffix)

    # Non-empty (an unreadable file deliberately kept) means rmdir fails, which
    # is the correct outcome.
    try:
        directory.rmdir()
    except OSError:
        pass
    return moved


def _import_dir(store: Store, pool_key: str, directory: Path, cuts_suffix: str) -> int:
    moved = 0
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(cuts_suffix):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            # Leave an unreadable file in place: deleting it would destroy the
            # only copy of state we failed to import.
            print(f"cohort migration: skipping {path.name}: {exc}")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            continue
        cohort_id = data["id"]
        if not read_cohort(store, pool_key, cohort_id):
            # `progress` is a tally of `sessions` older writers persisted; it is
            # view-only now and _split_cohort drops it.
            write_cohort(store, pool_key, data)
            moved += 1

        cuts_file = directory / f"{cohort_id}{cuts_suffix}"
        if cuts_file.is_file():
            try:
                cuts = json.loads(cuts_file.read_text())
            except (OSError, ValueError) as exc:
                print(f"cohort migration: skipping {cuts_file.name}: {exc}")
                cuts = None
            if isinstance(cuts, dict):
                for key, cut in cuts.items():
                    if isinstance(cut, dict) and not read_cut(store, pool_key, cohort_id, key):
                        write_cut(store, pool_key, cohort_id, key, cut)
                cuts_file.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    return moved


def migrate_sqlite_cohorts(sqlite_path: Path) -> dict:
    """Import cohort state written by the SQLite version of this module.

    The intermediate step: cohorts moved out of JSON documents into
    <data_root>/index.db before they moved here. Existing rows win, so this is
    safe to re-run and cannot roll back newer state.
    """
    counts = {"cohorts": 0, "sessions": 0, "cuts": 0, "skipped_pools": 0}
    path = Path(sqlite_path)
    if not path.is_file():
        return counts

    legacy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    legacy.row_factory = sqlite3.Row
    try:
        names = {r[0] for r in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        if "cohorts" not in names:
            return counts

        for row in legacy.execute("SELECT * FROM cohorts").fetchall():
            pool_key = row["pool"]
            if projects.by_handle(pool_key) is None:
                counts["skipped_pools"] += 1
                continue
            cohort = dict(json.loads(row["fields"] or "{}"))
            cohort.update(
                id=row["id"], status=row["status"],
                created_at=row["created_at"], updated_at=row["updated_at"],
            )
            if row["label"] is not None:
                cohort["label"] = row["label"]
            if row["batch"]:
                cohort["batch"] = json.loads(row["batch"])
            sessions = []
            for s in legacy.execute(
                "SELECT * FROM cohort_sessions WHERE pool = ? AND cohort_id = ?"
                " ORDER BY position", (pool_key, row["id"]),
            ).fetchall():
                entry = dict(json.loads(s["fields"] or "{}"))
                entry["session_id"] = s["session_id"]
                for col in ("status", "analysis", "error"):
                    if s[col] is not None:
                        entry[col] = s[col]
                sessions.append(entry)
            cohort["sessions"] = sessions

            if read_cohort(_store, pool_key, row["id"]):
                continue
            write_cohort(_store, pool_key, cohort)
            counts["cohorts"] += 1
            counts["sessions"] += len(sessions)

        if "cohort_cuts" in names:
            for row in legacy.execute("SELECT * FROM cohort_cuts").fetchall():
                pool_key = row["pool"]
                if projects.by_handle(pool_key) is None:
                    continue
                try:
                    cut = json.loads(row["cut"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(cut, dict):
                    continue
                if read_cut(_store, pool_key, row["cohort_id"], row["key"]):
                    continue
                write_cut(_store, pool_key, row["cohort_id"], row["key"], cut)
                counts["cuts"] += 1
    finally:
        legacy.close()
    return counts


# --- per-session analysis results and job status ------------------------------
# These were analysis.json and analysis.status.json beside the recording. The
# frame cache stays on disk: it is regenerable scratch for one run, not state.


def read_analysis(store: Store, pool_key: str, session_id: str) -> Optional[dict]:
    with store.tx(pool_key) as (conn, project_id):
        row = conn.execute(
            "SELECT body FROM analyses WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        ).fetchone()
    if row is None:
        return None
    return row[0] if isinstance(row[0], dict) else None


def write_analysis(store: Store, pool_key: str, session_id: str, result: dict) -> dict:
    with store.tx(pool_key) as (conn, project_id):
        conn.execute(
            "INSERT INTO analyses (project_id, session_id, body, created_at)"
            " VALUES (%s, %s, %s::jsonb, %s)"
            " ON CONFLICT (project_id, session_id) DO UPDATE SET"
            " body = excluded.body, created_at = excluded.created_at",
            (
                project_id, session_id, json.dumps(result),
                int(result.get("created_at") or _now()),
            ),
        )
    return result


def clear_analysis(store: Store, pool_key: str, session_id: str) -> None:
    with store.tx(pool_key) as (conn, project_id):
        conn.execute(
            "DELETE FROM analyses WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )


def forget_analyses(store: Store, pool_key: str) -> int:
    """Drop every write-up and job in a pool. The analysis half of a reset,
    alongside forget_pool for cohorts."""
    with store.tx(pool_key) as (conn, project_id):
        removed = conn.execute(
            "DELETE FROM analyses WHERE project_id = %s", (project_id,)
        ).rowcount
        conn.execute("DELETE FROM analysis_jobs WHERE project_id = %s", (project_id,))
    return removed


def read_job(store: Store, pool_key: str, session_id: str) -> Optional[dict]:
    with store.tx(pool_key) as (conn, project_id):
        row = _rows(conn).execute(
            "SELECT status, error, updated_at FROM analysis_jobs"
            " WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        ).fetchone()
    if row is None:
        return None
    job = {"status": row["status"], "updated_at": row["updated_at"]}
    if row["error"]:
        job["error"] = row["error"]
    return job


def write_job(store: Store, pool_key: str, session_id: str, job: dict) -> dict:
    with store.tx(pool_key) as (conn, project_id):
        conn.execute(
            "INSERT INTO analysis_jobs (project_id, session_id, status, error, updated_at)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (project_id, session_id) DO UPDATE SET"
            " status = excluded.status, error = excluded.error,"
            " updated_at = excluded.updated_at",
            (project_id, session_id, job["status"], job.get("error"), job["updated_at"]),
        )
    return job


def clear_job(store: Store, pool_key: str, session_id: str) -> None:
    with store.tx(pool_key) as (conn, project_id):
        conn.execute(
            "DELETE FROM analysis_jobs WHERE project_id = %s AND session_id = %s",
            (project_id, session_id),
        )


def session_facts(store: Store, pool_key: str, session_id: str) -> tuple:
    """(entry url, order) for one session — what visitor.json and order.json held.

    The entry URL keeps its query string: it is the only place the UTMs
    survive, since page_path() drops them and the digest's URLs are
    post-navigation.
    """
    with store.tx(pool_key) as (conn, project_id):
        row = _rows(conn).execute(
            "SELECT s.url, o.checkout_started_at, o.purchased_at, o.order_id,"
            " o.total, o.currency"
            " FROM sessions s"
            " LEFT JOIN orders o"
            "   ON o.project_id = s.project_id AND o.session_id = s.session_id"
            " WHERE s.project_id = %s AND s.session_id = %s",
            (project_id, session_id),
        ).fetchone()
    if row is None:
        return None, None
    order = {
        k: row[k]
        for k in ("checkout_started_at", "purchased_at", "order_id", "total", "currency")
        if row[k] is not None
    }
    return row["url"], (order or None)
