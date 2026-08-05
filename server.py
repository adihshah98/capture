"""Ingest + replay server: the recorder POSTs gzipped event batches to
/ingest, each batch is stored as an immutable chunk on disk, and the replay
UI reads sessions back through a small JSON API.

Run: .venv/bin/uvicorn server:app --port 3000
"""

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

@asynccontextmanager
async def lifespan(_app):
    """Runs at startup, so it may reference module state defined further down."""
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

# The snippet runs on other people's pages, so the write side must be reachable
# cross-origin. Safe because the read side is gated by REPLAY_PASSWORD, which
# wildcard CORS cannot carry, and DELETE is absent from the method list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def replay_auth(request: Request, call_next):
    """The read side requires a login. Most specific mode wins: local dev
    opts out entirely; WorkOS (if configured) gives per-user sessions scoped
    to a brand's own projects; else REPLAY_PASSWORD guards everything as a
    single operator; nothing configured refuses to serve rather than falling
    open. The write side stays open for the snippet in every mode."""
    # Normalized so /Api/ and //replay.html can't slip past a check that
    # StaticFiles would still serve them from.
    path = re.sub(r"/+", "/", request.url.path).lower()
    protected = path.startswith("/api/") or path == "/replay.html"
    if protected and not REPLAY_AUTH_DISABLED:
        if WORKOS_ENABLED:
            session = read_session(request.cookies.get(SESSION_COOKIE))
            if not session:
                if path == "/replay.html":  # a person in a browser: send to login
                    return RedirectResponse("/login", status_code=302)
                return JSONResponse(  # the UI's fetches: point it at login
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
    """Without this, browsers heuristically cache bundles and keep running
    stale code after a deploy. Endpoints setting their own header are left."""
    response = await call_next(request)
    is_static = request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/"
    if is_static and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response

BASE_DIR = Path(__file__).resolve().parent
# Overridable so a test run never writes into the working copy's recordings.
DATA_ROOT = Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")
DATA_DIR = DATA_ROOT / "sessions"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Token -> {"name": ...}. No projects configured = single-tenant mode: tokens
# are ignored and everything lands in data/sessions.
PROJECTS_PATH = DATA_ROOT / "projects.json"
PROJECTS_DIR = DATA_ROOT / "projects"

# HTTP Basic, any username. Unset refuses the read side entirely;
# REPLAY_AUTH_DISABLED=1 opts out for local development.
REPLAY_PASSWORD = os.environ.get("REPLAY_PASSWORD", "")
REPLAY_AUTH_DISABLED = os.environ.get("REPLAY_AUTH_DISABLED") == "1"

# Per-user logins via WorkOS AuthKit. When both vars are set they replace the
# Basic password: each project's org_id (see `set-org`) says which WorkOS
# organization may read it, so a brand's team sees only their own recordings.
WORKOS_API_KEY = os.environ.get("WORKOS_API_KEY", "")
WORKOS_CLIENT_ID = os.environ.get("WORKOS_CLIENT_ID", "")
WORKOS_ENABLED = bool(WORKOS_API_KEY and WORKOS_CLIENT_ID)
WORKOS_API = "https://api.workos.com"

# Operator emails see every project (and the legacy no-project pool).
OPERATORS = {e.strip().lower() for e in os.environ.get("CAPTURE_OPERATORS", "").split(",") if e.strip()}

# Sessions are HMAC-signed cookies, no server-side store. Rotating the secret
# (or the API key it is derived from) logs everyone out — nothing else.
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
    """The payload of a validly signed, unexpired session cookie, else None."""
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
    """Storage pool for an ingest request; None means reject."""
    if not projects:
        return DATA_DIR
    if token and SAFE_ID.match(token) and token in projects:
        return project_sessions_dir(token)
    return None


def session_pools() -> list:
    """Every directory holding session subdirectories: legacy pool + one
    per project."""
    pools = [DATA_DIR]
    if PROJECTS_DIR.is_dir():
        for entry in PROJECTS_DIR.iterdir():
            if (entry / "sessions").is_dir():
                pools.append(entry / "sessions")
    return pools

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Old style: <ts>-<rand>.json; new style: <ts>-<window8>-<rand>.json
SAFE_CHUNK = re.compile(r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$")


def chunk_window(path) -> Optional[str]:
    """Window-id prefix from a chunk filename (None for pre-window chunks)."""
    parts = path.name.rsplit(".", 1)[0].split("-")
    return parts[1] if len(parts) == 3 else None
MAX_BODY_BYTES = 50 * 1024 * 1024
# Bounding the compressed body alone is not enough: a 50 MB gzip expands to
# gigabytes. A real batch is ~1 MB of JSON.
MAX_DECOMPRESSED_BYTES = int(os.environ.get("MAX_DECOMPRESSED_BYTES", 32 * 1024 * 1024))

# Ingest tokens are public by design, so without a ceiling one script can fill
# the volume for every brand. Either check is disabled by setting it to 0.
MAX_PROJECT_BYTES = int(os.environ.get("MAX_PROJECT_BYTES", 2 * 1024**3))
MIN_FREE_DISK_BYTES = int(os.environ.get("MIN_FREE_DISK_BYTES", 1024**3))

# 0 keeps everything forever.
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))


