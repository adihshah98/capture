"""Session index queries, project-scoped by row level security.

Replaces the SQLite index.db. Every function opens a connection bound to one
project, so a missing WHERE clause returns nothing rather than another
customer's recordings.

Class columns (`outcome`, `device_class`, `page`, utm_*, `is_meta`, …) are
written here via `capture.classify` so sampling can filter in SQL without
re-deriving labels.
"""

import json

from . import classify, db
SESSION_COLUMNS = """
    s.session_id, s.first_seen, s.last_seen, s.last_active, s.duration, s.windows,
    s.chunk_count, s.bytes, s.clicks, s.rage, s.visitor_id, s.url, s.device, s.attribution,
    o.checkout_started_at, o.purchased_at, o.order_id, o.total, o.currency
"""

FROM_SESSIONS = """
    FROM sessions s
    LEFT JOIN orders o ON o.project_id = s.project_id AND o.session_id = s.session_id
"""

FILTERS = {
    "checkout": "o.session_id IS NOT NULL",
    "purchased": "o.purchased_at IS NOT NULL",
    "rage": "s.rage > 0",
}


def _order(row: tuple) -> dict | None:
    started, purchased, order_id, total, currency = row
    order = {}
    if started is not None:
        order["checkout_started_at"] = started
    if purchased is not None:
        order["purchased_at"] = purchased
    if order_id:
        order["order_id"] = order_id
    if total is not None:
        order["total"] = total
    if currency:
        order["currency"] = currency
    return order or None


def row_to_info(row: tuple) -> dict:
    (
        sid,
        first,
        last,
        active,
        duration,
        windows,
        count,
        size,
        clicks,
        rage,
        visitor,
        url,
        device,
        attribution,
        *order_cols,
    ) = row
    info = {
        "session_id": sid,
        "first_seen": first,
        "last_seen": last,
        "last_active": active or 0,
        "duration": duration,
        "windows": windows or 1,
        "chunk_count": count,
        "bytes": size,
    }
    if visitor:
        info["visitor_id"] = visitor
    if url:
        info["url"] = url
    if clicks or rage:
        info["interactions"] = {"clicks": clicks, "rage": rage}
    if device:
        info["device"] = device
    if attribution:
        info["attribution"] = attribution
    order = _order(tuple(order_cols))
    if order:
        info["order"] = order
    return info


# The clamped span of the biggest window, matching server.window_duration.
PRIMARY_WINDOW_SPAN = """
    SELECT greatest(
             least(last_seen,
                   CASE WHEN last_active > 0 THEN last_active + 1500 ELSE last_seen END)
             - first_seen, 0)
    FROM session_windows
    WHERE project_id = %s AND session_id = %s
    ORDER BY bytes DESC, window_id ASC
    LIMIT 1
"""


def window_state(project_id, session_id: str, window_id: str) -> dict:
    """The scanner's carried state for one window — what window-state.json held."""
    with db.connection(project_id) as conn:
        row = conn.execute(
            "SELECT first_seen, last_seen, last_active, bytes, nav, href FROM session_windows"
            " WHERE session_id = %s AND window_id = %s",
            (session_id, str(window_id)),
        ).fetchone()
    if not row:
        return {}
    first, last, active, size, nav, href = row
    state = {"first": first, "last": last, "bytes": size}
    if active:
        state["active"] = active
    if nav is not None:
        state["nav"] = nav
    if href is not None:
        state["href"] = href
    return state


