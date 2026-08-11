import asyncio
import base64
import hashlib
import hmac
import json
import random
import re
import secrets
import shutil
import string
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

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
from . import compat, config, db, paths, projects, sessionstore, snippet, storage, workos
from .errors import ApiError
from .fsutil import atomic_write_bytes, atomic_write_text
from .paths import SAFE_CHUNK, SAFE_ID, chunk_timestamp, chunk_window
from .projects import ProjectsUnavailable


@asynccontextmanager
async def lifespan(_app):
    if config.REPLAY_AUTH_DISABLED:
        print(
            "warning: config.REPLAY_AUTH_DISABLED=1 — the replay UI and read API are "
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
            "config.REPLAY_PASSWORD is not set — the replay UI and read API will "
            "return 503 until it is"
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
        key = _cohort_key(owner, cohort["id"])
        if key not in _cohort_tasks:
            print(
                f"resuming cohort batch {cohort['id']} ({(cohort.get('batch') or {}).get('name')})"
            )
            _cohort_tasks[key] = asyncio.create_task(_resume_cohort_batch(owner, cohort["id"]))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
        await _stop_background_jobs()
        await asyncio.to_thread(db.close_pool)


app = FastAPI(title="capture", lifespan=lifespan)

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
            session = read_session(request.cookies.get(config.SESSION_COOKIE))

            if not session or not session.get("user_id"):
                if path == "/replay.html":
                    return RedirectResponse("/login", status_code=302)
                return JSONResponse({"error": "login required", "login": "/login"}, status_code=401)
            request.state.auth = session
        elif not config.REPLAY_PASSWORD:
            return Response(
                "Replay access is unconfigured. Set config.REPLAY_PASSWORD to a secret "
                "(or WORKOS_API_KEY + WORKOS_CLIENT_ID for per-user logins), "
                "or config.REPLAY_AUTH_DISABLED=1 to serve recordings to anyone.\n",
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


def _sign(raw: str) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def make_session(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{raw}.{_sign(raw)}"


def read_session(cookie: str | None) -> dict | None:
    if not cookie or "." not in cookie:
        return None
    raw, sig = cookie.rsplit(".", 1)
    if not secrets.compare_digest(_sign(raw), sig):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def load_projects() -> dict:
    """Kept for the read paths that only need name/org/domains by handle. The
    record of truth is Postgres; a database that will not answer must fail
    closed rather than degrade to {} and route every token into one pool."""
    try:
        return {
            p.handle: {"name": p.name, "org_id": p.org_id, "domains": list(p.domains)}
            for p in projects.all_projects()
        }
    except Exception as exc:
        raise ProjectsUnavailable(f"cannot read projects: {exc}") from exc


def require_allowed_domain(request: Request, project: projects.Project | None) -> None:
    domains = list(project.domains) if project else []
    if not domains:
        return
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    host = (urllib.parse.urlparse(source).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in domains):
        raise ApiError(403, "domain not allowed for this project")


def gunzip(body: bytes, limit: int) -> bytes:
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = stream.decompress(body, limit)
    if stream.unconsumed_tail:
        raise ValueError("decompressed payload too large")
    return raw


_bytes_used: dict = {}


def project_bytes_used(project) -> int:
    """Cached so the quota check is not a query per ingest; the running total is
    maintained in SQL and re-read only on a cold cache."""
    if project.id not in _bytes_used:
        _bytes_used[project.id] = sessionstore.project_bytes(project.id)
    return _bytes_used[project.id]


@app.exception_handler(ApiError)
async def api_error_response(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.exception_handler(ProjectsUnavailable)
async def projects_unavailable_response(_request, exc: ProjectsUnavailable) -> JSONResponse:
    print(f"projects: refusing to serve — {exc}")
    return JSONResponse({"error": "project configuration unavailable"}, status_code=503)


def resolve_project(handle: str | None) -> projects.Project:
    found = projects.by_handle(handle)
    if found is None:
        raise ApiError(404, "unknown project")
    return found


def resolve_pool(project: str | None) -> Path:
    return resolve_project(project).pool


def resolve_session(session_id: str, project: str | None) -> Path:
    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")
    path = resolve_pool(project) / session_id
    if not path.is_dir():
        raise ApiError(404, "not found")
    return path


@app.post("/ingest")
async def ingest(
    request: Request,
    session_id: str = Query(""),
    compression: str | None = Query(None),
    token: str | None = Query(None),
):
    if config.READ_ONLY:
        raise ApiError(403, "this server is read-only and does not accept recordings")
    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")

    project = projects.by_token(token)
    if project is None:
        raise ApiError(401, "unknown project token")
    require_allowed_domain(request, project)

    if (
        config.MIN_FREE_DISK_BYTES
        and shutil.disk_usage(paths.DATA_ROOT).free < config.MIN_FREE_DISK_BYTES
    ):
        raise ApiError(507, "server storage full")
    if config.MAX_PROJECT_BYTES and project_bytes_used(project) >= config.MAX_PROJECT_BYTES:
        raise ApiError(429, "project storage quota reached")

    body = await request.body()
    if len(body) > config.MAX_BODY_BYTES:
        raise ApiError(413, "payload too large")

    try:
        raw = gunzip(body, config.MAX_DECOMPRESSED_BYTES) if compression == "gzip-js" else body
        payload = json.loads(raw)
    except Exception as exc:
        raise ApiError(400, "bad payload") from exc

    if not isinstance(payload, dict):
        raise ApiError(400, "bad payload")

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise ApiError(400, "no events")

    # No directory is created here any more: chunks go to storage and the last
    # two sidecars are columns. Analysis makes one when it stages frames.
    meta = {}
    visitor, url = payload.get("visitor_id"), payload.get("url")
    if isinstance(visitor, str) and SAFE_ID.match(visitor):
        meta["visitor_id"] = visitor
    if isinstance(url, str) and url:
        meta["url"] = url[:2048]

    now = int(time.time() * 1000)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    window_prefix = re.sub(r"[^a-f0-9]", "", str(payload.get("window_id") or ""))[:8]
    parts = [str(now), window_prefix, suffix]
    chunk_name = "-".join(p for p in parts if p) + ".json"
    stored = json.dumps(
        {
            "window_id": payload.get("window_id"),
            "url": payload.get("url"),
            "received_at": now,
            "events": events,
        }
    ).encode()
    # Off the loop deliberately. A 380KB PUT to S3 takes ~64ms; run inline it
    # would serialise every other request behind it and cap one worker at
    # roughly 15 req/s, against ~70 when this was a local write.
    await asyncio.to_thread(
        storage.put_chunk, project.storage_prefix, session_id, chunk_name, stored
    )
    clicks, rage = count_clicks(events)
    device = read_device(events)
    size = len(stored)
    first_ts, last_ts = event_span(events) or (now, now)

    window_id = str(payload.get("window_id") or "default")
    state = await asyncio.to_thread(sessionstore.window_state, project.id, session_id, window_id)
    active_ts = last_active_scan(events, state)
    if active_ts:
        state["active"] = max(state.get("active", 0), active_ts)
    state["first"] = min(state.get("first", first_ts), first_ts)
    state["last"] = max(state.get("last", last_ts), last_ts)

    await asyncio.to_thread(
        sessionstore.record_chunk,
        project.id,
        session_id,
        window_id=window_id,
        first_ts=first_ts,
        last_ts=last_ts,
        active_ts=active_ts,
        size=size,
        meta=meta,
        clicks=clicks,
        rage=rage,
        device=device,
        attribution=parse_attribution(meta.get("url")),
        state=state,
    )
    _bytes_used[project.id] = _bytes_used.get(project.id, 0) + size

    return {"status": "ok", "stored": len(events)}


ORDER_FILE = "order.json"
VISITOR_FILE = "visitor.json"

INTERACTIONS_FILE = "interactions.json"

DEVICE_FILE = "device.json"


def read_device(events: list) -> dict | None:
    for event in events:
        if not isinstance(event, dict) or event.get("type") != 4:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        width, height = data.get("width"), data.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0:
            return {"width": width, "height": height}
    return None


def count_clicks(events: list) -> tuple:
    """This batch's clicks only. The running totals accumulate in SQL, where an
    increment cannot be lost between a read and a write."""
    clicks = rage = 0
    for event in events:
        if not isinstance(event, dict) or event.get("type") != 5:
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("tag") != "capture-click":
            continue
        clicks += 1
        payload = data.get("payload")
        if isinstance(payload, dict) and payload.get("rage"):
            rage += 1
    return clicks, rage


@app.post("/order")
async def order_event(
    request: Request,
    session_id: str = Query(""),
    token: str | None = Query(None),
):
    if config.READ_ONLY:
        raise ApiError(403, "this server is read-only and does not accept recordings")
    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")
    project = projects.by_token(token)
    if project is None:
        raise ApiError(401, "unknown project token")

    body = await request.body()
    if len(body) > config.ORDER_MAX_BODY_BYTES:
        raise ApiError(413, "payload too large")
    try:
        payload = json.loads(body)
    except Exception as exc:
        raise ApiError(400, "bad payload") from exc
    if not isinstance(payload, dict):
        raise ApiError(400, "bad payload")
    event = payload.get("event")
    if event not in ("checkout_started", "checkout_completed"):
        raise ApiError(400, "unknown event")

    now = int(time.time() * 1000)
    order = sessionstore.record_order(project.id, session_id, event, payload, now)
    if order is None:
        raise ApiError(404, "unknown session")
    return {"status": "ok"}


@app.get("/healthz")
def healthz():
    checks = {}

    try:
        probe = paths.DATA_ROOT / ".healthz"
        atomic_write_text(probe, "ok")
        probe.unlink()
        checks["storage_writable"] = True
    except Exception:
        checks["storage_writable"] = False

    try:
        free = shutil.disk_usage(paths.DATA_ROOT).free
        checks["disk_above_floor"] = (
            not config.MIN_FREE_DISK_BYTES or free >= config.MIN_FREE_DISK_BYTES
        )
    except Exception:
        checks["disk_above_floor"] = False

    try:
        load_projects()
        checks["projects_readable"] = True
    except ProjectsUnavailable:
        checks["projects_readable"] = False

    checks["bundles_present"] = all(
        (paths.BASE_DIR / "public" / name).is_file()
        for name in ("capture.js", "recorder.bundle.js", "replay.bundle.js")
    )
    checks["replay_auth_enabled"] = (
        bool(config.REPLAY_PASSWORD or workos.enabled()) and not config.REPLAY_AUTH_DISABLED
    )
    # Reported, not health-gating: analysis is an optional extra, and a box
    # without a key or a browser is still a perfectly healthy recorder.
    checks["read_only"] = config.READ_ONLY
    checks["analysis_configured"] = ap.enabled()
    checks["analysis_runtime_present"] = ap.runtime_present()

    informational = (
        "replay_auth_enabled",
        "read_only",
        "analysis_configured",
        "analysis_runtime_present",
    )
    healthy = all(v for k, v in checks.items() if k not in informational)
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )


@app.get("/config")
def remote_config(token: str | None = Query(None)):
    project = projects.by_token(token) if token else None
    if project is not None and project.config:
        response = JSONResponse(project.config)
        response.headers["Cache-Control"] = "no-cache"
        return response
    config_path = paths.CONFIG_PATH
    if project is not None and project.storage_prefix:
        project_config = paths.PROJECTS_DIR / project.storage_prefix / "config.json"
        if project_config.is_file():
            config_path = project_config
    try:
        cfg = json.loads(config_path.read_text())
        if not isinstance(cfg, dict):
            raise ValueError("config.json must be a JSON object")
    except FileNotFoundError:
        cfg = {}
    except Exception as exc:
        print(f"config: serving defaults, {config_path.name} unusable: {exc}")
        cfg = {}
    response = JSONResponse(cfg)
    response.headers["Cache-Control"] = "no-cache"
    return response


ACTIVE_SOURCES = {1, 2, 3, 4, 5, 6, 7, 12}
SNAPSHOT_META_WINDOW_MS = 500
WINDOW_STATE_FILE = "window-state.json"


def last_active_scan(events: list, state: dict) -> int | None:
    """Mirror the player's buildSegments activity rules; `state` carries the
    per-window href/nav position across chunks."""
    nav = state.get("nav", -(10**15))
    last = None
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = e.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        kind = e.get("type")
        payload = e.get("data")
        data = payload if isinstance(payload, dict) else {}
        active = False
        if kind == 4:
            href = data.get("href")
            if href != state.get("href"):
                state["href"] = href
                nav = ts
                active = True
        elif kind == 2:
            active = ts - nav <= SNAPSHOT_META_WINDOW_MS
        elif kind == 3:
            active = data.get("source") in ACTIVE_SOURCES
        if active:
            last = ts
    state["nav"] = nav
    return int(last) if last is not None else None


def event_span(events: list) -> tuple | None:
    ts = [
        e["timestamp"]
        for e in events
        if isinstance(e, dict) and isinstance(e.get("timestamp"), (int, float))
    ]
    return (int(min(ts)), int(max(ts))) if ts else None


def window_duration(states: dict) -> int | None:
    """Clamped span of the primary (biggest) window — the one the player
    opens by default."""
    spans = [w for w in states.values() if isinstance(w.get("first"), (int, float))]
    if not spans:
        return None
    primary = max(spans, key=lambda w: w.get("bytes", 0))
    end = primary["last"]
    if primary.get("active"):
        end = min(end, primary["active"] + 1500)
    return max(int(end - primary["first"]), 0)


def chunk_files(session_dir: Path) -> list:
    return sorted(
        (c for c in session_dir.glob("*.json") if SAFE_CHUNK.match(c.name)),
        key=lambda c: chunk_timestamp(c.name),
    )


def read_pool(project: str | None) -> Path | None:
    found = projects.by_handle(project)
    return found.pool if found else None


ATTRIBUTION_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "fbclid")


def parse_attribution(url: str | None) -> dict | None:
    """Pull utm_* / fbclid from an entry URL for indexing and segment rollups."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    params: dict = {}
    for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=False).items():
        if values and values[0]:
            params[key] = values[0]
    fragment = parsed.fragment or ""
    if fragment and re.match(r"^[^/]+=", fragment):
        for key, values in urllib.parse.parse_qs(fragment, keep_blank_values=False).items():
            if key not in params and values and values[0]:
                params[key] = values[0]
    out: dict[str, str | bool] = {}
    for key in ATTRIBUTION_KEYS:
        if key not in params:
            continue
        if key == "fbclid":
            out["fbclid"] = True
        else:
            out[key] = str(params[key])[:256]
    return out or None


def session_states(entry: Path) -> tuple:
    """Rebuild per-window spans from the chunks themselves, so a session that
    predates window-state.json still resolves. Returns (states, first, last, active)."""
    first = last = None
    active = 0
    states: dict = {}
    for chunk in chunk_files(entry):
        try:
            data = json.loads(chunk.read_text())
        except Exception:
            continue
        events = data.get("events") or []
        span = event_span(events)
        if not span:
            continue
        first = span[0] if first is None else min(first, span[0])
        last = span[1] if last is None else max(last, span[1])
        state = states.setdefault(str(data.get("window_id") or "default"), {})
        chunk_active = last_active_scan(events, state)
        if chunk_active:
            active = max(active, chunk_active)
            state["active"] = max(state.get("active", 0), chunk_active)
        state["first"] = min(state.get("first", span[0]), span[0])
        state["last"] = max(state.get("last", span[1]), span[1])
        state["bytes"] = state.get("bytes", 0) + chunk.stat().st_size
    return states, first, last, active


def session_info(entry: Path) -> dict | None:
    chunks = chunk_files(entry)
    if not chunks:
        return None
    states, first, last, active = session_states(entry)
    info = {
        "session_id": entry.name,
        "first_seen": first if first is not None else chunk_timestamp(chunks[0].name),
        "last_seen": last if last is not None else chunk_timestamp(chunks[-1].name),
        "last_active": active,
        "duration": window_duration(states),
        "windows": len(states) or 1,
        "chunk_count": len(chunks),
        "bytes": sum(c.stat().st_size for c in chunks),
    }
    for key, name in (
        ("order", ORDER_FILE),
        ("interactions", INTERACTIONS_FILE),
        ("device", DEVICE_FILE),
    ):
        path = entry / name
        if path.is_file():
            try:
                value = json.loads(path.read_text())
                if isinstance(value, dict):
                    info[key] = value
            except Exception:
                pass
    meta_path = entry / VISITOR_FILE
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text())
            info.update({k: meta[k] for k in ("visitor_id", "url") if isinstance(meta.get(k), str)})
        except Exception:
            pass
    attribution = parse_attribution(info.get("url"))
    if attribution:
        info["attribution"] = attribution
    return info


@app.get("/login")
def login():
    if not workos.enabled():
        return Response(
            "Per-user login is not configured; set WORKOS_API_KEY and WORKOS_CLIENT_ID.\n",
            status_code=503,
            media_type="text/plain",
        )
    state = secrets.token_urlsafe(24)
    target = workos.authorize_url(snippet.public_base_url() + "/auth/callback", state)
    response = RedirectResponse(target, status_code=302)
    response.set_cookie(
        config.STATE_COOKIE, state, max_age=600, httponly=True, secure=True, samesite="lax"
    )
    return response


@app.get("/auth/callback")
def auth_callback(
    request: Request, code: str | None = Query(None), state: str | None = Query(None)
):
    if not workos.enabled():
        raise ApiError(503, "login not configured")
    if not code or not state or state != request.cookies.get(config.STATE_COOKIE):
        return RedirectResponse("/login", status_code=302)
    try:
        email, user_id = workos.authenticate(code)
    except Exception as exc:
        print(f"auth: WorkOS code exchange failed: {exc}")
        raise ApiError(401, "login failed — start again at /login") from exc
    if not user_id:
        raise ApiError(401, "login failed — start again at /login")
    session = make_session(
        {"email": email, "user_id": user_id, "exp": int(time.time()) + config.SESSION_TTL_S}
    )
    response = RedirectResponse("/replay.html", status_code=302)
    response.set_cookie(
        config.SESSION_COOKIE,
        session,
        max_age=config.SESSION_TTL_S,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    response.delete_cookie(config.STATE_COOKIE)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(config.SESSION_COOKIE)
    return response


def allowed_projects(request: Request) -> set | None:
    auth = getattr(request.state, "auth", None)
    if auth is None or (auth.get("email") or "").lower() in config.OPERATORS:
        return None
    orgs = workos.user_orgs(auth.get("user_id") or "")
    return {p.handle for p in projects.for_orgs(orgs)}


def require_read_access(request: Request, project: str | None) -> None:
    allowed = allowed_projects(request)
    if allowed is not None and (not project or project not in allowed):
        raise ApiError(403, "not your project")


@app.get("/api/projects")
def list_projects(request: Request):
    allowed = allowed_projects(request)
    visible = [
        {"token": p.handle, "name": p.name}
        for p in projects.all_projects()
        if allowed is None or p.handle in allowed
    ]
    email = (getattr(request.state, "auth", None) or {}).get("email")
    return {
        "projects": visible,
        "analysis_enabled": ap.enabled() and ap.runtime_present(),
        **({"email": email} if email else {}),
    }


SESSION_FILTERS = sessionstore.FILTERS


@app.get("/api/sessions")
def list_sessions(
    request: Request,
    project: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    filter: str | None = Query(None),
    q: str | None = Query(None),
):
    require_read_access(request, project)
    try:
        return sessionstore.listing(
            resolve_project(project).id, limit=limit, cursor=cursor, filter=filter, q=q
        )
    except ValueError as exc:
        raise ApiError(400, "bad cursor") from exc


@app.get("/api/sessions/{session_id}")
def get_session(request: Request, session_id: str, project: str | None = Query(None)):
    require_read_access(request, project)
    found = resolve_project(project)
    info = sessionstore.get(found.id, session_id) or session_info(
        resolve_session(session_id, project)
    )
    if info is None:
        raise ApiError(404, "not found")
    return info


@app.delete("/api/sessions/{session_id}")
def delete_session(request: Request, session_id: str, project: str | None = Query(None)):
    require_read_access(request, project)
    found = resolve_project(project)
    # Called for its 404: deleting an unknown session must not report success.
    resolve_session(session_id, project)
    freed = storage.delete_session(found.storage_prefix, session_id)
    sessionstore.forget(found.id, session_id)
    # Cohort membership references a recording that no longer exists. Left
    # behind, enrich_cohort_status falls back to the stored status and keeps
    # counting the session as analyzed.
    try:
        ap.forget_session(found, session_id)
    except Exception as exc:
        # The session is already gone from the index; a failure here leaves a
        # stale membership row, which is bad but not worth failing the delete.
        print(f"cohort cleanup after forgetting {session_id}: {exc}")
    _bytes_used.pop(found.id, None)
    print(f"deleted session {session_id} ({freed} bytes)")
    return {"status": "deleted", "session_id": session_id, "bytes_freed": freed}


@app.get("/api/sessions/{session_id}/chunks")
def session_chunks(request: Request, session_id: str, project: str | None = Query(None)):
    require_read_access(request, project)
    found = resolve_project(project)
    chunks = storage.list_chunks(found.storage_prefix, session_id)
    if not chunks:
        raise ApiError(404, "not found")
    url = None
    first = storage.open_chunk(found.storage_prefix, session_id, chunks[0].name)
    if first:
        try:
            url = json.loads(first).get("url")
        except Exception:
            pass
    manifest = [
        {
            "name": c.name,
            "timestamp": chunk_timestamp(c.name),
            "bytes": c.size,
            "window": chunk_window(c.name),
        }
        for c in chunks
    ]
    windows = list(dict.fromkeys(c["window"] for c in manifest))
    return {"session_id": session_id, "url": url, "windows": windows, "chunks": manifest}


@app.get("/api/sessions/{session_id}/chunks/{chunk_name}")
def session_chunk(
    request: Request, session_id: str, chunk_name: str, project: str | None = Query(None)
):
    require_read_access(request, project)
    if not SAFE_ID.match(session_id) or not SAFE_CHUNK.match(chunk_name):
        raise ApiError(400, "invalid id")
    prefix = resolve_project(project).storage_prefix

    # Hand the browser a presigned URL so the bytes never transit the origin.
    # None means the chunk has not reached S3 yet, so serve it from the pool.
    signed = storage.chunk_url(prefix, session_id, chunk_name)
    if signed:
        # The redirect itself must not be cached — the URL it carries expires.
        # The object it points at is immutable and caches for a day.
        return RedirectResponse(signed, status_code=307, headers={"Cache-Control": "no-store"})

    body = storage.open_chunk(prefix, session_id, chunk_name)
    if body is None:
        raise ApiError(404, "not found")
    return Response(
        body,
        media_type="application/json",
        headers={"Cache-Control": storage.CHUNK_CACHE_CONTROL},
    )


class _Running(NamedTuple):
    """The project is carried alongside the task so shutdown can report a job as
    failed without looking anything up — at shutdown the database may already be
    going away, and the registry used to reconstruct a Path from its own key."""

    project: projects.Project
    task: asyncio.Task


# Keyed by (project_id, session_id) rather than a resolved directory: the tenant
# is a project, and two projects must never collide on a session id.
_analysis_tasks: dict = {}
# Concurrency lives in the analysis package: one Chromium at a time for
# reconstruction, a few overlapping Gemini calls. Both gates are shared here.
_cohort_tasks: dict = {}


async def _stop_background_jobs() -> None:
    """On a clean stop, leave no job claiming to be running with no task behind it."""
    pending = [r.task for r in _analysis_tasks.values() if not r.task.done()]
    pending += [t for t in _cohort_tasks.values() if not t.done()]
    for (_project_id, session_id), running in _analysis_tasks.items():
        if not running.task.done():
            ap.set_job(running.project, session_id, "error", error="server restarted mid-analysis")
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        print(f"shutdown: cancelled {len(pending)} background job(s)")


def _analysis_key(project: projects.Project, session_id: str) -> tuple:
    return (project.id, session_id)


def _cohort_key(project: projects.Project, cohort_id: str) -> tuple:
    return (project.id, cohort_id)


def _stage_chunks(project, session_ids: list) -> list:
    staged = []
    for session_id in session_ids:
        directory = project.pool / session_id
        for chunk in storage.list_chunks(project.storage_prefix, session_id):
            path = directory / chunk.name
            if path.exists():
                continue
            body = storage.open_chunk(project.storage_prefix, session_id, chunk.name)
            if body is None:
                continue
            atomic_write_bytes(path, body)
            staged.append(path)
    return staged


def _unstage_chunks(paths: list) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


@asynccontextmanager
async def chunks_on_disk(project, session_ids: list):
    """analyze.py reads chunks as files, and writes analysis.json, the job status
    and the frame cache back into the same directory. Staging into a temp dir
    would carry those results off with it when the directory was cleaned up, so
    chunks are fetched into the real session directory and only the files this
    fetched are removed afterwards. A no-op when chunks are already local."""
    if not storage.configured():
        yield
        return
    staged = await asyncio.to_thread(_stage_chunks, project, list(session_ids))
    try:
        yield
    finally:
        await asyncio.to_thread(_unstage_chunks, staged)


async def _run_analysis_job(
    project: projects.Project, session_id: str, chunk_names: list, force: bool = True
) -> None:
    ap.set_job(project, session_id, "running")
    try:
        async with chunks_on_disk(project, [session_id]):
            await ap.run_session(project, session_id, chunk_names, force=force)
    except Exception as exc:
        ap.set_job(project, session_id, "error", error=str(exc))
    finally:
        _analysis_tasks.pop(_analysis_key(project, session_id), None)


def _require_analysis_runtime() -> None:
    if not ap.enabled():
        raise ApiError(503, "analysis is not configured on this server")
    if not ap.runtime_present():
        raise ApiError(503, "analysis runtime is unavailable on this server")


async def _start_session_analysis(
    project: projects.Project, session_id: str, force: bool = False
) -> dict:
    """Queue (or return) per-session analysis — shared by the single-session API and cohorts."""
    chunks = storage.list_chunks(project.storage_prefix, session_id)
    if not chunks:
        raise ApiError(400, "this session has no chunks to analyze")

    if not force:
        cached = ap.session_analysis(project, session_id)
        if cached:
            if "status" not in cached:
                cached = {**cached, "status": "done"}
            return cached

    key = _analysis_key(project, session_id)
    existing = _analysis_tasks.get(key)
    if existing and not existing.task.done():
        return {"status": "running", "accepted": True}

    job = ap.job(project, session_id)
    if job and job.get("status") == "running" and (existing is None or existing.task.done()):
        # Stale status from a crashed worker — allow a fresh start.
        ap.clear_job(project, session_id)

    if force:
        ap.clear_session_analysis(project, session_id)

    ap.set_job(project, session_id, "running")
    task = asyncio.create_task(
        _run_analysis_job(project, session_id, [c.name for c in chunks], force=True)
    )
    _analysis_tasks[key] = _Running(project, task)
    return {"status": "running", "accepted": True}


async def _await_session_analysis(
    project: projects.Project, session_id: str, force: bool = False
) -> None:
    """Run one session through the shared analysis queue and wait until it settles."""
    result = await _start_session_analysis(project, session_id, force=force)
    if result.get("status") == "done":
        return
    running = _analysis_tasks.get(_analysis_key(project, session_id))
    if running is not None:
        await running.task
    job = ap.job(project, session_id)
    if job and job.get("status") == "error":
        raise RuntimeError(job.get("error") or "analysis failed")
    if not ap.session_analysis(project, session_id):
        raise RuntimeError("analysis finished without a result")


def _cohort_session_ids(cohort: dict) -> list:
    return [
        s["session_id"]
        for s in (cohort.get("sessions") or [])
        if isinstance(s, dict) and s.get("session_id")
    ]


async def _run_cohort_job(project: projects.Project, cohort_id: str, force: bool = False) -> None:
    """Analyze one cohort in the background. Other cohorts are untouched."""
    key = _cohort_key(project, cohort_id)
    try:
        cohort = ap.cohort(project, cohort_id)
        if not cohort:
            return
        async with chunks_on_disk(project, _cohort_session_ids(cohort)):
            await ap.run_cohort(project, cohort, force=force)
    except asyncio.CancelledError:
        cohort = ap.cohort(project, cohort_id)
        if cohort:
            batch_name = (cohort.get("batch") or {}).get("name")
            if batch_name:
                await ap.cancel_batch(batch_name)
            ap.mark_paused(project, cohort)
        raise
    finally:
        _cohort_tasks.pop(key, None)


async def _resume_cohort_batch(project: projects.Project, cohort_id: str) -> None:
    """Re-attach to a Batch job after a process restart."""
    key = _cohort_key(project, cohort_id)
    try:
        cohort = ap.cohort(project, cohort_id)
        if not cohort:
            return
        async with chunks_on_disk(project, _cohort_session_ids(cohort)):
            await ap.resume_cohort(project, cohort)
    finally:
        _cohort_tasks.pop(key, None)


@app.get("/api/sessions/{session_id}/analysis")
def get_analysis(request: Request, session_id: str, project: str | None = Query(None)):
    require_read_access(request, project)
    found = resolve_project(project)
    session_dir = resolve_session(session_id, project)
    cached = ap.session_analysis(found, session_id)
    if cached:
        if "status" not in cached:
            cached = {**cached, "status": "done"}
        info = session_info(session_dir)
        if info:
            if info.get("order"):
                cached = {**cached, "order": info["order"]}
            if info.get("attribution"):
                cached = {**cached, "attribution": info["attribution"]}
            elif info.get("url"):
                attr = parse_attribution(info["url"])
                if attr:
                    cached = {**cached, "attribution": attr}
        return cached
    job = ap.job(found, session_id)
    if job and job.get("status") in ("running", "error"):
        return job
    raise ApiError(404, "not analyzed yet")


@app.post("/api/sessions/{session_id}/analysis")
async def create_analysis(
    request: Request,
    session_id: str,
    project: str | None = Query(None),
    force: bool = Query(False),
):
    require_read_access(request, project)
    _require_analysis_runtime()
    found = resolve_project(project)
    resolve_session(session_id, project)
    result = await _start_session_analysis(found, session_id, force=force)
    if result.get("accepted"):
        return JSONResponse({"status": "running"}, status_code=202)
    return result


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

    key = _cohort_key(found, cohort_id)
    task = _cohort_tasks.get(key)
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
    _require_analysis_runtime()
    found = resolve_project(project)
    cohort = _resolve_cohort_or_404(found, cohort_id)

    key = _cohort_key(found, cohort["id"])
    existing = _cohort_tasks.get(key)
    if existing and not existing.done():
        return JSONResponse(
            cohort_response(found, ap.cohort_view(found, cohort)),
            status_code=202,
        )

    ap.save_cohort(found, {**cohort, "status": "running"})
    task = asyncio.create_task(_run_cohort_job(found, cohort["id"], force=force))
    _cohort_tasks[key] = task
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

    key = _cohort_key(found, cohort["id"])
    task = _cohort_tasks.get(key)
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
    _require_analysis_runtime()
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

    key = _cohort_key(found, cohort["id"])
    existing = _cohort_tasks.get(key)
    if existing and not existing.done():
        # A cohort run owns the file; let it pick this row up on its own pass.
        await _start_session_analysis(found, session_id)
    else:
        task = asyncio.create_task(_retry_cohort_session_job(found, cohort["id"], session_id))
        _cohort_tasks[key] = task

    live = ap.cohort(found, cohort["id"]) or cohort
    return JSONResponse(
        cohort_response(found, ap.cohort_view(found, live)),
        status_code=202,
    )


async def _retry_cohort_session_job(
    found: projects.Project, cohort_id: str, session_id: str
) -> None:
    key = _cohort_key(found, cohort_id)
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
            await _await_session_analysis(found, session_id)
            row["status"] = "done"
            row["analysis"] = ap.ANALYSIS_FILE
            row.pop("error", None)
        except Exception as exc:
            row["status"] = "error"
            row["error"] = getattr(exc, "message", None) or str(exc) or "analysis failed"
        ap.save_cohort(found, ap.storable_cohort(ap.cohort_view(found, cohort), cohort))
    finally:
        _cohort_tasks.pop(key, None)


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

    _require_analysis_runtime()
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
        _bytes_used.clear()
    return removed


app.mount("/", StaticFiles(directory=paths.BASE_DIR / "public", html=True), name="static")
