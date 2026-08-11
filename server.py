import asyncio
import base64
import hashlib
import hmac
import json
import os
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
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
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

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

import analyze
import compat
import db
import projects
import sessionstore
import storage
from projects import ProjectsUnavailable
from fsutil import atomic_write_json, atomic_write_text

@asynccontextmanager
async def lifespan(_app):

    if REPLAY_AUTH_DISABLED:
        print(
            "warning: REPLAY_AUTH_DISABLED=1 — the replay UI and read API are "
            "open to anyone who can reach this server"
        )
    elif WORKOS_ENABLED:
        print(
            "replay auth: WorkOS per-user logins; projects are visible to "
            "their org_id members" + (" and operators" if OPERATORS else " (no operators set)")
        )
    elif not REPLAY_PASSWORD:
        print(
            "REPLAY_PASSWORD is not set — the replay UI and read API will "
            "return 503 until it is"
        )

    if READ_ONLY:
        print(
            "CAPTURE_READ_ONLY=1 — recording is refused and retention is disabled; "
            "this server only reads the database it is pointed at"
        )

    async def loop():
        while True:
            removed = await asyncio.to_thread(sweep_expired_sessions)
            if removed:
                print(f"retention: removed {removed} expired session(s)")
            await asyncio.sleep(24 * 3600)

    task = None if READ_ONLY else asyncio.create_task(loop())
    # Cohorts mid-Batch lose their asyncio task on restart — pick them back up.
    for pool, cohort in analyze.cohorts_needing_batch_resume(DATA_ROOT):
        key = _cohort_key(pool, cohort["id"])
        if key not in _cohort_tasks:
            print(f"resuming cohort batch {cohort['id']} ({(cohort.get('batch') or {}).get('name')})")
            _cohort_tasks[key] = asyncio.create_task(
                _resume_cohort_batch(pool, cohort["id"])
            )
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
    if protected and not REPLAY_AUTH_DISABLED:
        if WORKOS_ENABLED:
            session = read_session(request.cookies.get(SESSION_COOKIE))

            if not session or not session.get("user_id"):
                if path == "/replay.html":
                    return RedirectResponse("/login", status_code=302)
                return JSONResponse(
                    {"error": "login required", "login": "/login"}, status_code=401
                )
            request.state.auth = session
        elif not REPLAY_PASSWORD:
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
            if not secrets.compare_digest(supplied, REPLAY_PASSWORD):
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

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")
DATA_DIR = DATA_ROOT / "sessions"
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROJECTS_PATH = DATA_ROOT / "projects.json"
PROJECTS_DIR = DATA_ROOT / "projects"

REPLAY_PASSWORD = os.environ.get("REPLAY_PASSWORD", "")
REPLAY_AUTH_DISABLED = os.environ.get("REPLAY_AUTH_DISABLED") == "1"

WORKOS_API_KEY = os.environ.get("WORKOS_API_KEY", "")
WORKOS_CLIENT_ID = os.environ.get("WORKOS_CLIENT_ID", "")
WORKOS_ENABLED = bool(WORKOS_API_KEY and WORKOS_CLIENT_ID)
WORKOS_API = os.environ.get("WORKOS_API") or "https://api.workos.com"
ORG_CACHE_TTL_S = float(os.environ.get("ORG_CACHE_TTL_S", "60"))

OPERATORS = {e.strip().lower() for e in os.environ.get("CAPTURE_OPERATORS", "").split(",") if e.strip()}

SESSION_SECRET = os.environ.get("SESSION_SECRET") or hashlib.sha256(
    f"session:{WORKOS_API_KEY}".encode()
).hexdigest()
SESSION_TTL_S = 7 * 24 * 3600
SESSION_COOKIE = "capture_session"
STATE_COOKIE = "capture_login_state"

