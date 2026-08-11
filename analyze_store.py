"""SQLite storage for cohort state.

Cohort membership, per-session status and the cut cache used to be JSON
documents rewritten in full by concurrent writers. `write_cohort` is called
inside loops — once per session in `apply_cohort_batch`, once per extraction
start in `extract_one` — so a 100-session cohort meant hundreds of
full-document rewrites, any one of which could be interrupted. A truncated
write made `_read_cohort_file` return None and the cohort vanished from the UI
along with its in-flight batch job name. `write_cut` was a plain lost update:
read the whole map, mutate, write it back.

This module keeps the same function names and dict shapes the rest of the
codebase already passes around, so callers see a storage swap and nothing else.
What changed underneath:

- a cohort's session rows are their own table, so marking one session done is
  an UPDATE of one row rather than a rewrite of the document;
- read-modify-write happens inside IMMEDIATE transactions, so two cuts of one
  cohort finishing together no longer drop one;
- write-up markdown and JPEG frames stay on disk, referenced by path. Only the
  small relational state moved.

The connection lives here rather than in server.py so that `analyze` does not
depend on the web layer — the CLI path builds a store the same way the server
does.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

# Cohort ids reach this module from the same validated path as before; the
# store keys on them but never builds a filesystem path out of them.

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohorts (
    pool         TEXT NOT NULL,
    id           TEXT NOT NULL,
    label        TEXT,
    status       TEXT NOT NULL DEFAULT 'idle',
    created_at   INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL DEFAULT 0,
    batch        TEXT,
    fields       TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (pool, id)
);

CREATE TABLE IF NOT EXISTS cohort_sessions (
    pool       TEXT NOT NULL,
    cohort_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    position   INTEGER NOT NULL,
    status     TEXT,
    analysis   TEXT,
    error      TEXT,
    fields     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (pool, cohort_id, session_id)
    -- A real FK to sessions (pool, session_id) is not declared: SQLite ignores
    -- foreign keys unless every connection sets PRAGMA foreign_keys=ON, and the
    -- connection that does the deleting is server.py's, which does not. The
    -- rule is enforced by forget_session() at the delete paths instead, where
    -- it is visible in the code doing the deleting.
);

CREATE TABLE IF NOT EXISTS cohort_cuts (
    pool      TEXT NOT NULL,
    cohort_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    cut       TEXT NOT NULL,
    PRIMARY KEY (pool, cohort_id, key)
);

CREATE INDEX IF NOT EXISTS cohorts_recency
    ON cohorts (pool, created_at DESC);
-- forget_session deletes by session across every cohort; the primary key
-- leads with cohort_id, so without this that is a full table scan on a path
-- the retention sweep walks once per expired session.
CREATE INDEX IF NOT EXISTS cohort_sessions_by_session
    ON cohort_sessions (pool, session_id);
-- cohorts_needing_batch_resume was a directory scan reading every cohort file
-- on every server start; with this it is an index lookup.
CREATE INDEX IF NOT EXISTS cohorts_resumable
    ON cohorts (status) WHERE batch IS NOT NULL;
"""

# Columns promoted out of the cohort blob. Everything else in a cohort dict
# (purchased_n, all_n, slice, selection, url_contains, ...) rides in `fields`,
# which keeps the sampling vocabulary free to change without a migration.
COHORT_COLUMNS = ("id", "label", "status", "created_at", "updated_at", "batch")
SESSION_COLUMNS = ("session_id", "status", "analysis", "error")

_conns: dict = {}
_lock = threading.Lock()


def connect(data_root: Path) -> sqlite3.Connection:
    """Open (once per data root) the index database that holds cohort state.

    Same file as the session index, so a cohort can be joined against
    `sessions` instead of being enriched row by row in Python. WAL means this
    connection and server.py's own reader coexist.
    """
    key = str(Path(data_root).resolve())
    with _lock:
        conn = _conns.get(key)
        if conn is not None:
            return conn
        path = Path(data_root)
        path.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path / "index.db", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "PRAGMA journal_mode=WAL;\nPRAGMA synchronous=NORMAL;\n" + SCHEMA
        )
        conn.commit()
        _conns[key] = conn
        return conn


