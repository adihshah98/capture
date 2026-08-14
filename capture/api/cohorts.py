"""Cohorts, cuts and the chat over them.

The largest router, and the one that owns the most state: a cohort is a frozen
sample of sessions, a cut is a cached analysis of a filter over that sample, and
both are expensive enough that the endpoints are mostly about not paying twice.

Two registries live here rather than in capture/jobs.py because only this router
touches them — an in-flight cut belongs to one cohort of one project. The analysis
and cohort task registries are in jobs.py instead, because the lifespan shares them.

/api/facets is in this module but not under the /api/cohorts path: it describes a
whole pool, and the cohort picker calls it before a cohort exists. That is why
there is no APIRouter prefix here — adding one would move it.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import analysis_port as ap
from .. import db, jobs, projects
from ..errors import ApiError
from .deps import readable_project, session_dir

router = APIRouter()


def _resolve_cohort_or_404(project: projects.Project, cohort_id: str | None) -> dict:
    """A cohort by id, or the newest one when the caller did not name one."""
    if cohort_id:
        cohort = ap.cohort(project, cohort_id)
        if not cohort:
            raise ApiError(404, "no such cohort")
        return cohort
    cohort = ap.latest_cohort(project)
    if not cohort:
        raise ApiError(404, "no cohort yet")
    return cohort


@router.get("/api/cohorts")
def list_cohorts(found: projects.Project = Depends(readable_project)):
    """Every cohort in this pool, newest first — membership counts only."""
    out = []
    for cohort in ap.cohorts(found):
        live = ap.cohort_view(found, cohort)
        out.append(
            {
                "id": live["id"],
                "label": live.get("label") or live["id"],
                "selection": live.get("selection"),
                "created_at": live.get("created_at"),
                "status": live.get("status"),
                "progress": live.get("progress"),
            }
        )
    return {"cohorts": out}


@router.get("/api/cohorts/{cohort_id}")
def get_cohort(cohort_id: str, found: projects.Project = Depends(readable_project)):
    cohort = _resolve_cohort_or_404(found, cohort_id)
    live = ap.cohort_view(found, cohort)
    # Persist refreshed status so a crashed runner still shows accurate counts.
    # Row-at-a-time, not write_cohort: DELETE+INSERT of membership races
    # mark_cohort_session, and a poll during extract can drop in-flight marks.
    ap.persist_cohort_refresh(found, cohort, live)
    return cohort_response(found, live)


@router.patch("/api/cohorts/{cohort_id}")
async def rename_cohort(
    request: Request,
    cohort_id: str,
    found: projects.Project = Depends(readable_project),
):
    """Rename a cohort. Membership is frozen — only the label is editable."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise ApiError(400, "bad payload") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("label"), str):
        raise ApiError(400, "label is required")
    try:
        renamed = ap.rename_cohort(found, cohort_id, payload["label"])
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if not renamed:
        raise ApiError(404, "no such cohort")
    return cohort_response(found, ap.cohort_view(found, renamed))


@router.delete("/api/cohorts/{cohort_id}")
async def delete_cohort(cohort_id: str, found: projects.Project = Depends(readable_project)):
    """Delete a cohort and its cached cuts. Per-session write-ups are kept."""
    cohort = ap.cohort(found, cohort_id)
    if not cohort:
        raise ApiError(404, "no such cohort")

    key = jobs.cohort_key(found, cohort_id)
    task = jobs.cohort_tasks.get(key)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        batch_name = (cohort.get("batch") or {}).get("name")
        if batch_name:
            await ap.cancel_batch(batch_name)

    # Drop any in-flight cut for this cohort. Compared as a tuple prefix: the
    # old string form could false-match a cohort id that contained the separator.
    for cut_key, cut_task in list(_cut_tasks.items()):
        if cut_key[:2] == key and cut_task and not cut_task.done():
            cut_task.cancel()

    if not ap.delete_cohort(found, cohort_id):
        raise ApiError(404, "no such cohort")
    return {"ok": True, "id": cohort_id}