def _sign(raw: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()

def make_session(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{raw}.{_sign(raw)}"

def read_session(cookie: Optional[str]) -> Optional[dict]:
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

def require_allowed_domain(request: Request, project: Optional[projects.Project]) -> None:
    domains = list(project.domains) if project else []
    if not domains:
        return
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    host = (urllib.parse.urlparse(source).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in domains):
        raise ApiError(403, "domain not allowed for this project")

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SAFE_CHUNK = re.compile(r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$")

def chunk_window(path) -> Optional[str]:
    parts = path.name.rsplit(".", 1)[0].split("-")
    return parts[1] if len(parts) == 3 else None
MAX_BODY_BYTES = 50 * 1024 * 1024

MAX_DECOMPRESSED_BYTES = int(os.environ.get("MAX_DECOMPRESSED_BYTES", 32 * 1024 * 1024))

MAX_PROJECT_BYTES = int(os.environ.get("MAX_PROJECT_BYTES", 2 * 1024**3))
MIN_FREE_DISK_BYTES = int(os.environ.get("MIN_FREE_DISK_BYTES", 1024**3))

RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))

# A mirror pointed at someone else's database — staging reading production.
# Recording and retention are refused outright rather than left to fail on a
# permission error: the sweep would otherwise try to DELETE rows it can see but
# does not own, which is production data one careless GRANT away from deletion.
READ_ONLY = os.environ.get("CAPTURE_READ_ONLY") == "1"

def gunzip(body: bytes, limit: int) -> bytes:

    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = stream.decompress(body, limit)
    if stream.unconsumed_tail:
        raise ValueError("decompressed payload too large")
    return raw

def dir_bytes(path: Path) -> int:
    # Only recorded chunks count toward the ingest quota. Analysis output is
    # derived, is bounded per session, and must never throttle recording.
    return sum(
        f.stat().st_size
        for f in path.rglob("*.json")
        if f.is_file() and SAFE_CHUNK.match(f.name)
    )

_bytes_used: dict = {}

def project_bytes_used(project) -> int:
    """Cached so the quota check is not a query per ingest; the running total is
    maintained in SQL and re-read only on a cold cache."""
    if project.id not in _bytes_used:
        _bytes_used[project.id] = sessionstore.project_bytes(project.id)
    return _bytes_used[project.id]

CONFIG_PATH = DATA_ROOT / "config.json"

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message

@app.exception_handler(ApiError)
async def api_error_response(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)

@app.exception_handler(ProjectsUnavailable)
async def projects_unavailable_response(_request, exc: ProjectsUnavailable) -> JSONResponse:
    print(f"projects: refusing to serve — {exc}")
    return JSONResponse({"error": "project configuration unavailable"}, status_code=503)

def resolve_project(handle: Optional[str]) -> projects.Project:
    found = projects.by_handle(handle)
    if found is None:
        raise ApiError(404, "unknown project")
    return found

def resolve_pool(project: Optional[str]) -> Path:
    return resolve_project(project).pool

def resolve_session(session_id: str, project: Optional[str]) -> Path:
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
    compression: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
):
    if READ_ONLY:
        raise ApiError(403, "this server is read-only and does not accept recordings")
    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")

    project = projects.by_token(token)
    if project is None:
        raise ApiError(401, "unknown project token")
    require_allowed_domain(request, project)
    pool = project.pool

    if MIN_FREE_DISK_BYTES and shutil.disk_usage(DATA_ROOT).free < MIN_FREE_DISK_BYTES:
        raise ApiError(507, "server storage full")
    if MAX_PROJECT_BYTES and project_bytes_used(project) >= MAX_PROJECT_BYTES:
        raise ApiError(429, "project storage quota reached")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(413, "payload too large")

    try:
        raw = gunzip(body, MAX_DECOMPRESSED_BYTES) if compression == "gzip-js" else body
        payload = json.loads(raw)
    except Exception:
        raise ApiError(400, "bad payload")

    if not isinstance(payload, dict):
        raise ApiError(400, "bad payload")

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise ApiError(400, "no events")

    session_dir = pool / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    meta = {}
    visitor, url = payload.get("visitor_id"), payload.get("url")
    if isinstance(visitor, str) and SAFE_ID.match(visitor):
        meta["visitor_id"] = visitor
    if isinstance(url, str) and url:
        meta["url"] = url[:2048]
    meta_path = session_dir / VISITOR_FILE
    if meta and not meta_path.is_file():
        atomic_write_json(meta_path, meta)

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
    storage.put_chunk(project.storage_prefix, session_id, chunk_name, stored)
    clicks, rage = count_clicks(events)
    device = read_device(events)
    size = len(stored)
    first_ts, last_ts = event_span(events) or (now, now)

    window_id = str(payload.get("window_id") or "default")
    state = sessionstore.window_state(project.id, session_id, window_id)
    active_ts = last_active_scan(events, state)
    if active_ts:
        state["active"] = max(state.get("active", 0), active_ts)
    state["first"] = min(state.get("first", first_ts), first_ts)
    state["last"] = max(state.get("last", last_ts), last_ts)

    sessionstore.record_chunk(
        project.id, session_id,
        window_id=window_id, first_ts=first_ts, last_ts=last_ts, active_ts=active_ts,
        size=size, meta=meta, clicks=clicks, rage=rage, device=device,
        attribution=parse_attribution(meta.get("url")), state=state,
    )
    _bytes_used[project.id] = _bytes_used.get(project.id, 0) + size

    return {"status": "ok", "stored": len(events)}

ORDER_FILE = "order.json"
ORDER_MAX_BODY_BYTES = 4096

VISITOR_FILE = "visitor.json"

INTERACTIONS_FILE = "interactions.json"

DEVICE_FILE = "device.json"

def read_device(events: list) -> Optional[dict]:
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
    token: Optional[str] = Query(None),
):

    if READ_ONLY:
        raise ApiError(403, "this server is read-only and does not accept recordings")
    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")
    project = projects.by_token(token)
    if project is None:
        raise ApiError(401, "unknown project token")
    session_dir = project.pool / session_id

    body = await request.body()
    if len(body) > ORDER_MAX_BODY_BYTES:
        raise ApiError(413, "payload too large")
    try:
        payload = json.loads(body)
    except Exception:
        raise ApiError(400, "bad payload")
    if not isinstance(payload, dict):
        raise ApiError(400, "bad payload")
    event = payload.get("event")
    if event not in ("checkout_started", "checkout_completed"):
        raise ApiError(400, "unknown event")

    now = int(time.time() * 1000)
    order = sessionstore.record_order(project.id, session_id, event, payload, now)
    if order is None:
        raise ApiError(404, "unknown session")
    # analyze.py reads order.json straight off disk, so it stays written until
    # those five functions move to Postgres. Postgres is the record; this mirrors it.
    atomic_write_json(session_dir / ORDER_FILE, order)
    return {"status": "ok"}

@app.get("/healthz")
def healthz():

    checks = {}

    try:
        probe = DATA_ROOT / ".healthz"
        atomic_write_text(probe, "ok")
        probe.unlink()
        checks["storage_writable"] = True
    except Exception:
        checks["storage_writable"] = False

    try:
        free = shutil.disk_usage(DATA_ROOT).free
        checks["disk_above_floor"] = not MIN_FREE_DISK_BYTES or free >= MIN_FREE_DISK_BYTES
    except Exception:
        checks["disk_above_floor"] = False

    try:
        load_projects()
        checks["projects_readable"] = True
    except ProjectsUnavailable:
        checks["projects_readable"] = False

    checks["bundles_present"] = all(
        (BASE_DIR / "public" / name).is_file()
        for name in ("capture.js", "recorder.bundle.js", "replay.bundle.js")
    )
    checks["replay_auth_enabled"] = bool(REPLAY_PASSWORD or WORKOS_ENABLED) and not REPLAY_AUTH_DISABLED
    # Reported, not health-gating: analysis is an optional extra, and a box
    # without a key or a browser is still a perfectly healthy recorder.
    checks["read_only"] = READ_ONLY
    checks["analysis_configured"] = analyze.ANALYSIS_ENABLED
    checks["analysis_runtime_present"] = analyze.runtime_present()

    informational = (
        "replay_auth_enabled", "read_only", "analysis_configured", "analysis_runtime_present",
    )
    healthy = all(v for k, v in checks.items() if k not in informational)
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )

@app.get("/config")
def remote_config(token: Optional[str] = Query(None)):

    project = projects.by_token(token) if token else None
    if project is not None and project.config:
        response = JSONResponse(project.config)
        response.headers["Cache-Control"] = "no-cache"
        return response
    config_path = CONFIG_PATH
    if project is not None and project.storage_prefix:
        project_config = PROJECTS_DIR / project.storage_prefix / "config.json"
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

def chunk_timestamp(path: Path) -> int:
    try:
        return int(path.name.split("-")[0])
    except ValueError:
        return 0

ACTIVE_SOURCES = {1, 2, 3, 4, 5, 6, 7, 12}
SNAPSHOT_META_WINDOW_MS = 500
WINDOW_STATE_FILE = "window-state.json"

def last_active_scan(events: list, state: dict) -> Optional[int]:
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
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
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

