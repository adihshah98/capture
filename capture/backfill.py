"""Walk the on-disk pools into Postgres.

Idempotent and re-runnable: every write is an upsert keyed on
(project_id, session_id), so an interrupted run resumes by being run again and a
completed run is a no-op. Chunks stay on disk — this moves the index only.
"""

import json
import sqlite3
import sys
from pathlib import Path

from . import db, paths, server

DEFAULT_PROJECT_NAME = "Default"


def legacy_pools() -> list:
    """The on-disk layout as it was before Postgres. Deliberately independent of
    projects.py, which now reads the projects it is this script's job to create."""
    pools = [paths.DATA_DIR]
    if paths.PROJECTS_DIR.is_dir():
        for entry in sorted(paths.PROJECTS_DIR.iterdir()):
            if (entry / "sessions").is_dir():
                pools.append(entry / "sessions")
    return pools


def legacy_projects_file() -> dict:
    try:
        data = json.loads(paths.PROJECTS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise SystemExit(f"projects.json is unreadable, refusing to guess: {exc}") from exc


def _json(value):
    return json.dumps(value) if value else None


def ensure_project(
    conn, *, name: str, token: str | None = None, meta: dict | None = None, is_default: bool = False
) -> str:
    """Return the project id for a pool, creating it and its token on first run."""
    meta = meta or {}
    if token:
        row = conn.execute(
            "SELECT project_id FROM project_tokens WHERE token = %s", (token,)
        ).fetchone()
        if row:
            return row[0]
    if is_default:
        row = conn.execute("SELECT id FROM projects WHERE is_default").fetchone()
        if row:
            return row[0]

    # storage_prefix locates this project's chunks: "" is the legacy
    # data/sessions pool, otherwise data/projects/<prefix>/sessions. Phase 3
    # reuses it verbatim as the S3 key prefix.
    project_id = conn.execute(
        "INSERT INTO projects (name, org_id, domains, is_default, storage_prefix)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (
            meta.get("name") or name,
            meta.get("org_id"),
            json.dumps(meta.get("domains") or []),
            is_default,
            "" if is_default else (token or ""),
        ),
    ).fetchone()[0]
    if token:
        conn.execute(
            "INSERT INTO project_tokens (token, project_id) VALUES (%s, %s)"
            " ON CONFLICT (token) DO NOTHING",
            (token, project_id),
        )
    return project_id


def upsert_session(conn, project_id: str, info: dict) -> None:
    interactions = info.get("interactions") or {}
    conn.execute(
        """
        INSERT INTO sessions
            (project_id, session_id, first_seen, last_seen, last_active, duration,
             windows, chunk_count, bytes, clicks, rage, visitor_id, url, device, attribution)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (project_id, session_id) DO UPDATE SET
            first_seen  = least(sessions.first_seen, excluded.first_seen),
            last_seen   = greatest(sessions.last_seen, excluded.last_seen),
            last_active = greatest(sessions.last_active, excluded.last_active),
            duration    = coalesce(excluded.duration, sessions.duration),
            windows     = greatest(sessions.windows, excluded.windows),
            chunk_count = excluded.chunk_count,
            bytes       = excluded.bytes,
            clicks      = excluded.clicks,
            rage        = excluded.rage,
            visitor_id  = coalesce(excluded.visitor_id, sessions.visitor_id),
            url         = coalesce(excluded.url, sessions.url),
            device      = coalesce(excluded.device, sessions.device),
            attribution = coalesce(excluded.attribution, sessions.attribution)
        """,
        (
            project_id,
            info["session_id"],
            info["first_seen"],
            info["last_seen"],
            info.get("last_active") or 0,
            info.get("duration"),
            info.get("windows") or 1,
            info.get("chunk_count") or 0,
            info.get("bytes") or 0,
            int(interactions.get("clicks") or 0),
            int(interactions.get("rage") or 0),
            info.get("visitor_id"),
            info.get("url"),
            _json(info.get("device")),
            _json(info.get("attribution")),
        ),
    )

    order = info.get("order") or {}
    if order:
        conn.execute(
            """
            INSERT INTO orders
                (project_id, session_id, checkout_started_at, purchased_at, order_id, total, currency)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, session_id) DO UPDATE SET
                checkout_started_at = coalesce(orders.checkout_started_at, excluded.checkout_started_at),
                purchased_at = coalesce(excluded.purchased_at, orders.purchased_at),
                order_id = coalesce(excluded.order_id, orders.order_id),
                total = coalesce(excluded.total, orders.total),
                currency = coalesce(excluded.currency, orders.currency)
            """,
            (
                project_id,
                info["session_id"],
                order.get("checkout_started_at"),
                order.get("purchased_at"),
                order.get("order_id"),
                order.get("total"),
                order.get("currency"),
            ),
        )