@router.get("/api/facets")
def get_pool_facets(
    found: projects.Project = Depends(readable_project),
    min_duration_ms: int = Query(ap.MIN_DURATION_MS, ge=0),
):
    """Facet values available in the pool — same fields as the Insights cut picker."""
    with db.connection(found.id) as conn:
        return ap.pool_facets(conn, found, min_duration_ms=min_duration_ms)


def _sample_cohort(found: projects.Project, selection: ap.CohortSpec, label: str) -> dict:
    """Sample a cohort, synchronously. Called via asyncio.to_thread.

    Every step here blocks: the pool checkout waits up to CAPTURE_DB_WAIT_S, and
    the sampling query sorts over the candidate window before any of it reaches
    Python.
    """
    with db.connection(found.id) as conn:
        return ap.build_cohort(conn, found, selection, label=label or None)


@router.post("/api/cohorts")
async def create_cohort(
    request: Request,
    found: projects.Project = Depends(readable_project),
    a_n: int = Query(0, ge=0, le=ap.MAX_SESSIONS, alias="a"),
    b_n: int = Query(0, ge=0, le=ap.MAX_SESSIONS, alias="b"),
    min_duration_ms: int = Query(ap.MIN_DURATION_MS, ge=0),
    url_contains: str = Query(ap.URL_CONTAINS),
    start_ms: int | None = Query(None, ge=0),
    end_ms: int | None = Query(None, ge=0),
    order: str = Query(ap.ORDER_LATEST),
    label: str | None = Query(None, max_length=120),
):
    """Sample a new cohort only — does not start analysis. Prior cohorts are kept.

    Groups only: `a` (+ optional `b`) with independent cut-style filters
    (`outcome`/`device`/… for A, `b_outcome`/`b_device`/… for B).

    Optional `start_ms` / `end_ms` bound `last_seen`; `order` is latest or earliest.
    Optional `label` sets the display name (otherwise an auto label is used).
    Total sessions are capped at COHORT_MAX_SESSIONS (500).
    """

    if any(name in request.query_params for name in ("purchased", "all", "any")):
        raise ApiError(400, "use a/b groups with slice filters, not purchased/all/any")

    sort = (order or "").strip().lower()
    if sort not in ap.ORDERS:
        raise ApiError(400, "order must be latest or earliest")
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise ApiError(400, "start_ms must be <= end_ms")
    chosen_label = ap.clean_label(label)

    slice_a = _slice_from_query(request.query_params)
    slice_b = _slice_from_query(request.query_params, prefix="b_")

    if a_n < 1:
        raise ApiError(400, "group A needs at least one session")
    total = a_n + b_n
    if total > ap.MAX_SESSIONS:
        raise ApiError(400, f"cohort size capped at {ap.MAX_SESSIONS} sessions")

    selection = ap.CohortSpec(
        a_n=a_n,
        b_n=b_n,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        start_ms=start_ms,
        end_ms=end_ms,
        order=sort,
        slice_spec=slice_a,
        slice_b=slice_b if b_n > 0 else {},
    )
    # Off the event loop: this is a pooled psycopg checkout (up to
    # CAPTURE_DB_WAIT_S) plus a sorted scan, and running it inline stalls every
    # other request the worker is serving.
    cohort = await asyncio.to_thread(_sample_cohort, found, selection, chosen_label)

    if not cohort["sessions"]:
        raise ApiError(400, "no sessions matched the sample filters")

    ap.save_cohort(found, cohort)
    return JSONResponse(
        cohort_response(found, ap.cohort_view(found, cohort)),
        status_code=201,
    )


@router.post("/api/cohorts/{cohort_id}/analyze")
async def start_cohort_analysis(
    cohort_id: str,
    found: projects.Project = Depends(readable_project),
    force: bool = Query(False),
):
    """Start (or resume) analysis for an existing idle/paused cohort."""
    jobs.require_runtime()
    cohort = _resolve_cohort_or_404(found, cohort_id)

    key = jobs.cohort_key(found, cohort["id"])
    existing = jobs.cohort_tasks.get(key)
    if existing and not existing.done():
        return JSONResponse(
            cohort_response(found, ap.cohort_view(found, cohort)),
            status_code=202,
        )

    ap.set_cohort_status(found, cohort, "running")
    task = asyncio.create_task(jobs.run_cohort(found, cohort["id"], force=force))
    jobs.cohort_tasks[key] = task
    live = ap.cohort(found, cohort["id"]) or cohort
    return JSONResponse(
        cohort_response(found, ap.cohort_view(found, live)),
        status_code=202,
    )