def event_span(events: list) -> Optional[tuple]:
    ts = [
        e["timestamp"]
        for e in events
        if isinstance(e, dict) and isinstance(e.get("timestamp"), (int, float))
    ]
    return (int(min(ts)), int(max(ts))) if ts else None

def window_duration(states: dict) -> Optional[int]:
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
        key=chunk_timestamp,
    )

def read_pool(project: Optional[str]) -> Optional[Path]:
    found = projects.by_handle(project)
    return found.pool if found else None

ATTRIBUTION_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "fbclid")

def parse_attribution(url: Optional[str]) -> Optional[dict]:
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
    out = {}
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

def session_info(entry: Path) -> Optional[dict]:
    chunks = chunk_files(entry)
    if not chunks:
        return None
    states, first, last, active = session_states(entry)
    info = {
        "session_id": entry.name,
        "first_seen": first if first is not None else chunk_timestamp(chunks[0]),
        "last_seen": last if last is not None else chunk_timestamp(chunks[-1]),
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

def workos_call(path: str, payload: Optional[dict] = None, bearer: bool = False) -> dict:
    req = urllib.request.Request(
        WORKOS_API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {WORKOS_API_KEY}"} if bearer else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)

def workos_authenticate(code: str) -> tuple:
    auth = workos_call(
        "/user_management/authenticate",
        {
            "client_id": WORKOS_CLIENT_ID,
            "client_secret": WORKOS_API_KEY,
            "grant_type": "authorization_code",
            "code": code,
        },
    )
    user = auth.get("user") or {}
    return (user.get("email") or "").lower(), user.get("id") or ""

_org_cache: dict = {}

def user_orgs(user_id: str) -> set:
    now = time.time()
    cached = _org_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        memberships = workos_call(
            "/user_management/organization_memberships"
            f"?user_id={urllib.parse.quote(user_id)}&statuses=active&limit=100",
            bearer=True,
        )
        orgs = {m["organization_id"] for m in memberships.get("data", []) if m.get("organization_id")}
    except Exception as exc:
        print(f"auth: membership lookup failed for {user_id}: {exc}")
        if cached:
            return cached[1]
        raise ApiError(503, "could not verify project access — try again shortly")
    _org_cache[user_id] = (now + ORG_CACHE_TTL_S, orgs)
    return orgs

@app.get("/login")
def login():
    if not WORKOS_ENABLED:
        return Response(
            "Per-user login is not configured; set WORKOS_API_KEY and WORKOS_CLIENT_ID.\n",
            status_code=503,
            media_type="text/plain",
        )
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": WORKOS_CLIENT_ID,
            "redirect_uri": public_base_url() + "/auth/callback",
            "response_type": "code",
            "provider": "authkit",
            "state": state,
        }
    )
    response = RedirectResponse(f"{WORKOS_API}/user_management/authorize?{query}", status_code=302)
    response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=True, samesite="lax")
    return response

@app.get("/auth/callback")
def auth_callback(request: Request, code: Optional[str] = Query(None), state: Optional[str] = Query(None)):
    if not WORKOS_ENABLED:
        raise ApiError(503, "login not configured")
    if not code or not state or state != request.cookies.get(STATE_COOKIE):

        return RedirectResponse("/login", status_code=302)
    try:
        email, user_id = workos_authenticate(code)
    except Exception as exc:
        print(f"auth: WorkOS code exchange failed: {exc}")
        raise ApiError(401, "login failed — start again at /login")
    if not user_id:
        raise ApiError(401, "login failed — start again at /login")
    session = make_session({"email": email, "user_id": user_id, "exp": int(time.time()) + SESSION_TTL_S})
    response = RedirectResponse("/replay.html", status_code=302)
    response.set_cookie(
        SESSION_COOKIE, session, max_age=SESSION_TTL_S, httponly=True, secure=True, samesite="lax"
    )
    response.delete_cookie(STATE_COOKIE)
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response

def allowed_projects(request: Request) -> Optional[set]:
    auth = getattr(request.state, "auth", None)
    if auth is None or (auth.get("email") or "").lower() in OPERATORS:
        return None
    orgs = user_orgs(auth.get("user_id") or "")
    return {p.handle for p in projects.for_orgs(orgs)}

def require_read_access(request: Request, project: Optional[str]) -> None:
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
        "analysis_enabled": analyze.ANALYSIS_ENABLED and analyze.runtime_present(),
        **({"email": email} if email else {}),
    }

SESSION_FILTERS = sessionstore.FILTERS

@app.get("/api/sessions")
def list_sessions(
    request: Request,
    project: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    cursor: Optional[str] = Query(None),
    filter: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    require_read_access(request, project)
    try:
        return sessionstore.listing(
            resolve_project(project).id, limit=limit, cursor=cursor, filter=filter, q=q
        )
    except ValueError:
        raise ApiError(400, "bad cursor")

@app.get("/api/sessions/{session_id}")
def get_session(request: Request, session_id: str, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    found = resolve_project(project)
    info = sessionstore.get(found.id, session_id) or session_info(
        resolve_session(session_id, project)
    )
    if info is None:
        raise ApiError(404, "not found")
    return info

@app.delete("/api/sessions/{session_id}")
def delete_session(request: Request, session_id: str, project: Optional[str] = Query(None)):

    require_read_access(request, project)
    found = resolve_project(project)
    session_dir = resolve_session(session_id, project)
    freed = storage.delete_session(found.storage_prefix, session_id)
    shutil.rmtree(session_dir, ignore_errors=True)
    sessionstore.forget(found.id, session_id)
    # Cohort membership references a recording that no longer exists. Left
    # behind, enrich_cohort_status falls back to the stored status and keeps
    # counting the session as analyzed.
    try:
        analyze.forget_session(found.pool, session_id)
    except Exception as exc:
        # The session is already gone from the index; a failure here leaves a
        # stale membership row, which is bad but not worth failing the delete.
        print(f"cohort cleanup after forgetting {session_id}: {exc}")
    _bytes_used.pop(found.id, None)
    print(f"deleted session {session_id} ({freed} bytes)")
    return {"status": "deleted", "session_id": session_id, "bytes_freed": freed}

@app.get("/api/sessions/{session_id}/chunks")
def session_chunks(request: Request, session_id: str, project: Optional[str] = Query(None)):
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
            "timestamp": storage.chunk_timestamp(c.name),
            "bytes": c.size,
            "window": storage.chunk_window(c.name),
        }
        for c in chunks
    ]
    windows = list(dict.fromkeys(c["window"] for c in manifest))
    return {"session_id": session_id, "url": url, "windows": windows, "chunks": manifest}