def record_chunk(
    project_id,
    session_id,
    *,
    window_id,
    first_ts,
    last_ts,
    active_ts,
    size,
    meta,
    clicks,
    rage,
    device,
    attribution,
    state,
) -> None:
    """One ingested batch. Counters accumulate in SQL, so concurrent batches for
    the same session cannot lose an increment the way a read-modify-write file could.

    Class columns follow the same first-write-wins coalesce as url/device/
    attribution — the entry batch freezes how the session is sliced forever.
    """
    url = meta.get("url")
    classes = classify.session_classes(url=url, device=device, attribution=attribution)
    with db.connection(project_id) as conn:
        conn.execute(
            """
            INSERT INTO sessions
                (project_id, session_id, first_seen, last_seen, last_active,
                 windows, chunk_count, bytes, clicks, rage, visitor_id, url, device, attribution,
                 device_class, page, utm_source, utm_medium, utm_campaign, utm_content,
                 has_fbclid, is_meta)
            VALUES (%s, %s, %s, %s, %s, 1, 1, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, session_id) DO UPDATE SET
                first_seen  = least(sessions.first_seen, excluded.first_seen),
                last_seen   = greatest(sessions.last_seen, excluded.last_seen),
                last_active = greatest(sessions.last_active, excluded.last_active),
                chunk_count = sessions.chunk_count + 1,
                bytes       = sessions.bytes + excluded.bytes,
                clicks      = sessions.clicks + excluded.clicks,
                rage        = sessions.rage + excluded.rage,
                visitor_id  = coalesce(sessions.visitor_id, excluded.visitor_id),
                url         = coalesce(sessions.url, excluded.url),
                device      = coalesce(sessions.device, excluded.device),
                attribution = coalesce(sessions.attribution, excluded.attribution),
                device_class = coalesce(sessions.device_class, excluded.device_class),
                page         = coalesce(sessions.page, excluded.page),
                utm_source   = coalesce(sessions.utm_source, excluded.utm_source),
                utm_medium   = coalesce(sessions.utm_medium, excluded.utm_medium),
                utm_campaign = coalesce(sessions.utm_campaign, excluded.utm_campaign),
                utm_content  = coalesce(sessions.utm_content, excluded.utm_content),
                -- Booleans cannot coalesce like nullable text: freeze with the
                -- first attribution write (NULL attribution leaves the door open).
                has_fbclid   = CASE
                    WHEN sessions.attribution IS NOT NULL THEN sessions.has_fbclid
                    ELSE excluded.has_fbclid
                END,
                is_meta      = CASE
                    WHEN sessions.attribution IS NOT NULL THEN sessions.is_meta
                    ELSE excluded.is_meta
                END
            """,
            (
                project_id,
                session_id,
                first_ts,
                last_ts,
                active_ts or 0,
                size,
                clicks,
                rage,
                meta.get("visitor_id"),
                url,
                json.dumps(device) if device else None,
                json.dumps(attribution) if attribution else None,
                classes["device_class"],
                classes["page"],
                classes["utm_source"],
                classes["utm_medium"],
                classes["utm_campaign"],
                classes["utm_content"],
                classes["has_fbclid"],
                classes["is_meta"],
            ),
        )
        conn.execute(
            """
            INSERT INTO session_windows
                (project_id, session_id, window_id, first_seen, last_seen, last_active, bytes, nav, href)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, session_id, window_id) DO UPDATE SET
                first_seen  = least(session_windows.first_seen, excluded.first_seen),
                last_seen   = greatest(session_windows.last_seen, excluded.last_seen),
                last_active = greatest(session_windows.last_active, excluded.last_active),
                bytes       = session_windows.bytes + excluded.bytes,
                nav         = coalesce(excluded.nav, session_windows.nav),
                href        = coalesce(excluded.href, session_windows.href)
            """,
            (
                project_id,
                session_id,
                str(window_id),
                int(state.get("first", first_ts)),
                int(state.get("last", last_ts)),
                int(state.get("active") or 0),
                size,
                int(state["nav"]) if isinstance(state.get("nav"), (int, float)) else None,
                state.get("href") if isinstance(state.get("href"), str) else None,
            ),
        )
        conn.execute(
            f"""
            UPDATE sessions SET
                windows  = (SELECT count(*) FROM session_windows
                             WHERE project_id = %s AND session_id = %s),
                duration = ({PRIMARY_WINDOW_SPAN})
            WHERE session_id = %s
            """,
            (project_id, session_id, project_id, session_id, session_id),
        )
        conn.execute(
            "UPDATE projects SET bytes_used = bytes_used + %s WHERE id = %s", (size, project_id)
        )


