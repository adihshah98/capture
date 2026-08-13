"""Choosing a cohort's members, and the facets offered when choosing them.

The one part of the package that talks SQL. Everything here takes a connection
and reads the session index; nothing writes.

**Real Postgres.** Placeholders are `%s` (psycopg). Queries hit `public.sessions`
(with an orders join only when a caller still wants timestamps). Row-level
security on those tables (via `db.connection(project_id)`) keeps a caller from
reading another tenant's rows; the `project_id` predicate is still required so
the planner can use the tenant indexes.

**Slice filters are SQL.** `outcome`, `device_class`, `page`, and campaign
columns are materialised at ingest (`classify.session_classes` via `sessionstore`).
Sampling pushes a normalised slice into `WHERE`, then `ORDER BY` + `LIMIT n`.
A short cohort means the pool under those predicates is genuinely short —
`{requested, matched, scanned, capped}` still records that; `capped` stays for
facet scans that deliberately stop at `CANDIDATE_CAP`, not for membership.

The signatures say `spec.Connection`, a Protocol naming the one method these
functions call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import spec as cohort_spec
from .slices import (
    DEVICE_VALUES,
    OUTCOME_LABELS,
    OUTCOME_VALUES,
    normalize_slice,
)
from .spec import CohortSpec, Connection


@dataclass(frozen=True)
class SampleStats:
    """How a sample request resolved.

    `matched < requested` is a shortfall. For membership sampling the filter is
    fully in SQL, so a shortfall means the pool does not hold enough matches
    (`capped=False`). `capped=True` is reserved for bounded scans (facets) that
    stop at `CANDIDATE_CAP` before exhausting the index.
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


def _attribution_from_columns(
    utm_source,
    utm_medium,
    utm_campaign,
    utm_content,
    has_fbclid,
) -> dict:
    """Rebuild the attribution dict cuts/labels expect from denormalised columns."""
    attrs: dict = {}
    if utm_source:
        attrs["utm_source"] = utm_source
    if utm_medium:
        attrs["utm_medium"] = utm_medium
    if utm_campaign:
        attrs["utm_campaign"] = utm_campaign
    if utm_content:
        attrs["utm_content"] = utm_content
    if has_fbclid:
        attrs["fbclid"] = True
    return attrs


def classified_row(
    session_id: str,
    *,
    outcome: str | None,
    device_class: str | None,
    page: str | None,
    utm_source=None,
    utm_medium=None,
    utm_campaign=None,
    utm_content=None,
    has_fbclid: bool = False,
) -> dict:
    """Slice-shaped row from materialised columns (same keys as cut matching)."""
    outcome_value = outcome or "rest"
    return {
        "session_id": session_id,
        "outcome": outcome_value,
        "device": device_class,
        "page": page,
        "attribution": _attribution_from_columns(
            utm_source, utm_medium, utm_campaign, utm_content, has_fbclid
        ),
        "bought": outcome_value == "purchased",
    }