@app.get("/api/sessions/{session_id}/chunks/{chunk_name}")
def session_chunk(request: Request, session_id: str, chunk_name: str, project: Optional[str] = Query(None)):
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
        return RedirectResponse(
            signed, status_code=307, headers={"Cache-Control": "no-store"}
        )

    body = storage.open_chunk(prefix, session_id, chunk_name)
    if body is None:
        raise ApiError(404, "not found")
    return Response(
        body,
        media_type="application/json",
        headers={"Cache-Control": storage.CHUNK_CACHE_CONTROL},
    )

_analysis_tasks: dict = {}
# Concurrency lives in analyze.py: one Chromium at a time for reconstruction,
# a few overlapping Gemini calls. Both gates are shared by every caller here.
_cohort_tasks: dict = {}

async def _stop_background_jobs() -> None:
    """On a clean stop, leave no job claiming to be running with no task behind it."""
    pending = [t for t in (*_analysis_tasks.values(), *_cohort_tasks.values()) if not t.done()]
    for key, task in _analysis_tasks.items():
        if not task.done():
            analyze.write_job(Path(key), "error", error="server restarted mid-analysis")
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        print(f"shutdown: cancelled {len(pending)} background job(s)")

def _analysis_key(session_dir: Path) -> str:
    return str(session_dir.resolve())

def _cohort_key(pool: Path, cohort_id: str) -> str:
    return f"{pool.resolve()}::{cohort_id}"

async def _run_analysis_job(session_dir: Path, chunk_names: list, force: bool = True) -> None:
    analyze.write_job(session_dir, "running")
    try:
        await analyze.analyze_session(session_dir, chunk_names, force=force)
    except Exception as exc:
        analyze.write_job(session_dir, "error", error=str(exc))
    finally:
        _analysis_tasks.pop(_analysis_key(session_dir), None)

def _require_analysis_runtime() -> None:
    if not analyze.ANALYSIS_ENABLED:
        raise ApiError(503, "analysis is not configured on this server")
    if not analyze.runtime_present():
        raise ApiError(503, "analysis runtime is unavailable on this server")

async def _start_session_analysis(session_dir: Path, force: bool = False) -> dict:
    """Queue (or return) per-session analysis — shared by the single-session API and cohorts."""
    chunks = chunk_files(session_dir)
    if not chunks:
        raise ApiError(400, "this session has no chunks to analyze")

    if not force:
        cached = analyze.read_analysis(session_dir)
        if cached:
            if "status" not in cached:
                cached = {**cached, "status": "done"}
            return cached

    key = _analysis_key(session_dir)
    existing = _analysis_tasks.get(key)
    if existing and not existing.done():
        return {"status": "running", "accepted": True}

    job = analyze.read_job(session_dir)
    if job and job.get("status") == "running" and (existing is None or existing.done()):
        # Stale status from a crashed worker — allow a fresh start.
        analyze.clear_job(session_dir)

    if force:
        analyze.clear_analysis(session_dir)

    analyze.write_job(session_dir, "running")
    task = asyncio.create_task(
        _run_analysis_job(session_dir, [c.name for c in chunks], force=True)
    )
    _analysis_tasks[key] = task
    return {"status": "running", "accepted": True}

async def _await_session_analysis(session_dir: Path, force: bool = False) -> None:
    """Run one session through the shared analysis queue and wait until it settles."""
    result = await _start_session_analysis(session_dir, force=force)
    if result.get("status") == "done":
        return
    key = _analysis_key(session_dir)
    task = _analysis_tasks.get(key)
    if task is not None:
        await task
    job = analyze.read_job(session_dir)
    if job and job.get("status") == "error":
        raise RuntimeError(job.get("error") or "analysis failed")
    if not analyze.read_analysis(session_dir):
        raise RuntimeError("analysis finished without a result")

async def _run_cohort_job(pool: Path, cohort_id: str, force: bool = False) -> None:
    """Analyze one cohort in the background. Other cohorts on disk are untouched."""
    key = _cohort_key(pool, cohort_id)
    try:
        cohort = analyze.read_cohort(pool, cohort_id)
        if not cohort:
            return
        await analyze.run_cohort_job(pool, cohort, force=force)
    except asyncio.CancelledError:
        cohort = analyze.read_cohort(pool, cohort_id)
        if cohort:
            batch_name = (cohort.get("batch") or {}).get("name")
            if batch_name:
                await analyze.cancel_gemini_batch(batch_name)
            analyze.mark_cohort_paused(pool, cohort)
        raise
    finally:
        _cohort_tasks.pop(key, None)


async def _resume_cohort_batch(pool: Path, cohort_id: str) -> None:
    """Re-attach to a Batch job after a process restart."""
    key = _cohort_key(pool, cohort_id)
    try:
        cohort = analyze.read_cohort(pool, cohort_id)
        if not cohort:
            return
        await analyze.resume_cohort_batch(pool, cohort)
    finally:
        _cohort_tasks.pop(key, None)