@router.post("/api/cohorts/{cohort_id}/pause")
async def pause_cohort_analysis(
    cohort_id: str,
    found: projects.Project = Depends(readable_project),
):
    """Pause an in-flight cohort analysis; completed session write-ups are kept."""
    cohort = _resolve_cohort_or_404(found, cohort_id)

    key = jobs.cohort_key(found, cohort["id"])
    task = jobs.cohort_tasks.get(key)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    else:
        batch_name = (cohort.get("batch") or {}).get("name")
        if batch_name:
            await ap.cancel_batch(batch_name)
        ap.mark_paused(found, cohort)

    live = ap.cohort(found, cohort_id) or cohort
    return cohort_response(found, ap.cohort_view(found, live))


@router.post("/api/cohorts/{cohort_id}/sessions/{session_id}/retry")
async def retry_cohort_session(
    cohort_id: str,
    session_id: str,
    found: projects.Project = Depends(readable_project),
):
    """Re-run one cohort row after it failed, without re-sampling the cohort."""
    jobs.require_runtime()
    cohort = _resolve_cohort_or_404(found, cohort_id)

    entry = next(
        (e for e in cohort.get("sessions") or [] if e.get("session_id") == session_id),
        None,
    )
    if entry is None:
        raise ApiError(404, "session is not in this cohort")

    session_dir(found, session_id)
    # Drop the recorded failure first, so a poll landing mid-retry does not
    # re-read the old error back over the running status. One row, not the
    # whole membership: write_cohort would DELETE+INSERT everyone else.
    ap.mark_cohort_session(found, cohort, entry, "queued")
    ap.clear_job(found, session_id)
    ap.set_cohort_status(found, cohort, "running")

    key = jobs.cohort_key(found, cohort["id"])
    existing = jobs.cohort_tasks.get(key)
    if existing and not existing.done():
        # A cohort run owns the file; let it pick this row up on its own pass.
        await jobs.start_session(found, session_id)
    else:
        task = asyncio.create_task(_retry_cohort_session_job(found, cohort["id"], session_id))
        jobs.cohort_tasks[key] = task

    live = ap.cohort(found, cohort["id"]) or cohort
    return JSONResponse(
        cohort_response(found, ap.cohort_view(found, live)),
        status_code=202,
    )


async def _retry_cohort_session_job(
    found: projects.Project, cohort_id: str, session_id: str
) -> None:
    key = jobs.cohort_key(found, cohort_id)
    try:
        cohort = ap.cohort(found, cohort_id) or {}
        row = next(
            (e for e in cohort.get("sessions") or [] if e.get("session_id") == session_id),
            None,
        )
        if row is None:
            return
        ap.mark_cohort_session(found, cohort, row, "running")
        try:
            # Not forced: a failed row has nothing cached to bust, and if it
            # succeeded meanwhile the retry should settle on that result.
            await jobs.await_session(found, session_id)
            ap.mark_cohort_session(found, cohort, row, "done", analysis=ap.ANALYSIS_FILE)
        except Exception as exc:
            ap.mark_cohort_session(
                found,
                cohort,
                row,
                "error",
                error=getattr(exc, "message", None) or str(exc) or "analysis failed",
            )
        live = ap.cohort_view(found, cohort)
        ap.persist_cohort_refresh(found, cohort, live)
    finally:
        jobs.cohort_tasks.pop(key, None)


_cut_tasks: dict = {}
# Failures are kept in memory, not in the cut cache: caching an error would
# make a transient API blip permanent for that cut, and the cache has no
# invalidation path by design.
_cut_errors: dict = {}