def classified_rows(conn: Connection, project_id: str, session_ids: list | tuple | set) -> dict:
    """Slice-shaped facts for these sessions, from the materialised columns.

    Scoped to the ids the caller already chose (a cohort). Does not scan the
    project and does not re-derive labels from raw JSON — sampling already
    filtered on the same columns, and a cut must use that vocabulary.
    """
    ids = [sid for sid in session_ids if sid]
    if not ids:
        return {}
    placeholders = ", ".join("%s" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT s.session_id, s.outcome, s.device_class, s.page,
               s.utm_source, s.utm_medium, s.utm_campaign, s.utm_content, s.has_fbclid
        FROM sessions s
        WHERE s.project_id = %s AND s.session_id IN ({placeholders})
        """,
        (project_id, *ids),
    ).fetchall()
    out = {}
    for (
        sid,
        outcome,
        device,
        page,
        utm_source,
        utm_medium,
        utm_campaign,
        utm_content,
        has_fbclid,
    ) in rows:
        out[sid] = classified_row(
            sid,
            outcome=outcome,
            device_class=device,
            page=page,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            utm_content=utm_content,
            has_fbclid=bool(has_fbclid),
        )
    return out


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


def _slice_where(slice_spec: dict | None) -> tuple[str, list]:
    """SQL predicates for a normalised cut-style slice on materialised columns.

    Mirrors `row_matches_slice`: AND across keys, OR within multi-value keys;
    unknown `device_class` / `page` (NULL) never satisfy those constraints.
    """
    spec = normalize_slice(slice_spec)
    if not spec:
        return "", []

    where: list[str] = []
    params: list = []

    want_outcome = spec.get("outcome")
    if want_outcome:
        placeholders = ", ".join("%s" for _ in want_outcome)
        where.append(f"s.outcome IN ({placeholders})")
        params.extend(want_outcome)

    want_device = spec.get("device")
    if want_device:
        placeholders = ", ".join("%s" for _ in want_device)
        where.append(f"s.device_class IN ({placeholders})")
        params.extend(want_device)

    want_page = spec.get("page")
    if want_page:
        where.append("s.page = %s")
        params.append(want_page)

    if "fbclid" in spec:
        where.append("s.has_fbclid" if spec["fbclid"] else "NOT s.has_fbclid")

    for key, column in (
        ("utm_medium", "s.utm_medium"),
        ("utm_campaign", "s.utm_campaign"),
        ("utm_content", "s.utm_content"),
    ):
        want = spec.get(key)
        if want:
            where.append(f"{column} = %s")
            params.append(want)

    want_source = spec.get("utm_source")
    if want_source == "meta":
        where.append("s.is_meta")
    elif want_source == "non_meta":
        where.append("NOT s.is_meta")
    elif want_source:
        where.append("s.utm_source = %s")
        params.append(want_source)

    return (" AND ".join(where), params) if where else ("", [])


def pool_facets(
    conn: Connection,
    project_id: str,
    *,
    min_duration_ms: int = 0,
) -> dict:
    """Facet values present in the project index (for the New cohort dialog).

    Scans at most `CANDIDATE_CAP` newest rows under the duration gate — the same
    recency window sampling uses — reading materialised class columns.
    """
    where_sql, params = _cohort_where(project_id, min_duration_ms=min_duration_ms, url_contains="")
    order_sql = _cohort_order_sql(cohort_spec.ORDER_LATEST)
    rows = conn.execute(
        f"""
        SELECT s.session_id, s.outcome, s.device_class, s.page,
               s.utm_source, s.utm_medium, s.utm_campaign, s.utm_content, s.has_fbclid
        FROM sessions s
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT %s
        """,
        (*params, cohort_spec.CANDIDATE_CAP),
    ).fetchall()
    return facets_from_rows(
        [
            classified_row(
                sid,
                outcome=outcome,
                device_class=device,
                page=page,
                utm_source=utm_source,
                utm_medium=utm_medium,
                utm_campaign=utm_campaign,
                utm_content=utm_content,
                has_fbclid=bool(has_fbclid),
            )
            for sid, outcome, device, page, utm_source, utm_medium, utm_campaign, utm_content, has_fbclid in rows
        ]
    )


def _cohort_where(
    project_id: str,
    *,
    min_duration_ms: int,
    url_contains: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    exclude_ids: set | None = None,
    slice_spec: dict | None = None,
) -> tuple[str, list]:
    """Shared WHERE clause for cohort sampling.

    Time bounds use `last_seen` so a window means “sessions that were active
    (or finished) in this period,” matching the recency sort key.

    `exclude_ids` is pushed into SQL (`NOT IN`) so group B does not re-read the
    rows A already took. Slice predicates ride on materialised class columns.
    """
    where = [
        "s.project_id = %s",
        "s.chunk_count > 0",
        "(s.last_seen - s.first_seen) >= %s",
    ]
    params: list = [project_id, int(min_duration_ms)]
    needle = (url_contains or "").strip()
    if needle:
        where.append("s.url LIKE %s")
        params.append(f"%{needle}%")
    if start_ms is not None:
        where.append("s.last_seen >= %s")
        params.append(int(start_ms))
    if end_ms is not None:
        where.append("s.last_seen <= %s")
        params.append(int(end_ms))
    excluded = [sid for sid in (exclude_ids or set()) if sid]
    if excluded:
        placeholders = ", ".join("%s" for _ in excluded)
        where.append(f"s.session_id NOT IN ({placeholders})")
        params.extend(excluded)
    slice_sql, slice_params = _slice_where(slice_spec)
    if slice_sql:
        where.append(slice_sql)
        params.extend(slice_params)
    return " AND ".join(where), params


def _cohort_order_sql(order: str) -> str:
    activity = "coalesce(s.clicks, 0)"
    if order == cohort_spec.ORDER_EARLIEST:
        return f"s.last_seen ASC, {activity} DESC, s.session_id ASC"
    return f"s.last_seen DESC, {activity} DESC, s.session_id DESC"


def _sample_rows(
    conn: Connection,
    project_id: str,
    *,
    n: int,
    min_duration_ms: int,
    url_contains: str,
    slice_spec: dict | None = None,
    exclude_ids: set | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    order: str = cohort_spec.ORDER_LATEST,
) -> list[dict]:
    """Up to `n` classified rows matching base gates + slice, already ordered."""
    if n <= 0:
        return []
    where_sql, params = _cohort_where(
        project_id,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        start_ms=start_ms,
        end_ms=end_ms,
        exclude_ids=exclude_ids,
        slice_spec=slice_spec,
    )
    order_sql = _cohort_order_sql(
        order if order in cohort_spec.ORDERS else cohort_spec.ORDER_LATEST
    )
    rows = conn.execute(
        f"""
        SELECT s.session_id, s.outcome, s.device_class, s.page,
               s.utm_source, s.utm_medium, s.utm_campaign, s.utm_content, s.has_fbclid
        FROM sessions s
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT %s
        """,
        (*params, max(0, int(n))),
    ).fetchall()
    return [
        classified_row(
            sid,
            outcome=outcome,
            device_class=device,
            page=page,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            utm_content=utm_content,
            has_fbclid=bool(has_fbclid),
        )
        for sid, outcome, device, page, utm_source, utm_medium, utm_campaign, utm_content, has_fbclid in rows
    ]


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
    project_id: str,
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

    One SQL query: slice predicates are columns, so `LIMIT n` is exact.
    """
    if n <= 0:
        return SampleResult(sessions=[], sample=_empty_stats(0))
    spec = normalize_slice(slice_spec)
    path_needle = "" if spec.get("page") else url_contains
    rows = _sample_rows(
        conn,
        project_id,
        n=n,
        min_duration_ms=min_duration_ms,
        url_contains=path_needle,
        slice_spec=spec,
        exclude_ids=exclude_ids,
        start_ms=start_ms,
        end_ms=end_ms,
        order=order,
    )
    sessions = [
        {"session_id": row["session_id"], "filter": tag, "status": "queued"} for row in rows
    ]
    matched = len(sessions)
    return SampleResult(
        sessions=sessions,
        sample=SampleStats(requested=n, matched=matched, scanned=matched, capped=False),
    )


def sample_cohort(conn: Connection, project_id: str, spec: CohortSpec) -> SampleResult:
    """Sessions matching a selection rule, plus how the request resolved.

    `spec.order` is latest (default) or earliest by `last_seen`; `start_ms` /
    `end_ms` bound that same clock. Sessions with more clicks win a tie.
    """
    spec = spec.normalised()
    base: dict[str, Any] = {
        "min_duration_ms": spec.min_duration_ms,
        "url_contains": spec.url_contains,
        "start_ms": spec.start_ms,
        "end_ms": spec.end_ms,
        "order": spec.order,
    }

    if spec.uses_groups:
        a = _sample_matching_rows(
            conn, project_id, spec.a_n, slice_spec=spec.slice_spec, tag="a", **base
        )
        if spec.b_n > 0:
            b = _sample_matching_rows(
                conn,
                project_id,
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

    def _entry(row: dict) -> dict:
        return {
            "session_id": row["session_id"],
            "filter": "purchased" if row.get("bought") else "all",
            "status": "queued",
        }

    if spec.any_n > 0:
        rows = _sample_rows(
            conn,
            project_id,
            n=spec.any_n,
            min_duration_ms=spec.min_duration_ms,
            url_contains=path_needle,
            slice_spec=filter_spec,
            start_ms=spec.start_ms,
            end_ms=spec.end_ms,
            order=spec.order,
        )
        sessions = [_entry(row) for row in rows]
        matched = len(sessions)
        return SampleResult(
            sessions=sessions,
            sample=SampleStats(requested=needed, matched=matched, scanned=matched, capped=False),
        )

    # purchased/all: two top-N queries with the same order ≡ interleaved
    # bucket-fill over the merged stream, without scanning non-matches.
    purchased_rows = _sample_rows(
        conn,
        project_id,
        n=spec.purchased_n,
        min_duration_ms=spec.min_duration_ms,
        url_contains=path_needle,
        slice_spec={**filter_spec, "outcome": ["purchased"]},
        start_ms=spec.start_ms,
        end_ms=spec.end_ms,
        order=spec.order,
    )
    non_rows = _sample_rows(
        conn,
        project_id,
        n=spec.all_n,
        min_duration_ms=spec.min_duration_ms,
        url_contains=path_needle,
        slice_spec={**filter_spec, "outcome": ["checkout", "rest"]},
        exclude_ids={row["session_id"] for row in purchased_rows},
        start_ms=spec.start_ms,
        end_ms=spec.end_ms,
        order=spec.order,
    )
    sessions = [_entry(row) for row in purchased_rows] + [_entry(row) for row in non_rows]
    matched = len(sessions)
    scanned = len(purchased_rows) + len(non_rows)
    return SampleResult(
        sessions=sessions,
        sample=SampleStats(requested=needed, matched=matched, scanned=scanned, capped=False),
    )


def sample_cohort_rows(conn: Connection, project_id: str, spec: CohortSpec) -> list:
    """Sessions matching a selection rule, in the mode the spec asks for.

    Prefer `sample_cohort` when the caller needs to know whether the request
    was filled; this keeps the list-shaped surface used by tests.
    """
    return sample_cohort(conn, project_id, spec).sessions