@app.get("/api/sessions/{session_id}/analysis")
def get_analysis(request: Request, session_id: str, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    session_dir = resolve_session(session_id, project)
    cached = analyze.read_analysis(session_dir)
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
    job = analyze.read_job(session_dir)
    if job and job.get("status") in ("running", "error"):
        return job
    raise ApiError(404, "not analyzed yet")


@app.post("/api/sessions/{session_id}/analysis")
async def create_analysis(
    request: Request,
    session_id: str,
    project: Optional[str] = Query(None),
    force: bool = Query(False),
):
    require_read_access(request, project)
    _require_analysis_runtime()
    session_dir = resolve_session(session_id, project)
    result = await _start_session_analysis(session_dir, force=force)
    if result.get("accepted"):
        return JSONResponse({"status": "running"}, status_code=202)
    return result

def _resolve_cohort_or_404(pool: Path, cohort_id: Optional[str]) -> dict:
    """A cohort by id, or the newest one when the caller did not name one."""
    if cohort_id:
        cohort = analyze.read_cohort(pool, cohort_id)
        if not cohort:
            raise ApiError(404, "no such cohort")
        return cohort
    analyze.migrate_legacy_cohort(pool)
    cohort = analyze.latest_cohort(pool)
    if not cohort:
        raise ApiError(404, "no cohort yet")
    return cohort


@app.get("/api/cohorts")
def list_cohorts(request: Request, project: Optional[str] = Query(None)):
    """Every cohort in this pool, newest first — membership counts only."""
    require_read_access(request, project)
    pool = resolve_pool(project)
    out = []
    for cohort in analyze.list_cohorts(pool):
        live = analyze.enrich_cohort_status(pool, cohort)
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
def get_cohort(request: Request, cohort_id: str, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    pool = resolve_pool(project)
    cohort = _resolve_cohort_or_404(pool, cohort_id)
    live = analyze.enrich_cohort_status(pool, cohort)
    # Persist refreshed status so a crashed runner still shows accurate counts.
    # Status only — persistable_cohort drops the derived view enrich lays over
    # it, so a poll cannot freeze a guessed `selection` into the file.
    if live.get("sessions") != cohort.get("sessions") or live.get("status") != cohort.get("status"):
        analyze.write_cohort(pool, analyze.persistable_cohort(live, cohort))
    return cohort_response(pool, live)


@app.patch("/api/cohorts/{cohort_id}")
async def rename_cohort(request: Request, cohort_id: str, project: Optional[str] = Query(None)):
    """Rename a cohort. Membership is frozen — only the label is editable."""
    require_read_access(request, project)
    pool = resolve_pool(project)
    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "bad payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("label"), str):
        raise ApiError(400, "label is required")
    try:
        renamed = analyze.rename_cohort(pool, cohort_id, payload["label"])
    except ValueError as exc:
        raise ApiError(400, str(exc))
    if not renamed:
        raise ApiError(404, "no such cohort")
    return cohort_response(pool, analyze.enrich_cohort_status(pool, renamed))


@app.delete("/api/cohorts/{cohort_id}")
async def delete_cohort(request: Request, cohort_id: str, project: Optional[str] = Query(None)):
    """Delete a cohort and its cached cuts. Per-session write-ups are kept."""
    require_read_access(request, project)
    pool = resolve_pool(project)
    cohort = analyze.read_cohort(pool, cohort_id)
    if not cohort:
        raise ApiError(404, "no such cohort")

    key = _cohort_key(pool, cohort_id)
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
            await analyze.cancel_gemini_batch(batch_name)

    # Drop any in-flight cut for this cohort.
    prefix = f"{key}:"
    for cut_key, cut_task in list(_cut_tasks.items()):
        if cut_key.startswith(prefix) and cut_task and not cut_task.done():
            cut_task.cancel()

    if not analyze.delete_cohort(pool, cohort_id):
        raise ApiError(404, "no such cohort")
    return {"ok": True, "id": cohort_id}


@app.get("/api/facets")
def get_pool_facets(
    request: Request,
    project: Optional[str] = Query(None),
    min_duration_ms: int = Query(analyze.COHORT_MIN_DURATION_MS, ge=0),
):
    """Facet values available in the pool — same fields as the Insights cut picker."""
    require_read_access(request, project)
    found = resolve_project(project)
    with compat.cohort_connection(found.id) as conn:
        return analyze.pool_facets(conn, found.id, min_duration_ms=min_duration_ms)


@app.post("/api/cohorts")
async def create_cohort(
    request: Request,
    project: Optional[str] = Query(None),
    purchased: Optional[int] = Query(None, ge=0, le=analyze.COHORT_MAX_SESSIONS),
    all_n: Optional[int] = Query(None, ge=0, le=analyze.COHORT_MAX_SESSIONS, alias="all"),
    any_n: int = Query(0, ge=0, le=analyze.COHORT_MAX_SESSIONS, alias="any"),
    a_n: int = Query(0, ge=0, le=analyze.COHORT_MAX_SESSIONS, alias="a"),
    b_n: int = Query(0, ge=0, le=analyze.COHORT_MAX_SESSIONS, alias="b"),
    min_duration_ms: int = Query(analyze.COHORT_MIN_DURATION_MS, ge=0),
    url_contains: str = Query(analyze.COHORT_URL_CONTAINS),
    start_ms: Optional[int] = Query(None, ge=0),
    end_ms: Optional[int] = Query(None, ge=0),
    order: str = Query(analyze.COHORT_ORDER_LATEST),
    label: Optional[str] = Query(None, max_length=120),
    outcome: Optional[list] = Query(None),
    device: Optional[list] = Query(None),
    page: Optional[str] = Query(None),
    utm_source: Optional[str] = Query(None),
    utm_medium: Optional[str] = Query(None),
    utm_campaign: Optional[str] = Query(None),
    utm_content: Optional[str] = Query(None),
    fbclid: Optional[str] = Query(None),
    b_outcome: Optional[list] = Query(None),
    b_device: Optional[list] = Query(None),
    b_page: Optional[str] = Query(None),
    b_utm_source: Optional[str] = Query(None),
    b_utm_medium: Optional[str] = Query(None),
    b_utm_campaign: Optional[str] = Query(None),
    b_utm_content: Optional[str] = Query(None),
    b_fbclid: Optional[str] = Query(None),
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
    pool = resolve_pool(project)

    sort = (order or "").strip().lower()
    if sort not in analyze.COHORT_ORDERS:
        raise ApiError(400, "order must be latest or earliest")
    if start_ms is not None and end_ms is not None and start_ms > end_ms:
        raise ApiError(400, "start_ms must be <= end_ms")
    chosen_label = analyze.clean_cohort_label(label)

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
        purchased_n = (
            analyze.COHORT_DEFAULT_PURCHASED_N if purchased is None else purchased
        )
        all_count = analyze.COHORT_DEFAULT_ALL_N if all_n is None else all_n
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
    if total > analyze.COHORT_MAX_SESSIONS:
        raise ApiError(400, f"cohort size capped at {analyze.COHORT_MAX_SESSIONS} sessions")

    with compat.cohort_connection(resolve_project(project).id) as conn:
        cohort = analyze.build_cohort(
            conn,
            resolve_project(project).id,
            purchased_n,
            all_count,
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

    analyze.write_cohort(pool, cohort)
    return JSONResponse(
        cohort_response(pool, analyze.enrich_cohort_status(pool, cohort)),
        status_code=201,
    )


@app.post("/api/cohorts/{cohort_id}/analyze")
async def start_cohort_analysis(
    request: Request,
    cohort_id: str,
    project: Optional[str] = Query(None),
    force: bool = Query(False),
):
    """Start (or resume) analysis for an existing idle/paused cohort."""
    require_read_access(request, project)
    _require_analysis_runtime()
    pool = resolve_pool(project)
    cohort = _resolve_cohort_or_404(pool, cohort_id)

    key = _cohort_key(pool, cohort["id"])
    existing = _cohort_tasks.get(key)
    if existing and not existing.done():
        return JSONResponse(
            cohort_response(pool, analyze.enrich_cohort_status(pool, cohort)),
            status_code=202,
        )

    analyze.write_cohort(pool, {**cohort, "status": "running"})
    task = asyncio.create_task(_run_cohort_job(pool, cohort["id"], force=force))
    _cohort_tasks[key] = task
    live = analyze.read_cohort(pool, cohort["id"]) or cohort
    return JSONResponse(
        cohort_response(pool, analyze.enrich_cohort_status(pool, live)),
        status_code=202,
    )


@app.post("/api/cohorts/{cohort_id}/pause")
async def pause_cohort_analysis(
    request: Request,
    cohort_id: str,
    project: Optional[str] = Query(None),
):
    """Pause an in-flight cohort analysis; completed session write-ups are kept."""
    require_read_access(request, project)
    pool = resolve_pool(project)
    cohort = _resolve_cohort_or_404(pool, cohort_id)

    key = _cohort_key(pool, cohort["id"])
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
            await analyze.cancel_gemini_batch(batch_name)
        analyze.mark_cohort_paused(pool, cohort)

    live = analyze.read_cohort(pool, cohort_id) or cohort
    return cohort_response(pool, analyze.enrich_cohort_status(pool, live))


@app.post("/api/cohorts/{cohort_id}/sessions/{session_id}/retry")
async def retry_cohort_session(
    request: Request,
    cohort_id: str,
    session_id: str,
    project: Optional[str] = Query(None),
):
    """Re-run one cohort row after it failed, without re-sampling the cohort."""
    require_read_access(request, project)
    _require_analysis_runtime()
    pool = resolve_pool(project)
    cohort = _resolve_cohort_or_404(pool, cohort_id)

    entry = next(
        (e for e in cohort.get("sessions") or [] if e.get("session_id") == session_id),
        None,
    )
    if entry is None:
        raise ApiError(404, "session is not in this cohort")

    session_dir = resolve_session(session_id, project)
    # Drop the recorded failure first, so a poll landing mid-retry does not
    # re-read the old error back over the running status.
    entry["status"] = "queued"
    entry.pop("error", None)
    analyze.clear_job(session_dir)
    analyze.write_cohort(pool, {**cohort, "status": "running"})

    key = _cohort_key(pool, cohort["id"])
    existing = _cohort_tasks.get(key)
    if existing and not existing.done():
        # A cohort run owns the file; let it pick this row up on its own pass.
        await _start_session_analysis(session_dir)
    else:
        task = asyncio.create_task(
            _retry_cohort_session_job(pool, cohort["id"], session_id, session_dir)
        )
        _cohort_tasks[key] = task

    live = analyze.read_cohort(pool, cohort["id"]) or cohort
    return JSONResponse(
        cohort_response(pool, analyze.enrich_cohort_status(pool, live)),
        status_code=202,
    )


async def _retry_cohort_session_job(
    pool: Path, cohort_id: str, session_id: str, session_dir: Path
) -> None:
    key = _cohort_key(pool, cohort_id)
    try:
        cohort = analyze.read_cohort(pool, cohort_id) or {}
        row = next(
            (e for e in cohort.get("sessions") or [] if e.get("session_id") == session_id),
            None,
        )
        if row is None:
            return
        row["status"] = "running"
        row.pop("error", None)
        analyze.write_cohort(pool, cohort)
        try:
            # Not forced: a failed row has nothing cached to bust, and if it
            # succeeded meanwhile the retry should settle on that result.
            await _await_session_analysis(session_dir)
            row["status"] = "done"
            row["analysis"] = analyze.ANALYSIS_FILE
            row.pop("error", None)
        except Exception as exc:
            row["status"] = "error"
            row["error"] = getattr(exc, "message", None) or str(exc) or "analysis failed"
        analyze.write_cohort(
            pool,
            analyze.persistable_cohort(
                analyze.enrich_cohort_status(pool, cohort), cohort
            ),
        )
    finally:
        _cohort_tasks.pop(key, None)


_cut_tasks: dict = {}
# Failures are kept in memory, not in the cut cache: caching an error would
# make a transient API blip permanent for that cut, and the cache has no
# invalidation path by design.
_cut_errors: dict = {}


def _cut_task_key(pool: Path, cohort_id: str, key: str) -> str:
    return f"{pool.resolve()}::{cohort_id}::{key}"


async def _run_cut_job(pool: Path, cohort_id: str, rows: list, slice_spec, key: str) -> None:
    task_key = _cut_task_key(pool, cohort_id, key)
    try:
        cut = await analyze.build_cut_analysis(rows, slice_spec)
        analyze.write_cut(pool, cohort_id, key, cut)
        _cut_errors.pop(task_key, None)
    except Exception as exc:
        _cut_errors[task_key] = (
            getattr(exc, "message", None) or str(exc) or "cut analysis failed"
        )
    finally:
        _cut_tasks.pop(task_key, None)


@app.get("/api/cohorts/{cohort_id}/facets")
def get_cohort_facets(
    request: Request, cohort_id: str, project: Optional[str] = Query(None)
):
    """Slice values present in this cohort, so the picker only offers real ones."""
    require_read_access(request, project)
    pool = resolve_pool(project)
    cohort = _resolve_cohort_or_404(pool, cohort_id)
    live = analyze.enrich_cohort_status(pool, cohort)
    return {"cohort_id": cohort["id"], **cohort_facets(pool, live)}


@app.get("/api/cohorts/{cohort_id}/cut")
async def get_cut(
    request: Request,
    cohort_id: str,
    project: Optional[str] = Query(None),
    # The group is an intersection of these; outcome and device take repeats
    # or a comma list, so "purchased+checkout on mobile" is expressible.
    outcome: Optional[list] = Query(None),
    device: Optional[list] = Query(None),
    page: Optional[str] = Query(None),
    utm_source: Optional[str] = Query(None),
    utm_medium: Optional[str] = Query(None),
    utm_campaign: Optional[str] = Query(None),
    utm_content: Optional[str] = Query(None),
    fbclid: Optional[str] = Query(None),
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
    pool = resolve_pool(project)
    cohort = _resolve_cohort_or_404(pool, cohort_id)
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
    key = analyze.cut_key(slice_spec)
    # A refresh is a run by definition: there is no reason to bust the cache
    # and then decline to refill it.
    run = run or refresh

    task_key = _cut_task_key(pool, cohort_id, key)
    existing = _cut_tasks.get(task_key)
    if existing and not existing.done():
        # A run already in flight is the fresh answer a refresh was asking for.
        return JSONResponse(
            {"status": "running", "cut": key, "cohort_id": cohort_id}, status_code=202
        )

    cached = analyze.read_cut(pool, cohort_id, key)
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
    live = analyze.enrich_cohort_status(pool, cohort)
    rows = _cut_rows(pool, live)
    if not rows:
        raise ApiError(400, "no analyzed sessions in this cohort yet")

    # Only now, with the call certain to be queued: a refresh that fails its
    # preconditions must leave the old answer standing rather than delete it.
    if refresh:
        analyze.clear_cut(pool, cohort_id, key)

    task = asyncio.create_task(_run_cut_job(pool, cohort_id, rows, slice_spec, key))
    _cut_tasks[task_key] = task
    return JSONResponse(
        {"status": "running", "cut": key, "cohort_id": cohort_id}, status_code=202
    )


def _sse(event: str, data: dict) -> str:
    """One SSE frame. Newlines in the payload are JSON-escaped, so the data
    line stays a single line and the frame cannot be split by model output."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@app.post("/api/cohorts/{cohort_id}/chat")
async def post_cohort_chat(
    request: Request,
    cohort_id: str,
    project: Optional[str] = Query(None),
    # Same group selection as /cut — the chat answers about the cut on screen.
    outcome: Optional[list] = Query(None),
    device: Optional[list] = Query(None),
    page: Optional[str] = Query(None),
    utm_source: Optional[str] = Query(None),
    utm_medium: Optional[str] = Query(None),
    utm_campaign: Optional[str] = Query(None),
    utm_content: Optional[str] = Query(None),
    fbclid: Optional[str] = Query(None),
):
    """Ask a question about one group's write-ups. Streams the answer as SSE.

    Unlike a cut, nothing is cached: the question is open-ended, so every turn
    is a fresh call. Text-only, so this needs the model but not the headless
    reconstruction runtime a per-session analysis does.
    """
    require_read_access(request, project)
    pool = resolve_pool(project)
    cohort = _resolve_cohort_or_404(pool, cohort_id)
    cohort_id = cohort["id"]

    if not analyze.ANALYSIS_ENABLED:
        raise ApiError(503, "analysis is not configured on this server")

    try:
        payload = await request.json()
    except Exception:
        raise ApiError(400, "expected a JSON body")
    messages = analyze.clean_chat_messages(
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

    live = analyze.enrich_cohort_status(pool, cohort)
    rows = _cut_rows(pool, live)
    if not rows:
        raise ApiError(400, "no analyzed sessions in this cohort yet")

    selected, label, given, citations, spec = await analyze.chat_context(rows, slice_spec)
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
                "model": analyze.GEMINI_MODEL,
            },
        )
        try:
            _, model_gate = analyze._gates()
            async with model_gate:
                async for chunk in analyze.stream_gemini_chat(
                    selected, label, given, messages, spec
                ):
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


def stamp_cohort_attribution(pool: Path, cohort: dict) -> dict:
    """Attach attribution, device, outcome and page so the UI can slice client-side."""
    facts = _session_facts(pool)
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


def cohort_response(pool: Path, cohort: dict) -> dict:
    return stamp_cohort_attribution(
        pool, analyze.attach_cohort_insights(pool, cohort)
    )


def project_for_pool(pool: Path) -> projects.Project:
    """Reverse of Project.pool, so the cohort helpers can keep passing a path."""
    for candidate in projects.all_projects():
        if candidate.pool == pool:
            return candidate
    raise ApiError(404, "unknown project")

def _session_facts(pool: Path) -> dict:
    """session_id → {attribution, device, outcome, page} straight from the index."""
    with db.connection(project_for_pool(pool).id) as conn:
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
            "device": analyze.device_class(device),
            "outcome": analyze.outcome_class(order),
            "page": analyze.page_path(url),
        }
    return out


def _cut_rows(pool: Path, cohort: dict) -> list:
    """Analyzed sessions in this cohort, with the write-up a cut reasons over."""
    facts = _session_facts(pool)
    rows = []
    for entry in cohort.get("sessions") or []:
        sid = entry["session_id"]
        session_dir = pool / sid
        cached = analyze.read_analysis(session_dir)
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


def cohort_facets(pool: Path, cohort: dict) -> dict:
    """The slice values actually present in this cohort, for the group picker.

    Counts come from analyzed sessions only, since those are the ones a cut
    can reason over — offering a filter that would select zero write-ups is
    worse than not offering it.
    """
    return analyze.facets_from_rows(_cut_rows(pool, cohort))


def _parse_fbclid_flag(raw: Optional[str]) -> Optional[bool]:
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
                raise ApiError(
                    400, f"{field} must be one of: " + ", ".join(allowed)
                )
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
    spec = {}
    outcomes = _multi_param(outcome, analyze.OUTCOME_VALUES, "outcome")
    if outcomes:
        spec["outcome"] = outcomes
    devices = _multi_param(device, analyze.DEVICE_VALUES, "device")
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
    if RETENTION_DAYS <= 0:
        return 0
    cutoff_ms = int((time.time() - RETENTION_DAYS * 86400) * 1000)
    removed = 0
    for project in projects.all_projects(fresh=True):
        for session_id in sessionstore.expired(project.id, cutoff_ms):
            shutil.rmtree(project.pool / session_id, ignore_errors=True)
            sessionstore.forget(project.id, session_id)
            # Same rule as delete_session: a session purged here no longer has
            # a directory, so its cohort memberships point at write-ups that
            # are gone.
            try:
                analyze.forget_session(project.pool, session_id)
            except Exception as exc:
                print(f"cohort cleanup after expiring {session_id}: {exc}")
            removed += 1
    if removed:
        _bytes_used.clear()
    return removed

app.mount("/", StaticFiles(directory=BASE_DIR / "public", html=True), name="static")

PUBLIC_URL_FILE = DATA_ROOT / "public_url"

def public_base_url() -> str:

    try:
        from_file = PUBLIC_URL_FILE.read_text()
    except Exception:
        from_file = ""
    for value in (os.environ.get("CAPTURE_PUBLIC_URL"), from_file):
        if value:
            return value.strip().rstrip("/")
    return "https://capture.example.com"

# Byte-identical to the block in docs/integration.md; a test pins them.
INTEGRATION_SNIPPET = """<script>
(function () {
  var host = '__HOST__';
  window.CAPTURE_CONFIG = Object.assign(
    { endpoint: host + '/ingest', configUrl: host + '/config', token: '__TOKEN__' },
    window.CAPTURE_CONFIG || {}
  );
  function load() {
    if (window.__captureLoaded) return;
    window.__captureLoaded = 1;
    var s = document.createElement('script');
    s.async = 1;
    s.src = host + '/recorder.bundle.js';
    s.onerror = function () { window.__captureLoaded = 0; };
    document.head.appendChild(s);
  }
  function schedule() {
    if ('requestIdleCallback' in window) requestIdleCallback(load, { timeout: 3000 });
    else setTimeout(load, 1500);
  }
  if (document.readyState === 'complete') schedule();
  else addEventListener('load', schedule);
})();
</script>"""

def integration_snippet(base: str, token: str, indent: str = "  ") -> str:
    body = INTEGRATION_SNIPPET.replace("__HOST__", base).replace("__TOKEN__", token)
    return "\n".join(indent + line if line else line for line in body.split("\n"))

def add_project(name: str) -> None:
    token = "cap_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=24))
    project = projects.create(name, token)
    project.pool.mkdir(parents=True, exist_ok=True)
    base = public_base_url()
    print(f"project '{name}' created — token: {token}")
    print("\nAdd this snippet to the brand's site, on every page:\n")
    print(integration_snippet(base, token))
    if base == "https://capture.example.com":
        print(
            "\n  (placeholder host — set CAPTURE_PUBLIC_URL or run ./setup.sh "
            "to record your real one)"
        )
    print(
        "\nOptional per-project recording config: "
        f"data/projects/{token}/config.json (falls back to data/config.json)"
    )

def find_project(handle: str):
    """CLI lookup: accepts the project handle or any of its ingest tokens."""
    return projects.by_handle(handle) or projects.by_token(handle)

def list_projects_cli() -> None:
    known = projects.all_projects(fresh=True)
    if not known:
        print("no projects configured — run add-project, or backfill.py to import existing data")
        return
    for project in known:
        count, used = projects.usage(project)
        org = project.org_id or "no org — operators only"
        tokens = ", ".join(project.tokens) or "no live token"
        label = project.handle or "(default)"
        print(
            f"{label}  {project.name}  —  {count} session(s), {used / 1024**2:.1f} MB"
            f"  —  {org}  —  {tokens}"
        )

def set_project_org(handle: str, org_id: str) -> None:
    project = find_project(handle)
    if project is None:
        print(f"no such project: {handle}")
        return
    projects.set_org(project, org_id)
    print(f"'{project.name}' is now readable by members of WorkOS organization {org_id}")

def set_project_domains(handle: str, domains: list) -> None:
    project = find_project(handle)
    if project is None:
        print(f"no such project: {handle}")
        return
    cleaned = [
        host.lower()
        for d in domains
        if (host := urllib.parse.urlparse(d if "://" in d else f"//{d}").hostname)
    ]
    projects.set_domains(project, cleaned)
    if cleaned:
        print(f"'{project.name}' now accepts recordings only from {', '.join(cleaned)} (and subdomains)")
    else:
        print(f"'{project.name}' now accepts recordings from any domain")

def invite_user(email: str, target: str) -> None:
    if not WORKOS_ENABLED:
        print("invites need WORKOS_API_KEY and WORKOS_CLIENT_ID in the environment")
        return
    project = find_project(target)
    org_id = project.org_id if project else target
    if not (org_id or "").startswith("org_"):
        print(f"'{target}' has no WorkOS org — pass an org_… id or run set-org first")
        return
    try:
        invitation = workos_call(
            "/user_management/invitations",
            {"email": email, "organization_id": org_id},
            bearer=True,
        )
    except urllib.error.HTTPError as err:
        print(f"invite failed: {err.read().decode(errors='replace')}")
        return
    expires = invitation.get("expires_at") or "?"
    print(f"invited {email} to {org_id} — they'll get an email link, valid until {expires}")

def remove_project(handle: str, purge: bool) -> None:
    project = find_project(handle)
    if project is None:
        print(f"no such project: {handle}")
        return
    count, used = projects.usage(project)
    for token in project.tokens:
        projects.revoke_token(token)
    print(f"revoked '{project.name}' — batches sent with its tokens are now rejected")

    if purge:
        shutil.rmtree(project.pool.parent, ignore_errors=True)
        projects.delete(project)
        print(f"deleted {count} stored session(s), {used / 1024**2:.1f} MB")
    elif count:
        print(f"kept {count} session(s), {used / 1024**2:.1f} MB — re-run with --purge to erase")

def _cli(argv: list) -> None:
    if len(argv) >= 2 and argv[0] == "add-project":
        add_project(argv[1])
    elif argv[:1] == ["list-projects"]:
        list_projects_cli()
    elif len(argv) >= 2 and argv[0] == "remove-project":
        remove_project(argv[1], purge="--purge" in argv[2:])
    elif len(argv) >= 3 and argv[0] == "set-org":
        set_project_org(argv[1], argv[2])
    elif len(argv) >= 2 and argv[0] == "set-domains":
        set_project_domains(argv[1], argv[2:])
    elif len(argv) >= 3 and argv[0] == "invite":
        invite_user(argv[1], argv[2])
    elif argv[:1] == ["reindex"]:
        import backfill

        db.migrate()
        result = backfill.backfill()
        print(
            f"{result['sessions']} session(s) across {result['projects']} project(s);"
            f" {result['skipped']} empty dir(s) skipped"
        )
    else:
        print("usage: python server.py add-project <name>")
        print("       python server.py list-projects")
        print("       python server.py remove-project <token> [--purge]")
        print("       python server.py set-org <token> <workos_org_id>")
        print("       python server.py set-domains <token> [domain ...]")
        print("       python server.py invite <email> <project_token_or_org_id>")
        print("       python server.py reindex")
        print("run the server with: .venv/bin/uvicorn server:app --port 3000")


if __name__ == "__main__":
    import sys

    try:
        _cli(sys.argv[1:])
    except ProjectsUnavailable as exc:
        print(f"error: {exc}")
        print("the projects table is unreadable — refusing to guess at tenant routing")
        sys.exit(1)
    finally:
        # Without this the pool's worker threads outlive the command and print
        # shutdown warnings over its output.
        db.close_pool()