def close_all() -> None:
    """Drop cached connections. Tests use this between temp data roots."""
    with _lock:
        for conn in _conns.values():
            try:
                conn.close()
            except Exception:
                pass
        _conns.clear()


# One lock per connection. A sqlite3.Connection has a single transaction
# state, so `check_same_thread=False` lets threads share it but does not make
# it concurrent: two threads issuing BEGIN IMMEDIATE raise "cannot start a
# transaction within a transaction", and their cursors interleave into
# InterfaceError. server.py guards its own index connection the same way.
# Serializing is not a real cost — SQLite admits one writer regardless.
_tx_locks: dict = {}


def _tx_lock(conn: sqlite3.Connection) -> threading.RLock:
    with _lock:
        got = _tx_locks.get(id(conn))
        if got is None:
            got = threading.RLock()
            _tx_locks[id(conn)] = got
        return got


class _Tx:
    """BEGIN IMMEDIATE around a read-modify-write, serialized per connection.

    The default deferred transaction takes its write lock only at the first
    write, so two writers can both read, both decide, and the second clobbers
    the first — the `write_cut` lost update. IMMEDIATE takes the lock up front,
    which also makes SQLite raise rather than silently interleave if a caller
    ever nests one of these.

    Reentrant so a helper called inside a transaction (migrate_json_cohorts
    calling write_cohort) does not deadlock; the inner BEGIN is skipped and the
    outermost block owns the commit.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lock = _tx_lock(conn)
        self.outermost = False

    def __enter__(self) -> sqlite3.Connection:
        self.lock.acquire()
        # in_transaction is True only if an enclosing _Tx already began one.
        self.outermost = not self.conn.in_transaction
        if self.outermost:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if self.outermost:
                if exc_type is None:
                    self.conn.commit()
                else:
                    self.conn.rollback()
        finally:
            self.lock.release()
        return False


def _row_to_cohort(row: sqlite3.Row, sessions: list) -> dict:
    out = dict(json.loads(row["fields"] or "{}"))
    out["id"] = row["id"]
    out["status"] = row["status"]
    out["created_at"] = row["created_at"]
    out["updated_at"] = row["updated_at"]
    if row["label"] is not None:
        out["label"] = row["label"]
    if row["batch"]:
        out["batch"] = json.loads(row["batch"])
    else:
        # Absent rather than null: callers test `cohort.get("batch")` and
        # `cohort.pop("batch", None)` to mean "no job in flight".
        out.pop("batch", None)
    out["sessions"] = sessions
    return out


def _row_to_session(row: sqlite3.Row) -> dict:
    out = dict(json.loads(row["fields"] or "{}"))
    out["session_id"] = row["session_id"]
    for col in ("status", "analysis", "error"):
        if row[col] is not None:
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


def read_cohort(conn: sqlite3.Connection, pool_key: str, cohort_id: str) -> Optional[dict]:
    # Reads take the lock too: a cursor on a shared connection interleaves with
    # a concurrent writer's, and a cohort read straddling a membership rewrite
    # would see a torn view. Holding it across both statements makes the cohort
    # row and its sessions one consistent snapshot.
    with _tx_lock(conn):
        row = conn.execute(
            "SELECT * FROM cohorts WHERE pool = ? AND id = ?", (pool_key, cohort_id)
        ).fetchone()
        if row is None:
            return None
        return _row_to_cohort(row, _read_sessions(conn, pool_key, cohort_id))


def _read_sessions(conn: sqlite3.Connection, pool_key: str, cohort_id: str) -> list:
    with _tx_lock(conn):
        rows = conn.execute(
            """
            SELECT * FROM cohort_sessions
            WHERE pool = ? AND cohort_id = ?
            ORDER BY position
            """,
            (pool_key, cohort_id),
        ).fetchall()
    return [_row_to_session(r) for r in rows]


def write_cohort(conn: sqlite3.Connection, pool_key: str, cohort: dict) -> dict:
    """Upsert a cohort and its membership. Returns the stored dict.

    Sessions are replaced wholesale because callers hand back a full list, but
    the delete/insert runs in one transaction, so a reader never observes a
    cohort with no members — the old failure where a half-written file read as
    an empty or missing cohort.
    """
    cols, fields, sessions = _split_cohort(cohort)
    updated = int(time.time() * 1000)
    with _Tx(conn):
        conn.execute(
            """
            INSERT INTO cohorts (pool, id, label, status, created_at, updated_at, batch, fields)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (pool, id) DO UPDATE SET
                label = excluded.label,
                status = excluded.status,
                created_at = CASE WHEN excluded.created_at > 0
                                  THEN excluded.created_at ELSE cohorts.created_at END,
                updated_at = excluded.updated_at,
                batch = excluded.batch,
                fields = excluded.fields
            """,
            (
                pool_key,
                cols["id"],
                cols["label"],
                cols["status"],
                cols["created_at"],
                updated,
                cols["batch"],
                json.dumps(fields),
            ),
        )
        conn.execute(
            "DELETE FROM cohort_sessions WHERE pool = ? AND cohort_id = ?",
            (pool_key, cols["id"]),
        )
        conn.executemany(
            """
            INSERT INTO cohort_sessions
                (pool, cohort_id, session_id, position, status, analysis, error, fields)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (pool_key, cols["id"], sid, i, status, analysis, error, json.dumps(extra))
                for i, (sid, status, analysis, error, extra) in (
                    (i, _split_session(e)) for i, e in enumerate(sessions)
                )
            ],
        )
    return {**cohort, "updated_at": updated}