def gunzip(body: bytes, limit: int) -> bytes:
    """Decompress with a ceiling on the output, unlike gzip.decompress."""
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = stream.decompress(body, limit)
    if stream.unconsumed_tail:
        raise ValueError("decompressed payload too large")
    return raw


def dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*.json") if f.is_file())


# Scanned once, then tracked as chunks are written — walking every chunk on
# each ingest would cost more than the write itself.
_pool_bytes: dict = {}


def pool_bytes(pool: Path) -> int:
    if pool not in _pool_bytes:
        _pool_bytes[pool] = dir_bytes(pool)
    return _pool_bytes[pool]

# Global remote recording config; editing it changes recording behavior
# fleet-wide without page redeploys (clients re-fetch after a 1 h TTL).
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

    pool = resolve_sessions_dir(token, load_projects())
    if pool is None:
        raise ApiError(401, "unknown project token")

    # Before the body is read, so an abusive client is cheap to reject. The
    # recorder retries 5xx and drops the batch on 4xx, which is what each means.
    if MIN_FREE_DISK_BYTES and shutil.disk_usage(DATA_ROOT).free < MIN_FREE_DISK_BYTES:
        raise ApiError(507, "server storage full")
    if MAX_PROJECT_BYTES:
        # Cached after the first request; only the initial scan leaves the loop.
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

    visitor = payload.get("visitor_id")
    if isinstance(visitor, str) and SAFE_ID.match(visitor):
        visitor_path = session_dir / VISITOR_FILE
        if not visitor_path.is_file():
            visitor_path.write_text(json.dumps({"visitor_id": visitor}))

    # Timestamp orders chunks; the window prefix lets the manifest group them
    # per tab without opening a single file.
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
    _pool_bytes[pool] = pool_bytes(pool) + chunk_path.stat().st_size

    return {"status": "ok", "stored": len(events)}


# Written beside a session's chunks by /order; excluded from chunk listings.
ORDER_FILE = "order.json"
ORDER_MAX_BODY_BYTES = 4096

VISITOR_FILE = "visitor.json"


@app.post("/order")
async def order_event(
    request: Request,
    session_id: str = Query(""),
    token: Optional[str] = Query(None),
):
    """Purchase outcome for pages the recorder can't run on (Shopify's
    checkout): a custom web pixel posts checkout_started / checkout_completed
    with the session id it read from the storefront's localStorage. Stored as
    one small metadata file beside the session's chunks, not as events —
    there is no recording of the checkout itself to attach them to."""
    if not SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")
    pool = resolve_sessions_dir(token, load_projects())
    if pool is None:
        raise ApiError(401, "unknown project token")
    session_dir = pool / session_id
    # Annotate only sessions that were actually recorded — creating
    # directories here would let anyone holding the (public) token fill the
    # disk with junk sessions.
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
        # The bounds also exclude NaN/Infinity, which browsers' JSON.parse
        # would choke on when the session list is served back.
        if isinstance(total, (int, float)) and not isinstance(total, bool) and -1e15 < total < 1e15:
            order["total"] = total
        if isinstance(payload.get("currency"), str):
            order["currency"] = payload["currency"][:8]
    path.write_text(json.dumps(order))
    return {"status": "ok"}


