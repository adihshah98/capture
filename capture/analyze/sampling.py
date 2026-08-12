"""Choosing a cohort's members, and the facets offered when choosing them.

The one part of the package that talks SQL. Everything here takes a connection
and reads the session index; nothing writes.

Two things are worth knowing before editing.

**The connection is not SQLite.** The SQL uses `?` placeholders because this
code predates the move to Postgres. What actually arrives is a
`compat.SqliteStyleConnection` wrapping psycopg, reading a `compat.sessions`
view that flattens the normalised schema back into the old shape. See
migrations/0002_compat.sql -- and note that the view's `security_invoker`
clause is what keeps row-level security applied, so a caller-supplied `pool`
cannot read another tenant's rows. Activity tie-breaks sort on the plain
`clicks` column the view exposes (0005); they no longer go through a
SQLite-shaped `json_extract` shim.

The signatures used to say `sqlite3.Connection`, which was true only on the
deleted CLI path. They now say `spec.Connection`, a Protocol naming the one
method these functions call.

**Selective predicates run last.** The SQL applies the cheap filters -- pool,
duration, URL substring, time bounds -- then `ORDER BY` and `LIMIT`; the slice
itself (outcome, device, page, utm_*) is evaluated in Python afterwards, by
`row_matches_slice`. A rare slice can therefore need several pages of
candidates before it fills. Sampling paginates up to `CANDIDATE_CAP` and
records `{requested, matched, scanned, capped}` so a short cohort is never
presented as if the request was met. `capped` is true only when the scan hit
the hard cap while still short -- that is the case that scanning harder (or
pushing the slice into SQL) could still fix.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from . import spec as cohort_spec
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
from .spec import CohortSpec, Connection


@dataclass(frozen=True)
class SampleStats:
    """How a sample request resolved against the candidate window.

    `matched < requested` is a shortfall. `capped` distinguishes "the pool
    genuinely holds this many" (`capped=False`, we exhausted the index under
    the base gates) from "we stopped at CANDIDATE_CAP" (`capped=True`) -- only
    the second is fixable by scanning harder.
    """

    requested: int
    matched: int
    scanned: int
    capped: bool

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SampleResult:
    """Sessions chosen for a cohort, plus how the request resolved."""

    sessions: list
    sample: SampleStats


def _parse_index_json(raw) -> dict | None:
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
    counts: dict[str, dict[str, int]] = {
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
    """Facet values present in the pool index (for the New cohort dialog).

    Scans at most `CANDIDATE_CAP` newest rows under the duration gate — the same
    window sampling uses — then classifies in Python. An unbounded fetchall of
    every session (three json.loads each) grew with project size; pushing the
    classifiers into SQL would fork device/page semantics from the sampler.
    """
    where_sql, params = _cohort_where(pool, min_duration_ms=min_duration_ms, url_contains="")
    order_sql = _cohort_order_sql(cohort_spec.ORDER_LATEST)
    rows = conn.execute(
        f"""
        SELECT url, "order", device, attribution
        FROM sessions
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ?
        """,
        (*params, cohort_spec.CANDIDATE_CAP),
    ).fetchall()
    facts = [index_row_facts(url, order, device, attr) for url, order, device, attr in rows]
    return facets_from_rows(facts)


def _cohort_where(
    pool: str,
    *,
    min_duration_ms: int,
    url_contains: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    exclude_ids: set | None = None,
) -> tuple[str, list]:
    """Shared WHERE clause for cohort sampling.

    Time bounds use `last_seen` so a window means “sessions that were active
    (or finished) in this period,” matching the recency sort key.

    `exclude_ids` is pushed into SQL (`NOT IN`) so group B does not re-read the
    rows A already took from the same ordered window — filtering them in Python
    after LIMIT made B's effective window smaller by `|A|`.
    """
    where = [
        "pool = ?",
        "chunk_count > 0",
        "(last_seen - first_seen) >= ?",
    ]
    params: list = [pool, int(min_duration_ms)]
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
    excluded = [sid for sid in (exclude_ids or set()) if sid]
    if excluded:
        placeholders = ", ".join("?" for _ in excluded)
        where.append(f"session_id NOT IN ({placeholders})")
        params.extend(excluded)
    return " AND ".join(where), params


def _cohort_order_sql(order: str) -> str:
    # Plain integer on compat.sessions / the real table — not
    # json_extract(interactions, '$.clicks'), which needed a SQLite-shaped
    # shim and defeated any index on sessions.clicks.
    activity = "coalesce(clicks, 0)"
    if order == cohort_spec.ORDER_EARLIEST:
        return f"last_seen ASC, {activity} DESC, session_id ASC"
    return f"last_seen DESC, {activity} DESC, session_id DESC"


def _page_size(n: int) -> int:
    """How many index rows to pull per scan page before the hard cap."""
    return min(
        cohort_spec.CANDIDATE_CAP,
        max(n * 20, n + 200, 500),
    )


def _sample_candidates(
    conn: Connection,
    pool: str,
    *,
    min_duration_ms: int,
    url_contains: str,
    limit: int,
    offset: int = 0,
    start_ms: int | None = None,
    end_ms: int | None = None,
    order: str = cohort_spec.ORDER_LATEST,
    exclude_ids: set | None = None,
) -> list:
    """Index rows (with slice facts) that clear the base duration/path/time gates."""
    where_sql, base_params = _cohort_where(
        pool,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        start_ms=start_ms,
        end_ms=end_ms,
        exclude_ids=exclude_ids,
    )
    order_sql = _cohort_order_sql(
        order if order in cohort_spec.ORDERS else cohort_spec.ORDER_LATEST
    )
    rows = conn.execute(
        f"""
        SELECT session_id, url, "order", device, attribution
        FROM sessions
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ?
        OFFSET ?
        """,
        (*base_params, max(0, int(limit)), max(0, int(offset))),
    ).fetchall()
    out = []
    for sid, url, order_raw, device, attr in rows:
        fact = index_row_facts(url, order_raw, device, attr)
        fact["session_id"] = sid
        out.append(fact)
    return out


def _empty_stats(requested: int = 0) -> SampleStats:
    return SampleStats(requested=requested, matched=0, scanned=0, capped=False)


def _combine_stats(*parts: SampleStats) -> SampleStats:
    requested = sum(p.requested for p in parts)
    matched = sum(p.matched for p in parts)
    scanned = sum(p.scanned for p in parts)
    capped = any(p.capped for p in parts)
    return SampleStats(requested=requested, matched=matched, scanned=scanned, capped=capped)


def _sample_matching_rows(
    conn: Connection,
    pool: str,
    n: int,
    *,
    min_duration_ms: int,
    url_contains: str,
    slice_spec: dict | None,
    exclude_ids: set | None = None,
    tag: str = "a",
    start_ms: int | None = None,
    end_ms: int | None = None,
    order: str = cohort_spec.ORDER_LATEST,
) -> SampleResult:
    """Sessions matching a cut-style slice, tagged for the sample arm.

    Pages through the ordered index until `n` matches, the pool under the base
    gates is exhausted, or `CANDIDATE_CAP` rows have been scanned.
    """
    if n <= 0:
        return SampleResult(sessions=[], sample=_empty_stats(0))
    spec = normalize_slice(slice_spec)
    exclude = exclude_ids or set()
    path_needle = "" if spec.get("page") else url_contains
    page = _page_size(n)
    out: list = []
    scanned = 0
    offset = 0
    while len(out) < n and scanned < cohort_spec.CANDIDATE_CAP:
        batch = min(page, cohort_spec.CANDIDATE_CAP - scanned)
        candidates = _sample_candidates(
            conn,
            pool,
            min_duration_ms=min_duration_ms,
            url_contains=path_needle,
            limit=batch,
            offset=offset,
            start_ms=start_ms,
            end_ms=end_ms,
            order=order,
            # Drop A's members in SQL so B's LIMIT/OFFSET window is not spent
            # re-reading rows it must discard (bug #6).
            exclude_ids=exclude,
        )
        if not candidates:
            break
        scanned += len(candidates)
        offset += len(candidates)
        for row in candidates:
            sid = row["session_id"]
            if not row_matches_slice(row, spec):
                continue
            out.append({"session_id": sid, "filter": tag, "status": "queued"})
            if len(out) >= n:
                break
        if len(candidates) < batch:
            break
    matched = len(out)
    capped = matched < n and scanned >= cohort_spec.CANDIDATE_CAP
    return SampleResult(
        sessions=out,
        sample=SampleStats(requested=n, matched=matched, scanned=scanned, capped=capped),
    )


def sample_cohort(conn: Connection, pool: str, spec: CohortSpec) -> SampleResult:
    """Sessions matching a selection rule, plus how the request resolved.

    `spec.order` is latest (default) or earliest by `last_seen`; `start_ms` /
    `end_ms` bound that same clock. Sessions with more clicks win a tie.
    """
    spec = spec.normalised()
    # Annotated: these are splatted into _sample_matching_rows, and an
    # unannotated literal infers dict[str, object], which loses every parameter
    # type at the call site.
    base: dict[str, Any] = {
        "min_duration_ms": spec.min_duration_ms,
        "url_contains": spec.url_contains,
        "start_ms": spec.start_ms,
        "end_ms": spec.end_ms,
        "order": spec.order,
    }

    if spec.uses_groups:
        a = _sample_matching_rows(conn, pool, spec.a_n, slice_spec=spec.slice_spec, tag="a", **base)
        if spec.b_n > 0:
            b = _sample_matching_rows(
                conn,
                pool,
                spec.b_n,
                # An unset B slice means "same filter as A", not "no filter".
                slice_spec=spec.slice_b or spec.slice_spec,
                exclude_ids={s["session_id"] for s in a.sessions},
                tag="b",
                **base,
            )
            return SampleResult(
                sessions=a.sessions + b.sessions,
                sample=_combine_stats(a.sample, b.sample),
            )
        return a

    filter_spec = dict(normalize_slice(spec.slice_spec))
    if spec.any_n <= 0:
        # The purchased/all arms own the outcome split, so a slice must not
        # constrain it as well.
        filter_spec.pop("outcome", None)

    needed = spec.total
    if needed <= 0:
        return SampleResult(sessions=[], sample=_empty_stats(0))

    path_needle = "" if filter_spec.get("page") else spec.url_contains
    page = _page_size(needed)
    matched_rows: list[dict] = []
    scanned = 0
    offset = 0

    def _full() -> bool:
        if spec.any_n > 0:
            return len(matched_rows) >= spec.any_n
        bought = sum(1 for row in matched_rows if row.get("bought"))
        rest = len(matched_rows) - bought
        return bought >= spec.purchased_n and rest >= spec.all_n

    while not _full() and scanned < cohort_spec.CANDIDATE_CAP:
        batch = min(page, cohort_spec.CANDIDATE_CAP - scanned)
        candidates = _sample_candidates(
            conn,
            pool,
            min_duration_ms=spec.min_duration_ms,
            url_contains=path_needle,
            limit=batch,
            offset=offset,
            start_ms=spec.start_ms,
            end_ms=spec.end_ms,
            order=spec.order,
        )
        if not candidates:
            break
        scanned += len(candidates)
        offset += len(candidates)
        for row in candidates:
            if not row_matches_slice(row, filter_spec):
                continue
            matched_rows.append(row)
            if _full():
                break
        if len(candidates) < batch:
            break

    def _entry(row: dict) -> dict:
        return {
            "session_id": row["session_id"],
            "filter": "purchased" if row.get("bought") else "all",
            "status": "queued",
        }

    if spec.any_n > 0:
        sessions = [_entry(row) for row in matched_rows[: spec.any_n]]
    else:
        purchased: list[dict] = []
        non_purchase: list[dict] = []
        for row in matched_rows:
            if row.get("bought"):
                if len(purchased) < spec.purchased_n:
                    purchased.append(_entry(row))
            elif len(non_purchase) < spec.all_n:
                non_purchase.append(_entry(row))
            if len(purchased) >= spec.purchased_n and len(non_purchase) >= spec.all_n:
                break
        sessions = purchased + non_purchase

    matched = len(sessions)
    capped = matched < needed and scanned >= cohort_spec.CANDIDATE_CAP
    return SampleResult(
        sessions=sessions,
        sample=SampleStats(requested=needed, matched=matched, scanned=scanned, capped=capped),
    )


def sample_cohort_rows(conn: Connection, pool: str, spec: CohortSpec) -> list:
    """Sessions matching a selection rule, in the mode the spec asks for.

    Prefer `sample_cohort` when the caller needs to know whether the request
    was filled; this keeps the list-shaped surface used by tests and the
    compat checks.
    """
    return sample_cohort(conn, pool, spec).sessions