def update_session_status(
    conn: sqlite3.Connection,
    pool_key: str,
    cohort_id: str,
    session_id: str,
    *,
    status: str,
    analysis: Optional[str] = None,
    error: Optional[str] = None,
) -> bool:
    """Mark one session in one cohort.

    The point of the table: the per-session updates that ran inside
    `apply_cohort_batch` and `extract_one` loops touch one row instead of
    rewriting every other session's state alongside it.

    `fields` is left alone — the sampling tag (`filter`) belongs to the
    membership, not to this run's progress. Returns whether a row matched, so
    a mark aimed at a session that is no longer a member (deleted mid-run) is
    visible rather than silently dropped.
    """
    with _Tx(conn):
        cur = conn.execute(
            """
            UPDATE cohort_sessions
               SET status = ?, analysis = ?, error = ?
             WHERE pool = ? AND cohort_id = ? AND session_id = ?
            """,
            (status, analysis, error, pool_key, cohort_id, session_id),
        )
        matched = cur.rowcount > 0
        if matched:
            conn.execute(
                "UPDATE cohorts SET updated_at = ? WHERE pool = ? AND id = ?",
                (int(time.time() * 1000), pool_key, cohort_id),
            )
    return matched


def set_cohort_status(
    conn: sqlite3.Connection,
    pool_key: str,
    cohort_id: str,
    status: str,
    *,
    batch: Optional[dict] = None,
    clear_batch: bool = False,
) -> None:
    """Move a cohort's own status without rewriting its membership."""
    with _Tx(conn):
        if clear_batch:
            conn.execute(
                "UPDATE cohorts SET status = ?, batch = NULL, updated_at = ? WHERE pool = ? AND id = ?",
                (status, int(time.time() * 1000), pool_key, cohort_id),
            )
        elif batch is not None:
            conn.execute(
                "UPDATE cohorts SET status = ?, batch = ?, updated_at = ? WHERE pool = ? AND id = ?",
                (status, json.dumps(batch), int(time.time() * 1000), pool_key, cohort_id),
            )
        else:
            conn.execute(
                "UPDATE cohorts SET status = ?, updated_at = ? WHERE pool = ? AND id = ?",
                (status, int(time.time() * 1000), pool_key, cohort_id),
            )