def record_order(project_id, session_id, event: str, payload: dict, now: int) -> dict | None:
    """Returns the order as stored, or None when the session is unknown.

    Also refreshes `sessions.outcome` from the stored timestamps so sampling
    can filter purchases without joining orders at query time.
    """
    with db.connection(project_id) as conn:
        known = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = %s", (session_id,)
        ).fetchone()
        if not known:
            return None
        if event == "checkout_started":
            conn.execute(
                "INSERT INTO orders (project_id, session_id, checkout_started_at)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (project_id, session_id) DO UPDATE SET"
                " checkout_started_at = coalesce(orders.checkout_started_at, excluded.checkout_started_at)",
                (project_id, session_id, now),
            )
        else:
            total = payload.get("total")
            conn.execute(
                """
                INSERT INTO orders (project_id, session_id, purchased_at, order_id, total, currency)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, session_id) DO UPDATE SET
                    purchased_at = excluded.purchased_at,
                    order_id = coalesce(excluded.order_id, orders.order_id),
                    total = coalesce(excluded.total, orders.total),
                    currency = coalesce(excluded.currency, orders.currency)
                """,
                (
                    project_id,
                    session_id,
                    now,
                    payload["order_id"][:128] if isinstance(payload.get("order_id"), str) else None,
                    total
                    if isinstance(total, (int, float))
                    and not isinstance(total, bool)
                    and -1e15 < total < 1e15
                    else None,
                    payload["currency"][:8] if isinstance(payload.get("currency"), str) else None,
                ),
            )
        row = conn.execute(
            "SELECT checkout_started_at, purchased_at, order_id, total, currency"
            " FROM orders WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        order = _order(row) if row else None
        conn.execute(
            "UPDATE sessions SET outcome = %s WHERE session_id = %s",
            (classify.outcome_class(order), session_id),
        )
    return order


def get(project_id, session_id: str) -> dict | None:
    with db.connection(project_id) as conn:
        row = conn.execute(
            f"SELECT {SESSION_COLUMNS} {FROM_SESSIONS} WHERE s.session_id = %s", (session_id,)
        ).fetchone()
    return row_to_info(row) if row else None


def listing(
    project_id,
    *,
    limit: int,
    cursor: str | None = None,
    filter: str | None = None,
    q: str | None = None,
) -> dict:
    where: list[str] = ["TRUE"]
    params: list = []
    if filter in FILTERS:
        where.append(FILTERS[filter])
    if q:
        like = f"%{q}%"
        where.append("(s.session_id ILIKE %s OR s.visitor_id ILIKE %s OR s.url ILIKE %s)")
        params += [like, like, like]

    with db.connection(project_id) as conn:
        total = conn.execute(
            f"SELECT count(*) {FROM_SESSIONS} WHERE {' AND '.join(where)}", params
        ).fetchone()[0]
        page_where, page_params = list(where), list(params)
        if cursor:
            seen, _, sid = cursor.partition(":")
            if not seen.isdigit():
                raise ValueError("bad cursor")
            page_where.append("(s.last_seen < %s OR (s.last_seen = %s AND s.session_id < %s))")
            page_params += [int(seen), int(seen), sid]
        rows = conn.execute(
            f"SELECT {SESSION_COLUMNS} {FROM_SESSIONS} WHERE {' AND '.join(page_where)}"
            " ORDER BY s.last_seen DESC, s.session_id DESC LIMIT %s",
            [*page_params, limit + 1],
        ).fetchall()

    more = len(rows) > limit
    sessions = [row_to_info(r) for r in rows[:limit]]
    next_cursor = (
        f"{sessions[-1]['last_seen']}:{sessions[-1]['session_id']}" if more and sessions else None
    )
    return {"sessions": sessions, "total": total, "cursor": next_cursor}


def forget(project_id, session_id: str) -> None:
    with db.connection(project_id) as conn:
        # Windows and orders cascade from the session row. The analysis and its
        # job status have no foreign key — see migrations/0004 — so they go here,
        # where the deletion is visible.
        conn.execute("DELETE FROM analyses WHERE session_id = %s", (session_id,))
        conn.execute("DELETE FROM analysis_jobs WHERE session_id = %s", (session_id,))
        conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))


def expired(project_id, cutoff_ms: int) -> list:
    with db.connection(project_id) as conn:
        rows = conn.execute(
            "SELECT session_id FROM sessions WHERE last_seen < %s", (int(cutoff_ms),)
        ).fetchall()
    return [r[0] for r in rows]


def project_bytes(project_id) -> int:
    with db.connection(project_id) as conn:
        return int(conn.execute("SELECT coalesce(sum(bytes), 0) FROM sessions").fetchone()[0])