@app.get("/healthz")
def healthz():
    """What a deploy can break, rather than whether a port is open. Booleans
    only: this is unauthenticated."""
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

    # A build that produced nothing would otherwise serve 404s to every brand.
    checks["bundles_present"] = all(
        (BASE_DIR / "public" / name).is_file()
        for name in ("capture.js", "recorder.bundle.js", "replay.bundle.js")
    )
    checks["replay_auth_enabled"] = bool(REPLAY_PASSWORD or WORKOS_ENABLED) and not REPLAY_AUTH_DISABLED

    # Auth off is a deliberate local-dev choice: reported, but not a failure.
    healthy = all(v for k, v in checks.items() if k != "replay_auth_enabled")
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )


@app.get("/config")
def remote_config(token: Optional[str] = Query(None)):
    """Recording settings: per-project config.json if the project has one,
    else the global file. Missing or invalid files degrade to {} — a bad
    config must never stop recording."""
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
    response.headers["Cache-Control"] = "no-cache"  # clients cache via their own TTL
    return response


def chunk_timestamp(path: Path) -> int:
    try:
        return int(path.name.split("-")[0])
    except ValueError:
        return 0


def chunk_files(session_dir: Path) -> list:
    """A session's event chunks, ordered — metadata files (order.json) live
    in the same directory and must never be listed or replayed as chunks."""
    return sorted(
        (c for c in session_dir.glob("*.json") if SAFE_CHUNK.match(c.name)),
        key=chunk_timestamp,
    )


def read_pool(project: Optional[str]) -> Optional[Path]:
    """Sessions directory for a read-API request; None means unknown project.
    No param reads the legacy single-tenant pool."""
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
    """Exchange the AuthKit code for the user's email and organization ids."""
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
    orgs = {auth["organization_id"]} if auth.get("organization_id") else set()
    if user.get("id"):
        memberships = workos_call(
            f"/user_management/organization_memberships?user_id={user['id']}&statuses=active",
            bearer=True,
        )
        orgs |= {m["organization_id"] for m in memberships.get("data", []) if m.get("organization_id")}
    return (user.get("email") or "").lower(), sorted(orgs)


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
    # The state cookie ties the callback to a login this server started.
    if not code or not state or state != request.cookies.get(STATE_COOKIE):
        raise ApiError(401, "bad login attempt — start again at /login")
    try:
        email, orgs = workos_authenticate(code)
    except Exception as exc:
        print(f"auth: WorkOS code exchange failed: {exc}")
        raise ApiError(401, "login failed — start again at /login")
    session = make_session({"email": email, "orgs": orgs, "exp": int(time.time()) + SESSION_TTL_S})
    response = RedirectResponse("/replay.html", status_code=302)
    response.set_cookie(
        SESSION_COOKIE, session, max_age=SESSION_TTL_S, httponly=True, secure=True, samesite="lax"
    )
    response.delete_cookie(STATE_COOKIE)
    return response


@app.get("/logout")
def logout():
    response = Response("Signed out.\n", media_type="text/plain")
    response.delete_cookie(SESSION_COOKIE)
    return response


def allowed_projects(request: Request) -> Optional[set]:
    """Project tokens this request may read; None means unrestricted (an
    operator, the Basic-password mode, or auth disabled for local dev)."""
    auth = getattr(request.state, "auth", None)
    if auth is None or (auth.get("email") or "").lower() in OPERATORS:
        return None
    orgs = set(auth.get("orgs") or [])
    return {t for t, meta in load_projects().items() if (meta or {}).get("org_id") in orgs}


def require_read_access(request: Request, project: Optional[str]) -> None:
    """Reject reads outside the session's own projects. The ?project= query
    parameter is untrusted input once brands hold logins."""
    allowed = allowed_projects(request)
    if allowed is not None and (not project or project not in allowed):
        raise ApiError(403, "not your project")


