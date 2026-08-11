import asyncio
import base64
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

# Load .env before importing anything that reads configuration at import time.
# Real environment variables win, so a deployed box is unaffected by a stray file.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

from . import analysis_port as ap
from . import (
    compat,
    config,
    db,
    jobs,
    paths,
    projects,
    quota,
    session,
    sessionstore,
    storage,
    workos,
)
from .api import auth, ingest, meta, sessions
from .api.deps import (
    require_read_access,
    resolve_project,
    resolve_session,
)
from .errors import ApiError
from .projects import ProjectsUnavailable


@asynccontextmanager
async def lifespan(_app):
    if config.REPLAY_AUTH_DISABLED:
        print(
            "warning: REPLAY_AUTH_DISABLED=1 — the replay UI and read API are "
            "open to anyone who can reach this server"
        )
    elif workos.enabled():
        print(
            "replay auth: WorkOS per-user logins; projects are visible to "
            "their org_id members"
            + (" and operators" if config.OPERATORS else " (no operators set)")
        )
    elif not config.REPLAY_PASSWORD:
        print(
            "REPLAY_PASSWORD is not set — the replay UI and read API will " "return 503 until it is"
        )

    if config.READ_ONLY:
        print(
            "CAPTURE_READ_ONLY=1 — recording is refused and retention is disabled; "
            "this server only reads the database it is pointed at"
        )
    if config.PREFLIGHT:
        print("CAPTURE_PREFLIGHT=1 — no retention sweep, no cohort resume")

    async def loop():
        while True:
            removed = await asyncio.to_thread(sweep_expired_sessions)
            if removed:
                print(f"retention: removed {removed} expired session(s)")
            await asyncio.sleep(24 * 3600)

    task = None if (config.READ_ONLY or config.PREFLIGHT) else asyncio.create_task(loop())

    # Cohorts mid-Batch lose their asyncio task on restart — pick them back up.
    # Cohort state moved to Postgres, so this now depends on the database being
    # reachable. Failing to resume must not stop the server booting: it comes up
    # degraded and reports so, where before it would exit and never serve.
    def _resumable():
        if config.PREFLIGHT:
            return []
        try:
            return ap.pending_batch_resumes()
        except Exception as exc:
            print(f"cohort resume unavailable at startup: {exc}")
            return []

    for owner, cohort in _resumable():
        key = jobs.cohort_key(owner, cohort["id"])
        if key not in jobs.cohort_tasks:
            print(
                f"resuming cohort batch {cohort['id']} ({(cohort.get('batch') or {}).get('name')})"
            )
            jobs.cohort_tasks[key] = asyncio.create_task(jobs.resume_cohort(owner, cohort["id"]))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
        await jobs.stop_all()
        await asyncio.to_thread(db.close_pool)


app = FastAPI(title="capture", lifespan=lifespan)

app.include_router(meta.router)
app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(sessions.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def replay_auth(request: Request, call_next):
    path = re.sub(r"/+", "/", request.url.path).lower()
    protected = path.startswith("/api/") or path == "/replay.html"
    if protected and not config.REPLAY_AUTH_DISABLED:
        if workos.enabled():
            claims = session.read(request.cookies.get(config.SESSION_COOKIE))

            if not claims or not claims.get("user_id"):
                if path == "/replay.html":
                    return RedirectResponse("/login", status_code=302)
                return JSONResponse({"error": "login required", "login": "/login"}, status_code=401)
            request.state.auth = claims
        elif not config.REPLAY_PASSWORD:
            return Response(
                "Replay access is unconfigured. Set REPLAY_PASSWORD to a secret "
                "(or WORKOS_API_KEY + WORKOS_CLIENT_ID for per-user logins), "
                "or REPLAY_AUTH_DISABLED=1 to serve recordings to anyone.\n",
                status_code=503,
                media_type="text/plain",
            )
        else:
            supplied = ""
            header = request.headers.get("authorization", "")
            if header.startswith("Basic "):
                try:
                    supplied = base64.b64decode(header[6:]).decode().partition(":")[2]
                except Exception:
                    supplied = ""
            if not secrets.compare_digest(supplied, config.REPLAY_PASSWORD):
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="capture replay"'},
                )
    return await call_next(request)


# Stale-servable on purpose: a customer's page must never wait on our health.
VISITOR_ASSETS = frozenset({"/capture.js", "/recorder.bundle.js"})
VISITOR_ASSET_CACHE = "public, max-age=3600, stale-while-revalidate=86400, stale-if-error=86400"


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    response = await call_next(request)
    if "cache-control" in response.headers:
        return response
    path = request.url.path
    if path in VISITOR_ASSETS and response.status_code == 200:
        response.headers["Cache-Control"] = VISITOR_ASSET_CACHE
        return response
    if path.endswith((".js", ".css", ".html")) or path == "/":
        response.headers["Cache-Control"] = "no-cache"
    return response


