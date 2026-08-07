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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import analyze

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

    async def loop():
        while True:
            removed = await asyncio.to_thread(sweep_expired_sessions)
            if removed:
                print(f"retention: removed {removed} expired session(s)")
            await asyncio.sleep(24 * 3600)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()

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

@app.middleware("http")
async def revalidate_static(request: Request, call_next):

    response = await call_next(request)
    is_static = request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/"
    if is_static and "cache-control" not in response.headers:
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
    try:
        projects = json.loads(PROJECTS_PATH.read_text())
        return projects if isinstance(projects, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"projects: data/projects.json unusable: {exc}")
        return {}

def project_sessions_dir(token: str) -> Path:
    return PROJECTS_DIR / token / "sessions"

def resolve_sessions_dir(token: Optional[str], projects: dict) -> Optional[Path]:
    if not projects:
        return DATA_DIR
    if token and SAFE_ID.match(token) and token in projects:
        return project_sessions_dir(token)
    return None

def require_allowed_domain(request: Request, meta: Optional[dict]) -> None:
    domains = (meta or {}).get("domains") or []
    if not domains:
        return
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    host = (urllib.parse.urlparse(source).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in domains):
        raise ApiError(403, "domain not allowed for this project")

def session_pools() -> list:
    pools = [DATA_DIR]
    if PROJECTS_DIR.is_dir():
        for entry in PROJECTS_DIR.iterdir():
            if (entry / "sessions").is_dir():
                pools.append(entry / "sessions")
    return pools

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

def gunzip(body: bytes, limit: int) -> bytes:

    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = stream.decompress(body, limit)
    if stream.unconsumed_tail:
        raise ValueError("decompressed payload too large")
    return raw

def dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*.json") if f.is_file())

_pool_bytes: dict = {}

def pool_bytes(pool: Path) -> int:
    if pool not in _pool_bytes:
        _pool_bytes[pool] = dir_bytes(pool)
    return _pool_bytes[pool]

CONFIG_PATH = DATA_ROOT / "config.json"

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message

@app.exception_handler(ApiError)
async def api_error_response(_request, exc: ApiError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)

def resolve_pool(project: Optional[str]) -> Path:
    pool = read_pool(project)
    if pool is None:
        raise ApiError(404, "unknown project")
    return pool

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
    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")

    projects = load_projects()
    pool = resolve_sessions_dir(token, projects)
    if pool is None:
        raise ApiError(401, "unknown project token")
    require_allowed_domain(request, projects.get(token))

    if MIN_FREE_DISK_BYTES and shutil.disk_usage(DATA_ROOT).free < MIN_FREE_DISK_BYTES:
        raise ApiError(507, "server storage full")
    if MAX_PROJECT_BYTES:
        used = _pool_bytes.get(pool)
        if used is None:
            used = await asyncio.to_thread(pool_bytes, pool)
        if used >= MAX_PROJECT_BYTES:
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

    meta_path = session_dir / VISITOR_FILE
    if not meta_path.is_file():
        meta = {}
        visitor, url = payload.get("visitor_id"), payload.get("url")
        if isinstance(visitor, str) and SAFE_ID.match(visitor):
            meta["visitor_id"] = visitor
        if isinstance(url, str) and url:
            meta["url"] = url[:2048]
        if meta:
            meta_path.write_text(json.dumps(meta))

    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    window_prefix = re.sub(r"[^a-f0-9]", "", str(payload.get("window_id") or ""))[:8]
    parts = [str(int(time.time() * 1000)), window_prefix, suffix]
    chunk_path = session_dir / ("-".join(p for p in parts if p) + ".json")
    chunk_path.write_text(
        json.dumps(
            {
                "window_id": payload.get("window_id"),
                "url": payload.get("url"),
                "received_at": int(time.time() * 1000),
                "events": events,
            }
        )
    )
    index_interactions(session_dir, events)
    _pool_bytes[pool] = pool_bytes(pool) + chunk_path.stat().st_size

    return {"status": "ok", "stored": len(events)}

ORDER_FILE = "order.json"
ORDER_MAX_BODY_BYTES = 4096

VISITOR_FILE = "visitor.json"

INTERACTIONS_FILE = "interactions.json"

