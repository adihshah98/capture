"""Choosing a cohort's members, and the facets offered when choosing them.

The one part of the package that talks SQL. Everything here takes a connection
and reads the session index; nothing writes.

Two things are worth knowing before editing.

**The connection is not SQLite.** The SQL uses `?` placeholders and
`json_extract` because this code predates the move to Postgres. What actually
arrives is a `compat.SqliteStyleConnection` wrapping psycopg, reading a
`compat.sessions` view that flattens the normalised schema back into the old
shape. See migrations/0002_compat.sql -- and note that the view's
`security_invoker` clause is what keeps row-level security applied, so a
caller-supplied `pool` cannot read another tenant's rows.

The signatures used to say `sqlite3.Connection`, which was true only on the
deleted CLI path. They now say `spec.Connection`, a Protocol naming the one
method these functions call.

**Selective predicates run last.** The SQL applies the cheap filters -- pool,
duration, URL substring, time bounds -- then `ORDER BY` and `LIMIT`; the slice
itself (outcome, device, page, utm_*) is evaluated in Python afterwards, by
`row_matches_slice`. A rare slice can therefore fall outside the candidate
window and return fewer sessions than asked for, with no signal. That shortfall
is a known defect, tracked in docs/analyze-bug-review.md, and this module is
where it will be fixed.
"""

import json
from typing import List, Optional, Tuple

from . import spec as cohort_spec
from .spec import CohortSpec, Connection
from .slices import (
    DEVICE_VALUES,
    OUTCOME_LABELS,
    OUTCOME_VALUES,
    device_class,
    normalize_slice,
    outcome_class,
    page_path,
    row_matches_slice,
)