paths.DATA_DIR.mkdir(parents=True, exist_ok=True)


@app.exception_handler(ApiError)
async def api_error_response(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.exception_handler(ProjectsUnavailable)
async def projects_unavailable_response(_request, exc: ProjectsUnavailable) -> JSONResponse:
    print(f"projects: refusing to serve — {exc}")
    return JSONResponse({"error": "project configuration unavailable"}, status_code=503)


def _resolve_cohort_or_404(project: projects.Project, cohort_id: str | None) -> dict:
    """A cohort by id, or the newest one when the caller did not name one."""
    if cohort_id:
        cohort = ap.cohort(project, cohort_id)
        if not cohort:
            raise ApiError(404, "no such cohort")
        return cohort
    ap.import_legacy_cohorts(project)
    cohort = ap.latest_cohort(project)
    if not cohort:
        raise ApiError(404, "no cohort yet")
    return cohort


@app.get("/api/cohorts")
def list_cohorts(request: Request, project: str | None = Query(None)):
    """Every cohort in this pool, newest first — membership counts only."""
    require_read_access(request, project)
    found = resolve_project(project)
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


@app.get("/api/cohorts/{cohort_id}")
def get_cohort(request: Request, cohort_id: str, project: str | None = Query(None)):
    require_read_access(request, project)
    found = resolve_project(project)
    cohort = _resolve_cohort_or_404(found, cohort_id)
    live = ap.cohort_view(found, cohort)
    # Persist refreshed status so a crashed runner still shows accurate counts.
    # Status only — persistable_cohort drops the derived view enrich lays over
    # it, so a poll cannot freeze a guessed `selection` into the file.
    if live.get("sessions") != cohort.get("sessions") or live.get("status") != cohort.get("status"):
        ap.save_cohort(found, ap.storable_cohort(live, cohort))
    return cohort_response(found, live)


@app.patch("/api/cohorts/{cohort_id}")
async def rename_cohort(request: Request, cohort_id: str, project: str | None = Query(None)):
    """Rename a cohort. Membership is frozen — only the label is editable."""
    require_read_access(request, project)
    found = resolve_project(project)
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


@app.delete("/api/cohorts/{cohort_id}")
async def delete_cohort(request: Request, cohort_id: str, project: str | None = Query(None)):
    """Delete a cohort and its cached cuts. Per-session write-ups are kept."""
    require_read_access(request, project)
    found = resolve_project(project)
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


@app.get("/api/facets")
def get_pool_facets(
    request: Request,
    project: str | None = Query(None),
    min_duration_ms: int = Query(ap.MIN_DURATION_MS, ge=0),
):
    """Facet values available in the pool — same fields as the Insights cut picker."""
    require_read_access(request, project)
    found = resolve_project(project)
    with compat.cohort_connection(found.id) as conn:
        return ap.pool_facets(conn, found, min_duration_ms=min_duration_ms)


@app.post("/api/cohorts")
async def create_cohort(
    request: Request,
    project: str | None = Query(None),
    purchased: int | None = Query(None, ge=0, le=ap.MAX_SESSIONS),
    all_n: int | None = Query(None, ge=0, le=ap.MAX_SESSIONS, alias="all"),
    any_n: int = Query(0, ge=0, le=ap.MAX_SESSIONS, alias="any"),
    a_n: int = Query(0, ge=0, le=ap.MAX_SESSIONS, alias="a"),
    b_n: int = Query(0, ge=0, le=ap.MAX_SESSIONS, alias="b"),
    min_duration_ms: int = Query(ap.MIN_DURATION_MS, ge=0),
    url_contains: str = Query(ap.URL_CONTAINS),
    start_ms: int | None = Query(None, ge=0),
    end_ms: int | None = Query(None, ge=0),
    order: str = Query(ap.ORDER_LATEST),
    label: str | None = Query(None, max_length=120),
    outcome: list | None = Query(None),
    device: list | None = Query(None),
    page: str | None = Query(None),
    utm_source: str | None = Query(None),
    utm_medium: str | None = Query(None),
    utm_campaign: str | None = Query(None),
    utm_content: str | None = Query(None),
    fbclid: str | None = Query(None),
    b_outcome: list | None = Query(None),
    b_device: list | None = Query(None),
    b_page: str | None = Query(None),
    b_utm_source: str | None = Query(None),
    b_utm_medium: str | None = Query(None),
    b_utm_campaign: str | None = Query(None),
    b_utm_content: str | None = Query(None),
    b_fbclid: str | None = Query(None),
):
    """Sample a new cohort only — does not start analysis. Prior cohorts are kept.

    Preferred: `a` (+ optional `b`) with independent cut-style filters for each
    group (`outcome`/`device`/… for A, `b_outcome`/`b_device`/… for B).

    Optional `start_ms` / `end_ms` bound `last_seen`; `order` is latest or earliest.
    Optional `label` sets the display name (otherwise an auto label is used).
    Legacy: `any` for one mixed pool, or `purchased` + `all` for a purchase
    split. Omitting sizes falls back to the default 50+50 purchased sample.
    Total sessions are capped at COHORT_MAX_SESSIONS (500).
    """
    require_read_access(request, project)
    found = resolve_project(project)

    sort = (order or "").strip().lower()
    if sort not in ap.ORDERS:
        raise ApiError(400, "order must be latest or earliest")
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise ApiError(400, "start_ms must be <= end_ms")
    chosen_label = ap.clean_label(label)

    slice_a = _slice_from_params(
        outcome,
        device,
        page,
        utm_source,
        utm_medium,
        utm_campaign,
        utm_content,
        fbclid,
    )
    slice_b = _slice_from_params(
        b_outcome,
        b_device,
        b_page,
        b_utm_source,
        b_utm_medium,
        b_utm_campaign,
        b_utm_content,
        b_fbclid,
    )

    use_groups = a_n > 0 or b_n > 0
    if use_groups:
        if any_n or purchased is not None or all_n is not None:
            raise ApiError(400, "use either a/b groups or purchased/all/any, not both")
        if a_n < 1:
            raise ApiError(400, "group A needs at least one session")
        purchased_n = 0
        all_count = 0
        any_count = 0
        total = a_n + b_n
    elif any_n > 0:
        if purchased is not None or all_n is not None:
            raise ApiError(400, "use either any=N or purchased/all, not both")
        purchased_n = 0
        all_count = 0
        any_count = any_n
        total = any_n
    else:
        purchased_n = ap.DEFAULT_PURCHASED_N if purchased is None else purchased
        all_count = ap.DEFAULT_ALL_N if all_n is None else all_n
        # If only one side was set, the other defaults to 0 (not the 50/50 preset).
        if purchased is not None and all_n is None:
            all_count = 0
        if all_n is not None and purchased is None:
            purchased_n = 0
        any_count = 0
        total = purchased_n + all_count
        # Legacy purchased/non-purchase arms own the outcome split.
        slice_a.pop("outcome", None)

    if total < 1:
        raise ApiError(400, "request at least one session")
    if total > ap.MAX_SESSIONS:
        raise ApiError(400, f"cohort size capped at {ap.MAX_SESSIONS} sessions")

    with compat.cohort_connection(resolve_project(project).id) as conn:
        cohort = ap.build_cohort(
            conn,
            found,
            purchased_n=purchased_n,
            all_n=all_count,
            any_n=any_count,
            a_n=a_n if use_groups else 0,
            b_n=b_n if use_groups else 0,
            min_duration_ms=min_duration_ms,
            url_contains=url_contains,
            slice_spec=slice_a,
            slice_b=slice_b if use_groups and b_n > 0 else None,
            start_ms=start_ms,
            end_ms=end_ms,
            order=sort,
            label=chosen_label or None,
        )

    if not cohort["sessions"]:
        raise ApiError(400, "no sessions matched the sample filters")

    ap.save_cohort(found, cohort)
    return JSONResponse(
        cohort_response(found, ap.cohort_view(found, cohort)),
        status_code=201,
    )


@app.post("/api/cohorts/{cohort_id}/analyze")
async def start_cohort_analysis(
    request: Request,
    cohort_id: str,
    project: str | None = Query(None),
    force: bool = Query(False),
):
    """Start (or resume) analysis for an existing idle/paused cohort."""
    require_read_access(request, project)
    jobs.require_runtime()
    found = resolve_project(project)
    cohort = _resolve_cohort_or_404(found, cohort_id)

    key = jobs.cohort_key(found, cohort["id"])
    existing = jobs.cohort_tasks.get(key)
    if existing and not existing.done():
        return JSONResponse(
            cohort_response(found, ap.cohort_view(found, cohort)),
            status_code=202,
        )

    ap.save_cohort(found, {**cohort, "status": "running"})
    task = asyncio.create_task(jobs.run_cohort(found, cohort["id"], force=force))
    jobs.cohort_tasks[key] = task
    live = ap.cohort(found, cohort["id"]) or cohort
    return JSONResponse(
        cohort_response(found, ap.cohort_view(found, live)),
        status_code=202,
    )


@app.post("/api/cohorts/{cohort_id}/pause")
async def pause_cohort_analysis(
    request: Request,
    cohort_id: str,
    project: str | None = Query(None),
):
    """Pause an in-flight cohort analysis; completed session write-ups are kept."""
    require_read_access(request, project)
    found = resolve_project(project)
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


@app.post("/api/cohorts/{cohort_id}/sessions/{session_id}/retry")
async def retry_cohort_session(
    request: Request,
    cohort_id: str,
    session_id: str,
    project: str | None = Query(None),
):
    """Re-run one cohort row after it failed, without re-sampling the cohort."""
    require_read_access(request, project)
    jobs.require_runtime()
    found = resolve_project(project)
    cohort = _resolve_cohort_or_404(found, cohort_id)

    entry = next(
        (e for e in cohort.get("sessions") or [] if e.get("session_id") == session_id),
        None,
    )
    if entry is None:
        raise ApiError(404, "session is not in this cohort")

    resolve_session(session_id, project)
    # Drop the recorded failure first, so a poll landing mid-retry does not
    # re-read the old error back over the running status.
    entry["status"] = "queued"
    entry.pop("error", None)
    ap.clear_job(found, session_id)
    ap.save_cohort(found, {**cohort, "status": "running"})

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
        row["status"] = "running"
        row.pop("error", None)
        ap.save_cohort(found, cohort)
        try:
            # Not forced: a failed row has nothing cached to bust, and if it
            # succeeded meanwhile the retry should settle on that result.
            await jobs.await_session(found, session_id)
            row["status"] = "done"
            row["analysis"] = ap.ANALYSIS_FILE
            row.pop("error", None)
        except Exception as exc:
            row["status"] = "error"
            row["error"] = getattr(exc, "message", None) or str(exc) or "analysis failed"
        ap.save_cohort(found, ap.storable_cohort(ap.cohort_view(found, cohort), cohort))
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


@app.get("/api/cohorts/{cohort_id}/facets")
def get_cohort_facets(request: Request, cohort_id: str, project: str | None = Query(None)):
    """Slice values present in this cohort, so the picker only offers real ones."""
    require_read_access(request, project)
    found = resolve_project(project)
    cohort = _resolve_cohort_or_404(found, cohort_id)
    live = ap.cohort_view(found, cohort)
    return {"cohort_id": cohort["id"], **cohort_facets(found, live)}


@app.get("/api/cohorts/{cohort_id}/cut")
async def get_cut(
    request: Request,
    cohort_id: str,
    project: str | None = Query(None),
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
    require_read_access(request, project)
    found = resolve_project(project)
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


@app.post("/api/cohorts/{cohort_id}/chat")
async def post_cohort_chat(
    request: Request,
    cohort_id: str,
    project: str | None = Query(None),
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
    require_read_access(request, project)
    found = resolve_project(project)
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
    facts = _session_facts(found)
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


def _session_facts(found: projects.Project) -> dict:
    """session_id → {attribution, device, outcome, page} straight from the index."""
    with db.connection(found.id) as conn:
        rows = conn.execute(
            "SELECT s.session_id, s.attribution, s.device, s.url,"
            " o.checkout_started_at, o.purchased_at, o.total, o.currency"
            " FROM sessions s LEFT JOIN orders o"
            "   ON o.project_id = s.project_id AND o.session_id = s.session_id"
        ).fetchall()

    out = {}
    for sid, attribution, device, url, started, purchased, total, currency in rows:
        order = sessionstore._order((started, purchased, None, total, currency))
        out[sid] = {
            "attribution": attribution,
            **ap.classify(device, order, url),
        }
    return out


def _cut_rows(found: projects.Project, cohort: dict) -> list:
    """Analyzed sessions in this cohort, with the write-up a cut reasons over."""
    facts = _session_facts(found)
    rows = []
    for entry in cohort.get("sessions") or []:
        sid = entry["session_id"]
        cached = ap.session_analysis(found, sid)
        text = (cached or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        fact = facts.get(sid) or {}
        rows.append(
            {
                "session_id": sid,
                # Fall back to the sample filter when the index has no order row.
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


def sweep_expired_sessions() -> int:
    # Expiry is decided in SQL rather than by walking every session directory,
    # which is what made startup take ~23s at 9k sessions.
    if config.RETENTION_DAYS <= 0:
        return 0
    cutoff_ms = int((time.time() - config.RETENTION_DAYS * 86400) * 1000)
    removed = 0
    for project in projects.all_projects(fresh=True):
        for session_id in sessionstore.expired(project.id, cutoff_ms):
            # Through storage, not rmtree: dropping only the local directory
            # would strand the chunks in the bucket with nothing pointing at
            # them, and no later sweep could ever find them again.
            storage.delete_session(project.storage_prefix, session_id)
            sessionstore.forget(project.id, session_id)
            # Same rule as delete_session: a session purged here no longer has
            # a directory, so its cohort memberships point at write-ups that
            # are gone.
            try:
                ap.forget_session(project, session_id)
            except Exception as exc:
                print(f"cohort cleanup after expiring {session_id}: {exc}")
            removed += 1
    if removed:
        quota.clear()
    return removed


app.mount("/", StaticFiles(directory=paths.BASE_DIR / "public", html=True), name="static")