def index_interactions(session_dir: Path, events: list) -> None:
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
    if not clicks:
        return
    path = session_dir / INTERACTIONS_FILE
    try:
        counts = json.loads(path.read_text())
        if not isinstance(counts, dict):
            counts = {}
    except Exception:
        counts = {}
    counts["clicks"] = int(counts.get("clicks") or 0) + clicks
    counts["rage"] = int(counts.get("rage") or 0) + rage
    path.write_text(json.dumps(counts))

@app.post("/order")
async def order_event(
    request: Request,
    session_id: str = Query(""),
    token: Optional[str] = Query(None),
):

    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")
    pool = resolve_sessions_dir(token, load_projects())
    if pool is None:
        raise ApiError(401, "unknown project token")
    session_dir = pool / session_id

    if not session_dir.is_dir():
        raise ApiError(404, "unknown session")

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

    path = session_dir / ORDER_FILE
    try:
        order = json.loads(path.read_text())
        if not isinstance(order, dict):
            order = {}
    except Exception:
        order = {}

    now = int(time.time() * 1000)
    if event == "checkout_started":
        order.setdefault("checkout_started_at", now)
    else:
        order["purchased_at"] = now
        if isinstance(payload.get("order_id"), str):
            order["order_id"] = payload["order_id"][:128]
        total = payload.get("total")

        if isinstance(total, (int, float)) and not isinstance(total, bool) and -1e15 < total < 1e15:
            order["total"] = total
        if isinstance(payload.get("currency"), str):
            order["currency"] = payload["currency"][:8]
    path.write_text(json.dumps(order))
    return {"status": "ok"}

@app.get("/healthz")
def healthz():

    checks = {}

    try:
        probe = DATA_ROOT / ".healthz"
        probe.write_text("ok")
        probe.unlink()
        checks["storage_writable"] = True
    except Exception:
        checks["storage_writable"] = False

    try:
        free = shutil.disk_usage(DATA_ROOT).free
        checks["disk_above_floor"] = not MIN_FREE_DISK_BYTES or free >= MIN_FREE_DISK_BYTES
    except Exception:
        checks["disk_above_floor"] = False

    checks["bundles_present"] = all(
        (BASE_DIR / "public" / name).is_file()
        for name in ("capture.js", "recorder.bundle.js", "replay.bundle.js")
    )
    checks["replay_auth_enabled"] = bool(REPLAY_PASSWORD or WORKOS_ENABLED) and not REPLAY_AUTH_DISABLED

    healthy = all(v for k, v in checks.items() if k != "replay_auth_enabled")
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )

@app.get("/config")
def remote_config(token: Optional[str] = Query(None)):

    config_path = CONFIG_PATH
    if token and SAFE_ID.match(token) and token in load_projects():
        project_config = PROJECTS_DIR / token / "config.json"
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

def chunk_files(session_dir: Path) -> list:

    return sorted(
        (c for c in session_dir.glob("*.json") if SAFE_CHUNK.match(c.name)),
        key=chunk_timestamp,
    )

def read_pool(project: Optional[str]) -> Optional[Path]:
    if not project:
        return DATA_DIR
    if SAFE_ID.match(project) and project in load_projects():
        return project_sessions_dir(project)
    return None

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
    return {t for t, meta in load_projects().items() if (meta or {}).get("org_id") in orgs}

def require_read_access(request: Request, project: Optional[str]) -> None:
    allowed = allowed_projects(request)
    if allowed is not None and (not project or project not in allowed):
        raise ApiError(403, "not your project")

@app.get("/api/projects")
def list_projects(request: Request):
    allowed = allowed_projects(request)
    projects = [
        {"token": token, "name": (meta or {}).get("name") or token}
        for token, meta in load_projects().items()
        if allowed is None or token in allowed
    ]

    if allowed is None and projects and any(entry.is_dir() for entry in DATA_DIR.iterdir()):
        projects.append({"token": "", "name": "(no project)"})
    email = (getattr(request.state, "auth", None) or {}).get("email")
    return {
        "projects": projects,
        "analysis_enabled": analyze.ANALYSIS_ENABLED,
        **({"email": email} if email else {}),
    }