def _parse_index_json(raw) -> Optional[dict]:
    """A JSON-text index column as a dict, or None when it is not one.

    `except Exception` is deliberately wide -- the column is text and a malformed
    row must not fail a whole cohort -- but note what that costs: this function
    turns *any* error into "no data", so a caller whose rows all come back
    unclassified has a bug here, not an empty database. Extracting this module
    without carrying `import json` did exactly that, and every session read as a
    non-purchase with no device until the tests caught it.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def index_row_facts(
    url,
    order_raw,
    device_raw,
    attribution_raw,
) -> dict:
    """Slice fields for one index row — same vocabulary as cut matching."""
    order = _parse_index_json(order_raw)
    device = _parse_index_json(device_raw)
    attribution = _parse_index_json(attribution_raw) or {}
    return {
        "outcome": outcome_class(order),
        "device": device_class(device),
        "page": page_path(url),
        "attribution": attribution,
        "bought": bool(order and order.get("purchased_at")),
    }


def _facet_counts_listed(counts: dict, field: str, order=None) -> list:
    items = counts[field]
    keys = order if order else sorted(items, key=lambda k: (-items[k], k))
    return [
        {
            "value": k,
            "count": items[k],
            "label": OUTCOME_LABELS.get(k, k) if field == "outcome" else k,
        }
        for k in keys
        if items.get(k)
    ]


def facets_from_rows(rows: list) -> dict:
    """Build the cut-picker facet payload from slice-shaped rows."""
    counts = {
        "outcome": {},
        "device": {},
        "page": {},
        "utm_source": {},
        "utm_campaign": {},
    }
    for row in rows:
        outcome = row.get("outcome") or "rest"
        counts["outcome"][outcome] = counts["outcome"].get(outcome, 0) + 1
        if row.get("device"):
            counts["device"][row["device"]] = counts["device"].get(row["device"], 0) + 1
        if row.get("page"):
            counts["page"][row["page"]] = counts["page"].get(row["page"], 0) + 1
        attrs = row.get("attribution") or {}
        for key in ("utm_source", "utm_campaign"):
            value = str(attrs.get(key) or "").strip().lower()
            if value:
                counts[key][value] = counts[key].get(value, 0) + 1

    return {
        "n": len(rows),
        "outcome": _facet_counts_listed(counts, "outcome", order=OUTCOME_VALUES),
        "device": _facet_counts_listed(counts, "device", order=DEVICE_VALUES),
        "page": _facet_counts_listed(counts, "page"),
        "utm_source": _facet_counts_listed(counts, "utm_source"),
        "utm_campaign": _facet_counts_listed(counts, "utm_campaign"),
    }


def pool_facets(
    conn: Connection,
    pool: str,
    *,
    min_duration_ms: int = 0,
) -> dict:
    """Facet values present in the pool index (for the New cohort dialog)."""
    where_sql, params = _cohort_where(pool, min_duration_ms=min_duration_ms, url_contains="")
    rows = conn.execute(
        f"""
        SELECT url, "order", device, attribution
        FROM sessions
        WHERE {where_sql}
        """,
        params,
    ).fetchall()
    facts = [index_row_facts(url, order, device, attr) for url, order, device, attr in rows]
    return facets_from_rows(facts)


def _cohort_where(
    pool: str,
    *,
    min_duration_ms: int,
    url_contains: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Tuple[str, List]:
    """Shared WHERE clause for cohort sampling.

    Time bounds use `last_seen` so a window means “sessions that were active
    (or finished) in this period,” matching the recency sort key.
    """
    where = [
        "pool = ?",
        "chunk_count > 0",
        "(last_seen - first_seen) >= ?",
    ]
    params: List = [pool, int(min_duration_ms)]
    needle = (url_contains or "").strip()
    if needle:
        where.append("url LIKE ?")
        params.append(f"%{needle}%")
    if start_ms is not None:
        where.append("last_seen >= ?")
        params.append(int(start_ms))
    if end_ms is not None:
        where.append("last_seen <= ?")
        params.append(int(end_ms))
    return " AND ".join(where), params


def _cohort_order_sql(order: str) -> str:
    activity = "coalesce(json_extract(interactions, '$.clicks'), 0)"
    if order == cohort_spec.ORDER_EARLIEST:
        return f"last_seen ASC, {activity} DESC, session_id ASC"
    return f"last_seen DESC, {activity} DESC, session_id DESC"


def _sample_candidates(
    conn: Connection,
    pool: str,
    *,
    min_duration_ms: int,
    url_contains: str,
    limit: int,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = cohort_spec.ORDER_LATEST,
) -> list:
    """Index rows (with slice facts) that clear the base duration/path/time gates."""
    where_sql, base_params = _cohort_where(
        pool,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    order_sql = _cohort_order_sql(order if order in cohort_spec.ORDERS else cohort_spec.ORDER_LATEST)
    rows = conn.execute(
        f"""
        SELECT session_id, url, "order", device, attribution
        FROM sessions
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ?
        """,
        (*base_params, max(0, int(limit))),
    ).fetchall()
    out = []
    for sid, url, order_raw, device, attr in rows:
        fact = index_row_facts(url, order_raw, device, attr)
        fact["session_id"] = sid
        out.append(fact)
    return out


def _sample_matching_rows(
    conn: Connection,
    pool: str,
    n: int,
    *,
    min_duration_ms: int,
    url_contains: str,
    slice_spec: Optional[dict],
    exclude_ids: Optional[set] = None,
    tag: str = "a",
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = cohort_spec.ORDER_LATEST,
) -> list:
    """Sessions matching a cut-style slice, tagged for the sample arm."""
    if n <= 0:
        return []
    spec = normalize_slice(slice_spec)
    exclude = exclude_ids or set()
    candidate_limit = min(
        cohort_spec.CANDIDATE_CAP,
        max(n * 20, n + len(exclude) + 200, 500),
    )
    path_needle = "" if spec.get("page") else url_contains
    candidates = _sample_candidates(
        conn,
        pool,
        min_duration_ms=min_duration_ms,
        url_contains=path_needle,
        limit=candidate_limit,
        start_ms=start_ms,
        end_ms=end_ms,
        order=order,
    )
    out = []
    for row in candidates:
        sid = row["session_id"]
        if sid in exclude:
            continue
        if not row_matches_slice(row, spec):
            continue
        out.append({"session_id": sid, "filter": tag, "status": "queued"})
        if len(out) >= n:
            break
    return out


def sample_cohort_rows(conn: Connection, pool: str, spec: CohortSpec) -> list:
    """Sessions matching a selection rule, in the mode the spec asks for.

    `spec.order` is latest (default) or earliest by `last_seen`; `start_ms` /
    `end_ms` bound that same clock. Sessions with more clicks win a tie.
    """
    spec = spec.normalised()
    base = dict(
        min_duration_ms=spec.min_duration_ms,
        url_contains=spec.url_contains,
        start_ms=spec.start_ms,
        end_ms=spec.end_ms,
        order=spec.order,
    )

    if spec.uses_groups:
        sessions = _sample_matching_rows(
            conn, pool, spec.a_n, slice_spec=spec.slice_spec, tag="a", **base
        )
        if spec.b_n > 0:
            sessions.extend(
                _sample_matching_rows(
                    conn,
                    pool,
                    spec.b_n,
                    # An unset B slice means "same filter as A", not "no filter".
                    slice_spec=spec.slice_b or spec.slice_spec,
                    exclude_ids={s["session_id"] for s in sessions},
                    tag="b",
                    **base,
                )
            )
        return sessions

    filter_spec = dict(normalize_slice(spec.slice_spec))
    if spec.any_n <= 0:
        # The purchased/all arms own the outcome split, so a slice must not
        # constrain it as well.
        filter_spec.pop("outcome", None)

    needed = spec.total
    candidate_limit = min(
        cohort_spec.CANDIDATE_CAP,
        max(needed * 20, needed + 200, 500),
    )
    candidates = _sample_candidates(
        conn,
        pool,
        min_duration_ms=spec.min_duration_ms,
        # An explicit page filter supersedes the URL substring.
        url_contains="" if filter_spec.get("page") else spec.url_contains,
        limit=candidate_limit,
        start_ms=spec.start_ms,
        end_ms=spec.end_ms,
        order=spec.order,
    )
    matched = [row for row in candidates if row_matches_slice(row, filter_spec)]

    def _entry(row: dict) -> dict:
        return {
            "session_id": row["session_id"],
            "filter": "purchased" if row.get("bought") else "all",
            "status": "queued",
        }

    if spec.any_n > 0:
        return [_entry(row) for row in matched[: spec.any_n]]

    purchased = []
    non_purchase = []
    for row in matched:
        if row.get("bought"):
            if len(purchased) < spec.purchased_n:
                purchased.append(_entry(row))
        elif len(non_purchase) < spec.all_n:
            non_purchase.append(_entry(row))
        if len(purchased) >= spec.purchased_n and len(non_purchase) >= spec.all_n:
            break
    return purchased + non_purchase