def list_cohorts(conn: sqlite3.Connection, pool_key: str) -> list:
    """Every cohort in this pool, newest first, with membership."""
    with _tx_lock(conn):
        rows = conn.execute(
            "SELECT * FROM cohorts WHERE pool = ? ORDER BY created_at DESC, id DESC",
            (pool_key,),
        ).fetchall()
        return [_row_to_cohort(r, _read_sessions(conn, pool_key, r["id"])) for r in rows]


def resumable_cohorts(conn: sqlite3.Connection) -> list:
    """(pool_key, cohort) for every cohort with an in-flight batch job name.

    Index lookup over every pool at once — the old version walked every pool
    directory and parsed every cohort file on the server's startup path.
    """
    with _tx_lock(conn):
        rows = conn.execute(
            "SELECT * FROM cohorts WHERE status = 'running' AND batch IS NOT NULL"
        ).fetchall()
        out = []
        for row in rows:
            if not (json.loads(row["batch"]) or {}).get("name"):
                continue
            out.append(
                (row["pool"], _row_to_cohort(row, _read_sessions(conn, row["pool"], row["id"])))
            )
    return out


def delete_cohort(conn: sqlite3.Connection, pool_key: str, cohort_id: str) -> bool:
    with _Tx(conn):
        cur = conn.execute(
            "DELETE FROM cohorts WHERE pool = ? AND id = ?", (pool_key, cohort_id)
        )
        found = cur.rowcount > 0
        conn.execute(
            "DELETE FROM cohort_sessions WHERE pool = ? AND cohort_id = ?",
            (pool_key, cohort_id),
        )
        conn.execute(
            "DELETE FROM cohort_cuts WHERE pool = ? AND cohort_id = ?",
            (pool_key, cohort_id),
        )
    return found


def forget_session(conn: sqlite3.Connection, pool_key: str, session_id: str) -> int:
    """Drop a deleted session from every cohort that held it.

    A cohort member is a reference to a recording, not a copy of it: the
    write-up and frames live on disk under the session directory. Once that is
    gone the row cannot be substantiated, and leaving it behind is worse than
    dropping it — `enrich_cohort_status` falls through to the stored status
    when it finds nothing on disk, so a deleted session keeps being counted as
    a completed analysis and the cohort reports a result it cannot back up.

    Returns the number of memberships removed. Cohorts shrink rather than
    error: a frozen sample whose evidence was deleted is smaller, not broken.
    Cached cuts are deliberately left alone — they are a record of what the
    model said when it ran, and rewriting them to match would be a second
    fabrication. They carry their own citation map (see `cut_key`).
    """
    with _Tx(conn):
        rows = conn.execute(
            "SELECT DISTINCT cohort_id FROM cohort_sessions WHERE pool = ? AND session_id = ?",
            (pool_key, session_id),
        ).fetchall()
        if not rows:
            return 0
        cur = conn.execute(
            "DELETE FROM cohort_sessions WHERE pool = ? AND session_id = ?",
            (pool_key, session_id),
        )
        removed = cur.rowcount
        # Bump the owners so a poller sees the membership change.
        conn.executemany(
            "UPDATE cohorts SET updated_at = ? WHERE pool = ? AND id = ?",
            [(int(time.time() * 1000), pool_key, r["cohort_id"]) for r in rows],
        )
    return removed