@app.get("/api/sessions")
def list_sessions(request: Request, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    pool = resolve_pool(project)
    sessions = []
    for entry in pool.iterdir() if pool.is_dir() else []:
        if not entry.is_dir():
            continue
        chunks = chunk_files(entry)
        if not chunks:
            continue
        info = {
            "session_id": entry.name,
            "first_seen": chunk_timestamp(chunks[0]),
            "last_seen": chunk_timestamp(chunks[-1]),
            "chunk_count": len(chunks),
            "bytes": sum(c.stat().st_size for c in chunks),
        }
        order_path = entry / ORDER_FILE
        if order_path.is_file():
            try:
                info["order"] = json.loads(order_path.read_text())
            except Exception:
                pass
        meta_path = entry / VISITOR_FILE
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                info.update({k: meta[k] for k in ("visitor_id", "url") if isinstance(meta.get(k), str)})
            except Exception:
                pass
        interactions_path = entry / INTERACTIONS_FILE
        if interactions_path.is_file():
            try:
                interactions = json.loads(interactions_path.read_text())
                if isinstance(interactions, dict):
                    info["interactions"] = interactions
            except Exception:
                pass
        sessions.append(info)
    sessions.sort(key=lambda s: s["last_seen"], reverse=True)
    return {"sessions": sessions}

@app.delete("/api/sessions/{session_id}")
def delete_session(request: Request, session_id: str, project: Optional[str] = Query(None)):

    require_read_access(request, project)
    session_dir = resolve_session(session_id, project)
    freed = dir_bytes(session_dir)
    shutil.rmtree(session_dir, ignore_errors=True)
    _pool_bytes.pop(session_dir.parent, None)
    print(f"deleted session {session_id} ({freed} bytes)")
    return {"status": "deleted", "session_id": session_id, "bytes_freed": freed}

@app.get("/api/sessions/{session_id}/chunks")
def session_chunks(request: Request, session_id: str, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    chunks = chunk_files(resolve_session(session_id, project))
    url = None
    if chunks:
        try:
            url = json.loads(chunks[0].read_text()).get("url")
        except Exception:
            pass
    manifest = [
        {
            "name": c.name,
            "timestamp": chunk_timestamp(c),
            "bytes": c.stat().st_size,
            "window": chunk_window(c),
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
    path = resolve_pool(project) / session_id / chunk_name
    if not path.is_file():
        raise ApiError(404, "not found")

    return FileResponse(
        path, media_type="application/json", headers={"Cache-Control": "max-age=86400"}
    )

_analysis_locks: dict = {}

@app.get("/api/sessions/{session_id}/analysis")
def get_analysis(request: Request, session_id: str, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    session_dir = resolve_session(session_id, project)
    cached = analyze.read_analysis(session_dir)
    if not cached:
        raise ApiError(404, "not analyzed yet")
    return cached

@app.post("/api/sessions/{session_id}/analysis")
async def create_analysis(
    request: Request,
    session_id: str,
    project: Optional[str] = Query(None),
    force: bool = Query(False),
):
    require_read_access(request, project)
    if not analyze.ANALYSIS_ENABLED:
        raise ApiError(503, "analysis is not configured on this server")
    session_dir = resolve_session(session_id, project)
    chunks = chunk_files(session_dir)
    if not chunks:
        raise ApiError(400, "this session has no chunks to analyze")

    # One reconstruction per session at a time: it launches a browser, and a
    # double-click on the button should wait for the first run, not start a second.
    lock = _analysis_locks.setdefault(session_dir, asyncio.Lock())
    async with lock:
        try:
            return await analyze.analyze_session(
                session_dir, [c.name for c in chunks], force=force
            )
        except RuntimeError as exc:
            raise ApiError(500, str(exc))
        finally:
            if not lock.locked():
                _analysis_locks.pop(session_dir, None)

def sweep_expired_sessions() -> int:

    if RETENTION_DAYS <= 0:
        return 0
    cutoff_ms = (time.time() - RETENTION_DAYS * 86400) * 1000
    removed = 0
    for pool in session_pools():
        for entry in pool.iterdir():
            if not entry.is_dir():
                continue
            chunks = chunk_files(entry)
            newest = chunk_timestamp(chunks[-1]) if chunks else 0
            if newest < cutoff_ms:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    if removed:
        _pool_bytes.clear()
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

def add_project(name: str) -> None:
    token = "cap_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=24))
    projects = load_projects()
    projects[token] = {"name": name, "created_at": int(time.time() * 1000)}
    PROJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_PATH.write_text(json.dumps(projects, indent=2))
    project_sessions_dir(token).mkdir(parents=True, exist_ok=True)
    base = public_base_url()
    print(f"project '{name}' created — token: {token}")
    print("\nAdd this tag to the brand's site:\n")
    print(f'  <script src="{base}/capture.js" data-token="{token}"></script>')
    if base == "https://capture.example.com":
        print(
            "\n  (placeholder host — set CAPTURE_PUBLIC_URL or run ./setup.sh "
            "to record your real one)"
        )
    print(
        "\nOptional per-project recording config: "
        f"data/projects/{token}/config.json (falls back to data/config.json)"
    )

def pool_usage(pool: Path) -> tuple:
    if not pool.is_dir():
        return 0, 0
    return sum(1 for d in pool.iterdir() if d.is_dir()), dir_bytes(pool)

def list_projects_cli() -> None:
    projects = load_projects()
    if not projects:
        print("no projects configured — single-tenant mode, tokens are ignored")
        return
    for token, meta in projects.items():
        count, used = pool_usage(project_sessions_dir(token))
        name = (meta or {}).get("name") or "?"
        org = (meta or {}).get("org_id") or "no org — operators only"
        print(f"{token}  {name}  —  {count} session(s), {used / 1024**2:.1f} MB  —  {org}")

def set_project_org(token: str, org_id: str) -> None:
    projects = load_projects()
    if token not in projects:
        print(f"no such project: {token}")
        return
    projects[token] = {**(projects[token] or {}), "org_id": org_id}
    PROJECTS_PATH.write_text(json.dumps(projects, indent=2))
    name = (projects[token] or {}).get("name") or token
    print(f"'{name}' is now readable by members of WorkOS organization {org_id}")

def set_project_domains(token: str, domains: list) -> None:
    projects = load_projects()
    if token not in projects:
        print(f"no such project: {token}")
        return
    cleaned = [
        host.lower()
        for d in domains
        if (host := urllib.parse.urlparse(d if "://" in d else f"//{d}").hostname)
    ]
    projects[token] = {**(projects[token] or {}), "domains": cleaned}
    PROJECTS_PATH.write_text(json.dumps(projects, indent=2))
    name = (projects[token] or {}).get("name") or token
    if cleaned:
        print(f"'{name}' now accepts recordings only from {', '.join(cleaned)} (and subdomains)")
    else:
        print(f"'{name}' now accepts recordings from any domain")

def invite_user(email: str, target: str) -> None:
    if not WORKOS_ENABLED:
        print("invites need WORKOS_API_KEY and WORKOS_CLIENT_ID in the environment")
        return
    meta = load_projects().get(target)
    org_id = (meta or {}).get("org_id") if meta is not None else target
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

def remove_project(token: str, purge: bool) -> None:
    projects = load_projects()
    if token not in projects:
        print(f"no such project: {token}")
        return
    name = (projects.pop(token) or {}).get("name") or token
    PROJECTS_PATH.write_text(json.dumps(projects, indent=2))
    print(f"revoked '{name}' — batches sent with that token are now rejected")

    pool = project_sessions_dir(token)
    count, used = pool_usage(pool)
    if purge:
        shutil.rmtree(pool.parent, ignore_errors=True)
        print(f"deleted {count} stored session(s), {used / 1024**2:.1f} MB")
    elif count:
        print(f"kept {count} session(s), {used / 1024**2:.1f} MB — re-run with --purge to erase")

if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
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
    else:
        print("usage: python server.py add-project <name>")
        print("       python server.py list-projects")
        print("       python server.py remove-project <token> [--purge]")
        print("       python server.py set-org <token> <workos_org_id>")
        print("       python server.py set-domains <token> [domain ...]")
        print("       python server.py invite <email> <project_token_or_org_id>")
        print("run the server with: .venv/bin/uvicorn server:app --port 3000")