@app.get("/api/projects")
def list_projects(request: Request):
    """Projects for the replay UI's selector, limited to what the session may
    read. The legacy pool shows up as a blank token for operators when it
    still holds recordings, so pre-project sessions stay reachable after
    tokens are introduced."""
    allowed = allowed_projects(request)
    projects = [
        {"token": token, "name": (meta or {}).get("name") or token}
        for token, meta in load_projects().items()
        if allowed is None or token in allowed
    ]
    if allowed is None and projects and any(entry.is_dir() for entry in DATA_DIR.iterdir()):
        projects.append({"token": "", "name": "(no project)"})
    return {"projects": projects}


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
        visitor_path = entry / VISITOR_FILE
        if visitor_path.is_file():
            try:
                info["visitor_id"] = json.loads(visitor_path.read_text()).get("visitor_id")
            except Exception:
                pass
        sessions.append(info)
    sessions.sort(key=lambda s: s["last_seen"], reverse=True)
    return {"sessions": sessions}


@app.delete("/api/sessions/{session_id}")
def delete_session(request: Request, session_id: str, project: Optional[str] = Query(None)):
    """Irreversible. DELETE is absent from the CORS allowlist, so only the
    replay UI's own origin can reach it. Brands may delete within their own
    project — it is their data."""
    require_read_access(request, project)
    session_dir = resolve_session(session_id, project)
    freed = dir_bytes(session_dir)
    shutil.rmtree(session_dir, ignore_errors=True)
    _pool_bytes.pop(session_dir.parent, None)  # freed bytes belong back in the quota
    print(f"deleted session {session_id} ({freed} bytes)")
    return {"status": "deleted", "session_id": session_id, "bytes_freed": freed}


@app.get("/api/sessions/{session_id}/chunks")
def session_chunks(request: Request, session_id: str, project: Optional[str] = Query(None)):
    """Ordered chunk manifest; the player fetches chunks one by one."""
    require_read_access(request, project)
    chunks = chunk_files(resolve_session(session_id, project))
    url = None
    if chunks:  # only the first chunk is opened; the rest stay on disk
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
    windows = list(dict.fromkeys(c["window"] for c in manifest))  # by first appearance
    return {"session_id": session_id, "url": url, "windows": windows, "chunks": manifest}


@app.get("/api/sessions/{session_id}/chunks/{chunk_name}")
def session_chunk(request: Request, session_id: str, chunk_name: str, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    if not SAFE_ID.match(session_id) or not SAFE_CHUNK.match(chunk_name):
        raise ApiError(400, "invalid id")
    path = resolve_pool(project) / session_id / chunk_name
    if not path.is_file():
        raise ApiError(404, "not found")
    # Immutable, so the browser may cache them across scrubbing re-visits.
    return FileResponse(
        path, media_type="application/json", headers={"Cache-Control": "max-age=86400"}
    )


def sweep_expired_sessions() -> int:
    """Delete sessions whose newest chunk is past retention, across every
    pool. Empty directories count as expired."""
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
        _pool_bytes.clear()  # freed space must count against the quota again
    return removed


# Registered last so /ingest and /api/* win; serves index.html at "/".
app.mount("/", StaticFiles(directory=BASE_DIR / "public", html=True), name="static")


PUBLIC_URL_FILE = DATA_ROOT / "public_url"


def public_base_url() -> str:
    """The env var covers the running service; the file covers CLI runs, which
    don't load the systemd environment."""
    try:
        from_file = PUBLIC_URL_FILE.read_text()
    except Exception:
        from_file = ""
    for value in (os.environ.get("CAPTURE_PUBLIC_URL"), from_file):
        if value:
            return value.strip().rstrip("/")
    return "https://capture.example.com"


def add_project(name: str) -> None:
    """Create a project token and print the one-tag snippet for the brand."""
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
    """Grant a WorkOS organization read access to a project's recordings."""
    projects = load_projects()
    if token not in projects:
        print(f"no such project: {token}")
        return
    projects[token] = {**(projects[token] or {}), "org_id": org_id}
    PROJECTS_PATH.write_text(json.dumps(projects, indent=2))
    name = (projects[token] or {}).get("name") or token
    print(f"'{name}' is now readable by members of WorkOS organization {org_id}")


def remove_project(token: str, purge: bool) -> None:
    """Revoke a token. Recordings are kept unless --purge is passed: erasing a
    brand's history should never be a side effect of rotating their tag."""
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
    else:
        print("usage: python server.py add-project <name>")
        print("       python server.py list-projects")
        print("       python server.py remove-project <token> [--purge]")
        print("       python server.py set-org <token> <workos_org_id>")
        print("run the server with: .venv/bin/uvicorn server:app --port 3000")