def forget_pool(conn: sqlite3.Connection, pool_key: str) -> int:
    """Drop every cohort in a pool. For index_forget's whole-pool reset."""
    with _Tx(conn):
        cur = conn.execute("DELETE FROM cohort_sessions WHERE pool = ?", (pool_key,))
        removed = cur.rowcount
        conn.execute("DELETE FROM cohort_cuts WHERE pool = ?", (pool_key,))
        conn.execute("DELETE FROM cohorts WHERE pool = ?", (pool_key,))
    return removed


def read_cuts(conn: sqlite3.Connection, pool_key: str, cohort_id: str) -> dict:
    with _tx_lock(conn):
        rows = conn.execute(
            "SELECT key, cut FROM cohort_cuts WHERE pool = ? AND cohort_id = ?",
            (pool_key, cohort_id),
        ).fetchall()
    out = {}
    for row in rows:
        try:
            value = json.loads(row["cut"])
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            out[row["key"]] = value
    return out


def read_cut(conn: sqlite3.Connection, pool_key: str, cohort_id: str, key: str) -> Optional[dict]:
    with _tx_lock(conn):
        row = conn.execute(
            "SELECT cut FROM cohort_cuts WHERE pool = ? AND cohort_id = ? AND key = ?",
            (pool_key, cohort_id, key),
        ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["cut"])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_cut(conn: sqlite3.Connection, pool_key: str, cohort_id: str, key: str, cut: dict) -> dict:
    """Cache one cut. One row, so concurrent cuts of one cohort cannot drop
    each other the way the read-mutate-write of the whole map did."""
    with _Tx(conn):
        conn.execute(
            """
            INSERT INTO cohort_cuts (pool, cohort_id, key, cut) VALUES (?, ?, ?, ?)
            ON CONFLICT (pool, cohort_id, key) DO UPDATE SET cut = excluded.cut
            """,
            (pool_key, cohort_id, key, json.dumps(cut)),
        )
    return cut


def clear_cut(conn: sqlite3.Connection, pool_key: str, cohort_id: str, key: str) -> bool:
    with _Tx(conn):
        cur = conn.execute(
            "DELETE FROM cohort_cuts WHERE pool = ? AND cohort_id = ? AND key = ?",
            (pool_key, cohort_id, key),
        )
    return cur.rowcount > 0


def migrate_json_cohorts(
    conn: sqlite3.Connection, pool_key: str, cohorts_dir: Path, cuts_suffix: str
) -> int:
    """Import cohorts/*.json and *.cuts.json, then delete them.

    Follows `migrate_legacy_cohort`'s precedent: migrate once, leave nothing
    behind that a later reader could mistake for live state. A cohort already
    in the table wins — a re-run must not roll back newer state from a file
    that was never cleaned up.
    """
    directory = Path(cohorts_dir)
    if not directory.is_dir():
        return 0

    # One transaction for the whole import, so two servers starting together
    # cannot both read "not present" for the same cohort and double-write it.
    # The nested write_cohort / write_cut calls join this transaction rather
    # than opening their own.
    with _Tx(conn):
        moved = _import_dir(conn, pool_key, directory, cuts_suffix)

    # Leave nothing that reads as live state, and let callers treat a missing
    # directory as "nothing left to import". Non-empty (an unreadable file we
    # deliberately kept) means rmdir fails, which is the correct outcome.
    try:
        directory.rmdir()
    except OSError:
        pass
    return moved


def _import_dir(
    conn: sqlite3.Connection, pool_key: str, directory: Path, cuts_suffix: str
) -> int:
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
        exists = conn.execute(
            "SELECT 1 FROM cohorts WHERE pool = ? AND id = ?", (pool_key, cohort_id)
        ).fetchone()
        if not exists:
            # `progress` is a tally of `sessions` that older writers persisted;
            # it is view-only now and _split_cohort drops it.
            write_cohort(conn, pool_key, data)
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
                    if isinstance(cut, dict) and not read_cut(conn, pool_key, cohort_id, key):
                        write_cut(conn, pool_key, cohort_id, key, cut)
                cuts_file.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    return moved
