import asyncio
import base64
import concurrent.futures
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
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
    config,
    db,
    jobs,
    paths,
    retention,
    session,
    workos,
)
from psycopg_pool import PoolTimeout

from .api import auth, cohorts, ingest, meta, sessions
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

    # Every db call reaches the pool through asyncio.to_thread, so the default
    # executor is the queue in front of it. Left at its default of
    # cpu_count + 4 the two limits are set independently and the wrong one
    # binds: on a 10-core box 14 threads contend for 10 connections, and the 4
    # that miss wait on a checkout whose 5s timeout was chosen for "the
    # database is down", not "we out-fanned our own pool" — so a slow query
    # turns into a 500 rather than backpressure. Sizing the executor to the
    # pool makes threads queue in the executor instead, where waiting is free
    # and unbounded, and keeps CAPTURE_DB_POOL_MAX the single knob for
    # per-process database concurrency.
    loop_ = asyncio.get_running_loop()
    if not getattr(loop_, "_default_executor", None):
        loop_.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                max_workers=db.pool_max(),
                thread_name_prefix="capture-db",
            )
        )

    async def loop():
        while True:
            removed = await asyncio.to_thread(retention.sweep_expired_sessions)
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
app.include_router(cohorts.router)

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


@app.exception_handler(PoolTimeout)
async def pool_timeout_response(_request, exc: PoolTimeout) -> JSONResponse:
    """A checkout that timed out is congestion, not a bug in the handler.

    Sizing the executor to the pool keeps request handlers from causing this,
    but background jobs use their own threads and can still take every
    connection. Uncaught this is a 500, which the recorder treats as a dropped
    chunk; 503 + Retry-After is the same answer the database-is-down path
    already gives, and the recorder retries instead of losing the events.
    """
    print(f"db: pool exhausted — {exc}")
    return JSONResponse(
        {"error": "database busy"},
        status_code=503,
        headers={"Retry-After": "1"},
    )


# public/ is generated by `npm run build` and is not in git, so on a fresh clone it
# may not exist yet — and StaticFiles refuses to construct without its directory,
# which would turn "you forgot to build" into "the server will not start". Created
# empty instead: /healthz already reports bundles_present false.
PUBLIC_DIR = paths.BASE_DIR / "public"
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="static")