def _cut_task_key(project: projects.Project, cohort_id: str, key: str) -> tuple:
    return (project.id, cohort_id, key)


async def _run_cut_job(
    project: projects.Project, cohort_id: str, rows: list, slice_spec, key: str
) -> None:
    task_key = _cut_task_key(project, cohort_id, key)
    try:
        cut = await ap.build_cut(rows, slice_spec)
        ap.save_cut(project, cohort_id, key, cut)
        _cut_errors.pop(task_key, None)
    except Exception as exc:
        _cut_errors[task_key] = getattr(exc, "message", None) or str(exc) or "cut analysis failed"
    finally:
        _cut_tasks.pop(task_key, None)


@router.get("/api/cohorts/{cohort_id}/facets")
def get_cohort_facets(cohort_id: str, found: projects.Project = Depends(readable_project)):
    """Slice values present in this cohort, so the picker only offers real ones."""
    cohort = _resolve_cohort_or_404(found, cohort_id)
    live = ap.cohort_view(found, cohort)
    return {"cohort_id": cohort["id"], **cohort_facets(found, live)}


@router.get("/api/cohorts/{cohort_id}/cut")
async def get_cut(
    cohort_id: str,
    found: projects.Project = Depends(readable_project),
    # The group is an intersection of these; outcome and device take repeats
    # or a comma list, so "purchased+checkout on mobile" is expressible.
    outcome: list | None = Query(None),
    device: list | None = Query(None),
    page: str | None = Query(None),
    utm_source: str | None = Query(None),
    utm_medium: str | None = Query(None),
    utm_campaign: str | None = Query(None),
    utm_content: str | None = Query(None),
    fbclid: str | None = Query(None),
    run: bool = Query(False),
    refresh: bool = Query(False),
):
    """A cut's analysis: cached when we have it, queued when `run=1`.

    Cohorts are frozen once sampled, so a cached cut stays valid for as long as
    the question does. `refresh=1` drops the cached answer and re-asks — the
    escape hatch for a reworded prompt or a new model, and the only way to
    spend a second call on a cut.
    """
    cohort = _resolve_cohort_or_404(found, cohort_id)
    cohort_id = cohort["id"]

    slice_spec = (
        _slice_from_params(
            outcome,
            device,
            page,
            utm_source,
            utm_medium,
            utm_campaign,
            utm_content,
            fbclid,
        )
        or None
    )
    key = ap.cut_key(slice_spec)
    # A refresh is a run by definition: there is no reason to bust the cache
    # and then decline to refill it.
    run = run or refresh

    task_key = _cut_task_key(found, cohort_id, key)
    existing = _cut_tasks.get(task_key)
    if existing and not existing.done():
        # A run already in flight is the fresh answer a refresh was asking for.
        return JSONResponse(
            {"status": "running", "cut": key, "cohort_id": cohort_id}, status_code=202
        )

    cached = ap.cut(found, cohort_id, key)
    if cached and not refresh:
        return {**cached, "cohort_id": cohort_id, "cached": True}

    failure = _cut_errors.get(task_key)
    if failure and not run:
        return JSONResponse(
            {"status": "error", "error": failure, "cut": key, "cohort_id": cohort_id},
            status_code=200,
        )

    if not run:
        # Nothing cached and no mandate to spend a call — let the UI offer it.
        return JSONResponse(
            {"status": "absent", "cut": key, "cohort_id": cohort_id}, status_code=404
        )

    jobs.require_runtime()
    _cut_errors.pop(task_key, None)
    live = ap.cohort_view(found, cohort)
    rows = _cut_rows(found, live)
    if not rows:
        raise ApiError(400, "no analyzed sessions in this cohort yet")

    # Only now, with the call certain to be queued: a refresh that fails its
    # preconditions must leave the old answer standing rather than delete it.
    if refresh:
        ap.clear_cut(found, cohort_id, key)

    task = asyncio.create_task(_run_cut_job(found, cohort_id, rows, slice_spec, key))
    _cut_tasks[task_key] = task
    return JSONResponse({"status": "running", "cut": key, "cohort_id": cohort_id}, status_code=202)