def upsert_windows(conn, project_id: str, session_id: str, states: dict) -> None:
    for window_id, state in states.items():
        if not isinstance(state, dict) or state.get("first") is None:
            continue
        conn.execute(
            """
            INSERT INTO session_windows
                (project_id, session_id, window_id, first_seen, last_seen, last_active, bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, session_id, window_id) DO UPDATE SET
                first_seen  = least(session_windows.first_seen, excluded.first_seen),
                last_seen   = greatest(session_windows.last_seen, excluded.last_seen),
                last_active = greatest(session_windows.last_active, excluded.last_active),
                bytes       = excluded.bytes
            """,
            (
                project_id,
                session_id,
                str(window_id),
                int(state.get("first") or 0),
                int(state.get("last") or 0),
                int(state.get("active") or 0),
                int(state.get("bytes") or 0),
            ),
        )


def read_states(session_dir: Path, parse_chunks: bool = True) -> dict:
    """Prefer the recorded state; fall back to rebuilding it from the chunks so
    sessions written before window-state.json existed still get window rows.
    The index path passes parse_chunks=False — re-reading every chunk is the
    whole cost this migration is trying to avoid."""
    try:
        states = json.loads((session_dir / server.WINDOW_STATE_FILE).read_text())
        if isinstance(states, dict) and states:
            return states
    except Exception:
        pass
    return server.session_states(session_dir)[0] if parse_chunks else {}


def _loads(raw):
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def import_from_index(conn, index, project_id: str, pool: Path, pool_key: str) -> set:
    """Move the SQLite index straight across. It already holds every column the
    new schema needs, so this costs seconds where re-deriving from the chunk
    files costs an hour of parsing at production data sizes."""
    rows = index.execute(
        "SELECT session_id, first_seen, last_seen, last_active, duration, windows,"
        ' chunk_count, bytes, visitor_id, url, "order", interactions, device, attribution'
        " FROM sessions WHERE pool = ?",
        (pool_key,),
    ).fetchall()
    if not rows:
        return set()

    sessions, orders, windows_rows, seen = [], [], [], set()
    for (
        sid,
        first,
        last,
        active,
        duration,
        window_count,
        chunks,
        size,
        visitor,
        url,
        order_raw,
        inter_raw,
        device_raw,
        attr_raw,
    ) in rows:
        seen.add(sid)
        counts = _loads(inter_raw) or {}
        sessions.append(
            (
                project_id,
                sid,
                first,
                last,
                active or 0,
                duration,
                window_count or 1,
                chunks or 0,
                size or 0,
                int(counts.get("clicks") or 0),
                int(counts.get("rage") or 0),
                visitor,
                url,
                device_raw,
                attr_raw,
            )
        )
        order = _loads(order_raw)
        if order:
            orders.append(
                (
                    project_id,
                    sid,
                    order.get("checkout_started_at"),
                    order.get("purchased_at"),
                    order.get("order_id"),
                    order.get("total"),
                    order.get("currency"),
                )
            )
        for window_id, state in read_states(pool / sid, parse_chunks=False).items():
            if not isinstance(state, dict) or state.get("first") is None:
                continue
            windows_rows.append(
                (
                    project_id,
                    sid,
                    str(window_id),
                    int(state.get("first") or 0),
                    int(state.get("last") or 0),
                    int(state.get("active") or 0),
                    int(state.get("bytes") or 0),
                    int(state["nav"]) if isinstance(state.get("nav"), (int, float)) else None,
                    state.get("href") if isinstance(state.get("href"), str) else None,
                )
            )

    conn.cursor().executemany(
        """
        INSERT INTO sessions
            (project_id, session_id, first_seen, last_seen, last_active, duration, windows,
             chunk_count, bytes, clicks, rage, visitor_id, url, device, attribution)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (project_id, session_id) DO UPDATE SET
            first_seen  = least(sessions.first_seen, excluded.first_seen),
            last_seen   = greatest(sessions.last_seen, excluded.last_seen),
            last_active = greatest(sessions.last_active, excluded.last_active),
            duration    = coalesce(excluded.duration, sessions.duration),
            windows     = greatest(sessions.windows, excluded.windows),
            chunk_count = greatest(sessions.chunk_count, excluded.chunk_count),
            bytes       = greatest(sessions.bytes, excluded.bytes),
            clicks      = greatest(sessions.clicks, excluded.clicks),
            rage        = greatest(sessions.rage, excluded.rage),
            visitor_id  = coalesce(sessions.visitor_id, excluded.visitor_id),
            url         = coalesce(sessions.url, excluded.url),
            device      = coalesce(sessions.device, excluded.device),
            attribution = coalesce(sessions.attribution, excluded.attribution)
        """,
        sessions,
    )
    if orders:
        conn.cursor().executemany(
            """
            INSERT INTO orders
                (project_id, session_id, checkout_started_at, purchased_at, order_id, total, currency)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, session_id) DO UPDATE SET
                checkout_started_at = coalesce(orders.checkout_started_at, excluded.checkout_started_at),
                purchased_at = coalesce(excluded.purchased_at, orders.purchased_at),
                order_id = coalesce(excluded.order_id, orders.order_id),
                total = coalesce(excluded.total, orders.total),
                currency = coalesce(excluded.currency, orders.currency)
            """,
            orders,
        )
    if windows_rows:
        conn.cursor().executemany(
            """
            INSERT INTO session_windows
                (project_id, session_id, window_id, first_seen, last_seen, last_active, bytes, nav, href)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, session_id, window_id) DO UPDATE SET
                first_seen  = least(session_windows.first_seen, excluded.first_seen),
                last_seen   = greatest(session_windows.last_seen, excluded.last_seen),
                last_active = greatest(session_windows.last_active, excluded.last_active),
                bytes       = greatest(session_windows.bytes, excluded.bytes),
                nav         = coalesce(excluded.nav, session_windows.nav),
                href        = coalesce(excluded.href, session_windows.href)
            """,
            windows_rows,
        )
    return seen