def _sse(event: str, data: dict) -> str:
    """One SSE frame. Newlines in the payload are JSON-escaped, so the data
    line stays a single line and the frame cannot be split by model output."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.post("/api/cohorts/{cohort_id}/chat")
async def post_cohort_chat(
    request: Request,
    cohort_id: str,
    found: projects.Project = Depends(readable_project),
    # Same group selection as /cut — the chat answers about the cut on screen.
    outcome: list | None = Query(None),
    device: list | None = Query(None),
    page: str | None = Query(None),
    utm_source: str | None = Query(None),
    utm_medium: str | None = Query(None),
    utm_campaign: str | None = Query(None),
    utm_content: str | None = Query(None),
    fbclid: str | None = Query(None),
):
    """Ask a question about one group's write-ups. Streams the answer as SSE.

    Unlike a cut, nothing is cached: the question is open-ended, so every turn
    is a fresh call. Text-only, so this needs the model but not the headless
    reconstruction runtime a per-session analysis does.
    """
    cohort = _resolve_cohort_or_404(found, cohort_id)
    cohort_id = cohort["id"]

    if not ap.enabled():
        raise ApiError(503, "analysis is not configured on this server")

    try:
        payload = await request.json()
    except Exception as exc:
        raise ApiError(400, "expected a JSON body") from exc
    messages = ap.clean_chat_messages(
        (payload or {}).get("messages") if isinstance(payload, dict) else None
    )
    if not messages:
        raise ApiError(400, "no question to answer")
    if messages[-1]["role"] != "user":
        raise ApiError(400, "the last message must be the user's question")

    slice_spec = (
        _slice_from_params(
            outcome,
            device,
            page,
            utm_source,
            utm_medium,
            utm_campaign,
            utm_content,
            fbclid,
        )
        or None
    )

    live = ap.cohort_view(found, cohort)
    rows = _cut_rows(found, live)
    if not rows:
        raise ApiError(400, "no analyzed sessions in this cohort yet")

    selected, label, given, citations, spec = await ap.chat_context(rows, slice_spec)
    if not selected:
        raise ApiError(400, f"no analyzed sessions match {label} in this cohort")

    async def events():
        # The client needs the group's size and citation map before any text,
        # so it can render the denominator and resolve S<N> as answers stream.
        yield _sse(
            "meta",
            {
                "cohort_id": cohort_id,
                "label": label,
                "n": len(selected),
                "session_ids": [r.get("session_id") for r in selected],
                "citations": citations,
                "model": ap.MODEL,
            },
        )
        try:
            model_gate = ap.model_gate()
            async with model_gate:
                async for chunk in ap.stream_chat(selected, label, given, messages, spec):
                    yield _sse("delta", {"text": chunk})
        except asyncio.CancelledError:
            # The reader hung up. Nothing to report to — let the task unwind.
            raise
        except Exception as exc:
            # The response is already 200 with frames sent, so a failure has to
            # travel as an event rather than a status code.
            yield _sse("error", {"error": str(exc) or "chat failed"})
            return
        yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Nginx buffers proxied responses by default, which would hold the
            # whole answer back and defeat streaming entirely.
            "X-Accel-Buffering": "no",
        },
    )


def stamp_cohort_attribution(found: projects.Project, cohort: dict) -> dict:
    """Attach attribution, device, outcome and page so the UI can slice client-side."""
    facts = _session_facts(found, cohort)
    sessions = []
    for entry in cohort.get("sessions") or []:
        row = dict(entry)
        fact = facts.get(row.get("session_id")) or {}
        if fact.get("attribution"):
            row["attribution"] = fact["attribution"]
        else:
            row.pop("attribution", None)
        if fact.get("device"):
            row["device"] = fact["device"]
        if fact.get("page"):
            row["page"] = fact["page"]
        else:
            row.pop("page", None)
        row["outcome"] = fact.get("outcome") or (
            "purchased" if row.get("filter") == "purchased" else "rest"
        )
        sessions.append(row)
    return {**cohort, "sessions": sessions}


def cohort_response(found: projects.Project, cohort: dict) -> dict:
    return stamp_cohort_attribution(found, ap.with_insights(found, cohort))


def _session_facts(found: projects.Project, cohort: dict) -> dict:
    """session_id → classified row from materialised columns, this cohort only."""
    ids = [e.get("session_id") for e in cohort.get("sessions") or [] if e.get("session_id")]
    if not ids:
        return {}
    with db.connection(found.id) as conn:
        return ap.classified_rows(conn, found, ids)


def _cut_rows(found: projects.Project, cohort: dict) -> list:
    """Analyzed sessions in this cohort, with the write-up a cut reasons over."""
    facts = _session_facts(found, cohort)
    analyses = ap.session_analyses(
        found, [e["session_id"] for e in cohort.get("sessions") or [] if e.get("session_id")]
    )
    rows = []
    for entry in cohort.get("sessions") or []:
        sid = entry["session_id"]
        cached = analyses.get(sid)
        text = (cached or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        fact = facts.get(sid) or {}
        rows.append(
            {
                "session_id": sid,
                # Fall back to the sample filter when the index has no class row.
                "outcome": fact.get("outcome")
                or ("purchased" if entry.get("filter") == "purchased" else "rest"),
                "device": fact.get("device"),
                "page": fact.get("page"),
                "attribution": fact.get("attribution"),
                "text": text,
            }
        )
    return rows


def cohort_facets(found: projects.Project, cohort: dict) -> dict:
    """The slice values actually present in this cohort, for the group picker.

    Counts come from analyzed sessions only, since those are the ones a cut
    can reason over — offering a filter that would select zero write-ups is
    worse than not offering it.
    """
    return ap.facets_from_rows(_cut_rows(found, cohort))


def _parse_fbclid_flag(raw: str | None) -> bool | None:
    if raw is None or raw == "":
        return None
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "meta"):
        return True
    if value in ("0", "false", "no"):
        return False
    raise ApiError(400, "fbclid must be 1 or 0")


def _multi_param(raw, allowed, field) -> list:
    """Repeated query params or a comma list, validated against `allowed`."""
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    out = []
    for item in values:
        for part in str(item or "").split(","):
            value = part.strip().lower()
            if not value:
                continue
            # "all" is the legacy spelling for "did not purchase".
            if field == "outcome" and value == "all":
                for legacy in ("checkout", "rest"):
                    if legacy not in out:
                        out.append(legacy)
                continue
            if value not in allowed:
                raise ApiError(400, f"{field} must be one of: " + ", ".join(allowed))
            if value not in out:
                out.append(value)
    return out


def _slice_from_query(params, prefix: str = "") -> dict:
    """Cut-style slice from query params, optionally prefixed (`b_outcome`, …)."""

    def first(name: str) -> str | None:
        values = params.getlist(f"{prefix}{name}")
        return values[0] if values else None

    return _slice_from_params(
        params.getlist(f"{prefix}outcome") or None,
        params.getlist(f"{prefix}device") or None,
        first("page"),
        first("utm_source"),
        first("utm_medium"),
        first("utm_campaign"),
        first("utm_content"),
        first("fbclid"),
    )


def _slice_from_params(
    outcome,
    device,
    page,
    utm_source,
    utm_medium,
    utm_campaign,
    utm_content,
    fbclid,
) -> dict:
    spec: dict = {}
    outcomes = _multi_param(outcome, ap.OUTCOME_VALUES, "outcome")
    if outcomes:
        spec["outcome"] = outcomes
    devices = _multi_param(device, ap.DEVICE_VALUES, "device")
    if devices:
        spec["device"] = devices
    if isinstance(page, str) and page.strip():
        spec["page"] = page.strip()
    for key, value in (
        ("utm_source", utm_source),
        ("utm_medium", utm_medium),
        ("utm_campaign", utm_campaign),
        ("utm_content", utm_content),
    ):
        if isinstance(value, str) and value.strip():
            spec[key] = value.strip()
    flag = _parse_fbclid_flag(fbclid)
    if flag is not None:
        spec["fbclid"] = flag
    return spec