def backfill(verbose: bool = True) -> dict:
    projects = legacy_projects_file()
    counts = {"projects": 0, "sessions": 0, "from_index": 0, "parsed": 0, "skipped": 0}

    index_path = paths.DATA_ROOT / "index.db"
    index = None
    if index_path.is_file():
        index = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)

    try:
        with db.connection(admin=True) as conn:
            for pool in legacy_pools():
                token = None if pool == paths.DATA_DIR else pool.parent.name
                entries = [e for e in pool.iterdir() if e.is_dir()] if pool.is_dir() else []
                if not entries and token is None:
                    continue

                project_id = ensure_project(
                    conn,
                    name=DEFAULT_PROJECT_NAME if token is None else token,
                    token=token,
                    meta=projects.get(token) if token else None,
                    is_default=token is None,
                )
                counts["projects"] += 1
                label = token or DEFAULT_PROJECT_NAME

                imported = set()
                if index is not None:
                    imported = import_from_index(
                        conn, index, project_id, pool, "" if token is None else token
                    )
                    counts["from_index"] += len(imported)

                # Anything on disk the index never caught, derived the slow way.
                for entry in entries:
                    if entry.name in imported:
                        continue
                    info = server.session_info(entry)
                    if not info:
                        counts["skipped"] += 1
                        continue
                    upsert_session(conn, project_id, info)
                    upsert_windows(conn, project_id, entry.name, read_states(entry))
                    counts["parsed"] += 1

                conn.execute(
                    "UPDATE projects SET bytes_used ="
                    " (SELECT coalesce(sum(bytes), 0) FROM sessions WHERE project_id = %s)"
                    " WHERE id = %s",
                    (project_id, project_id),
                )
                if verbose:
                    print(
                        f"{label}: {len(entries)} dir(s) on disk, "
                        f"{len(imported)} from index -> {project_id}"
                    )
    finally:
        if index is not None:
            index.close()

    counts["sessions"] = counts["from_index"] + counts["parsed"]
    return counts


if __name__ == "__main__":
    if not db.configured():
        print("CAPTURE_DATABASE_URL is not set")
        sys.exit(1)
    try:
        db.migrate()
        result = backfill()
        print(
            f"backfilled {result['sessions']} session(s) across "
            f"{result['projects']} project(s); {result['skipped']} empty dir(s) skipped"
        )
    finally:
        db.close_pool()
