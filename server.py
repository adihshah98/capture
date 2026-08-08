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
import sqlite3
import string
import threading
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

# Load .env before importing anything that reads configuration at import time.
# Real environment variables win, so a deployed box is unaffected by a stray file.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

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
        await asyncio.to_thread(reconcile_index)
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
    # Only recorded chunks count toward the ingest quota. Analysis output is
    # derived, is bounded per session, and must never throttle recording.
    return sum(
        f.stat().st_size
        for f in path.rglob("*.json")
        if f.is_file() and SAFE_CHUNK.match(f.name)
    )

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

    meta = {}
    visitor, url = payload.get("visitor_id"), payload.get("url")
    if isinstance(visitor, str) and SAFE_ID.match(visitor):
        meta["visitor_id"] = visitor
    if isinstance(url, str) and url:
        meta["url"] = url[:2048]
    meta_path = session_dir / VISITOR_FILE
    if meta and not meta_path.is_file():
        meta_path.write_text(json.dumps(meta))

    now = int(time.time() * 1000)
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    window_prefix = re.sub(r"[^a-f0-9]", "", str(payload.get("window_id") or ""))[:8]
    parts = [str(now), window_prefix, suffix]
    chunk_path = session_dir / ("-".join(p for p in parts if p) + ".json")
    chunk_path.write_text(
        json.dumps(
            {
                "window_id": payload.get("window_id"),
                "url": payload.get("url"),
                "received_at": now,
                "events": events,
            }
        )
    )
    interactions = index_interactions(session_dir, events)
    device = index_device(session_dir, events)
    size = chunk_path.stat().st_size
    first_ts, last_ts = event_span(events) or (now, now)
    state_path = session_dir / WINDOW_STATE_FILE
    try:
        states = json.loads(state_path.read_text())
    except Exception:
        states = {}
    state = states.setdefault(str(payload.get("window_id") or "default"), {})
    active_ts = last_active_scan(events, state)
    if active_ts:
        state["active"] = max(state.get("active", 0), active_ts)
    state["first"] = min(state.get("first", first_ts), first_ts)
    state["last"] = max(state.get("last", last_ts), last_ts)
    state["bytes"] = state.get("bytes", 0) + size
    state_path.write_text(json.dumps(states))
    index_ingest(
        pool, session_id, first_ts, last_ts, active_ts or 0, window_duration(states),
        len(states), size, meta, interactions, device,
    )
    _pool_bytes[pool] = pool_bytes(pool) + size

    return {"status": "ok", "stored": len(events)}

ORDER_FILE = "order.json"
ORDER_MAX_BODY_BYTES = 4096

VISITOR_FILE = "visitor.json"

INTERACTIONS_FILE = "interactions.json"

DEVICE_FILE = "device.json"

def index_device(session_dir: Path, events: list) -> Optional[dict]:
    path = session_dir / DEVICE_FILE
    if path.exists():
        return None
    for event in events:
        if not isinstance(event, dict) or event.get("type") != 4:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        width, height = data.get("width"), data.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0:
            device = {"width": width, "height": height}
            path.write_text(json.dumps(device))
            return device
    return None

def index_interactions(session_dir: Path, events: list) -> Optional[dict]:
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
        return None
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
    return counts

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
    index_order(pool, session_id, order)
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
    # Reported, not health-gating: analysis is an optional extra, and a box
    # without a key or a browser is still a perfectly healthy recorder.
    checks["analysis_configured"] = analyze.ANALYSIS_ENABLED
    checks["analysis_runtime_present"] = analyze.runtime_present()

    informational = ("replay_auth_enabled", "analysis_configured", "analysis_runtime_present")
    healthy = all(v for k, v in checks.items() if k not in informational)
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
    if not project:
        return DATA_DIR
    if SAFE_ID.match(project) and project in load_projects():
        return project_sessions_dir(project)
    return None

INDEX_PATH = DATA_ROOT / "index.db"
_index_lock = threading.Lock()
_index_conn: Optional[sqlite3.Connection] = None

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

def ensure_index_schema(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "attribution" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN attribution TEXT")
    if "last_active" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_active INTEGER NOT NULL DEFAULT 0")
    if "duration" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN duration INTEGER")
    if "windows" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN windows INTEGER")

def index_conn() -> sqlite3.Connection:
    global _index_conn
    if _index_conn is None:
        conn = sqlite3.connect(INDEX_PATH, check_same_thread=False)
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE IF NOT EXISTS sessions (
                pool TEXT NOT NULL,
                session_id TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL,
                bytes INTEGER NOT NULL,
                visitor_id TEXT,
                url TEXT,
                "order" TEXT,
                interactions TEXT,
                device TEXT,
                attribution TEXT,
                PRIMARY KEY (pool, session_id)
            );
            CREATE INDEX IF NOT EXISTS sessions_recency
                ON sessions (pool, last_seen DESC, session_id DESC);
            """
        )
        ensure_index_schema(conn)
        conn.commit()
        _index_conn = conn
    return _index_conn

def pool_key(pool: Path) -> str:
    return "" if pool == DATA_DIR else pool.parent.name

def index_ingest(pool, session_id, first_ts, last_ts, active_ts, duration, windows, size, meta, interactions, device) -> None:
    attribution = parse_attribution(meta.get("url"))
    with _index_lock:
        conn = index_conn()
        conn.execute(
            """
            INSERT INTO sessions
                (pool, session_id, first_seen, last_seen, last_active, duration, windows, chunk_count, bytes,
                 visitor_id, url, interactions, device, attribution)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (pool, session_id) DO UPDATE SET
                first_seen = min(first_seen, excluded.first_seen),
                last_seen = max(last_seen, excluded.last_seen),
                last_active = max(coalesce(last_active, 0), excluded.last_active),
                duration = coalesce(excluded.duration, duration),
                windows = max(coalesce(windows, 1), excluded.windows),
                chunk_count = chunk_count + 1,
                bytes = bytes + excluded.bytes,
                visitor_id = coalesce(visitor_id, excluded.visitor_id),
                url = coalesce(url, excluded.url),
                interactions = coalesce(excluded.interactions, interactions),
                device = coalesce(device, excluded.device),
                attribution = coalesce(attribution, excluded.attribution)
            """,
            (
                pool_key(pool), session_id, first_ts, last_ts, active_ts, duration, windows, size,
                meta.get("visitor_id"), meta.get("url"),
                json.dumps(interactions) if interactions else None,
                json.dumps(device) if device else None,
                json.dumps(attribution) if attribution else None,
            ),
        )
        conn.commit()

def index_order(pool: Path, session_id: str, order: dict) -> None:
    with _index_lock:
        conn = index_conn()
        conn.execute(
            'UPDATE sessions SET "order" = ? WHERE pool = ? AND session_id = ?',
            (json.dumps(order), pool_key(pool), session_id),
        )
        conn.commit()

def index_forget(pool: Path, session_id: Optional[str] = None) -> None:
    with _index_lock:
        conn = index_conn()
        if session_id is None:
            conn.execute("DELETE FROM sessions WHERE pool = ?", (pool_key(pool),))
        else:
            conn.execute(
                "DELETE FROM sessions WHERE pool = ? AND session_id = ?",
                (pool_key(pool), session_id),
            )
        conn.commit()

def session_info(entry: Path) -> Optional[dict]:
    chunks = chunk_files(entry)
    if not chunks:
        return None
    first = last = None
    active = 0
    states = {}
    for chunk in chunks:
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

def rebuild_index(pool: Path) -> int:
    infos = []
    for entry in pool.iterdir() if pool.is_dir() else []:
        if not entry.is_dir():
            continue
        info = session_info(entry)
        if info:
            infos.append(info)
    key = pool_key(pool)
    with _index_lock:
        conn = index_conn()
        keep = {i["session_id"] for i in infos}
        stale = [
            (key, sid)
            for (sid,) in conn.execute("SELECT session_id FROM sessions WHERE pool = ?", (key,))
            if sid not in keep
        ]
        conn.executemany("DELETE FROM sessions WHERE pool = ? AND session_id = ?", stale)
        conn.executemany(
            """
            INSERT INTO sessions
                (pool, session_id, first_seen, last_seen, last_active, duration, windows, chunk_count, bytes,
                 visitor_id, url, "order", interactions, device, attribution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (pool, session_id) DO UPDATE SET
                first_seen = min(first_seen, excluded.first_seen),
                last_seen = max(last_seen, excluded.last_seen),
                last_active = max(coalesce(last_active, 0), excluded.last_active),
                duration = coalesce(excluded.duration, duration),
                windows = max(coalesce(windows, 1), excluded.windows),
                chunk_count = max(chunk_count, excluded.chunk_count),
                bytes = max(bytes, excluded.bytes),
                visitor_id = coalesce(excluded.visitor_id, visitor_id),
                url = coalesce(excluded.url, url),
                "order" = coalesce(excluded."order", "order"),
                interactions = coalesce(excluded.interactions, interactions),
                device = coalesce(excluded.device, device),
                attribution = coalesce(excluded.attribution, attribution)
            """,
            [
                (
                    key, i["session_id"], i["first_seen"], i["last_seen"], i.get("last_active", 0),
                    i.get("duration"), i.get("windows", 1), i["chunk_count"], i["bytes"],
                    i.get("visitor_id"), i.get("url"),
                    json.dumps(i["order"]) if "order" in i else None,
                    json.dumps(i["interactions"]) if "interactions" in i else None,
                    json.dumps(i["device"]) if "device" in i else None,
                    json.dumps(i["attribution"]) if "attribution" in i else None,
                )
                for i in infos
            ],
        )
        conn.commit()
    return len(infos)

def reconcile_index() -> None:
    for pool in session_pools():
        key = pool_key(pool)
        with _index_lock:
            (count,) = index_conn().execute(
                "SELECT count(*) FROM sessions WHERE pool = ?", (key,)
            ).fetchone()
        if count == 0 and any(entry.is_dir() for entry in pool.iterdir()):
            print(f"index: rebuilt '{key or '(no project)'}' — {rebuild_index(pool)} session(s)")
    backfill_attribution()

def backfill_attribution() -> int:
    """Fill attribution from stored entry URLs for rows indexed before this column existed."""
    updated = 0
    with _index_lock:
        conn = index_conn()
        rows = conn.execute(
            "SELECT pool, session_id, url FROM sessions "
            "WHERE attribution IS NULL AND url IS NOT NULL AND url != ''"
        ).fetchall()
        for key, sid, url in rows:
            attr = parse_attribution(url)
            if not attr:
                continue
            conn.execute(
                "UPDATE sessions SET attribution = ? WHERE pool = ? AND session_id = ?",
                (json.dumps(attr), key, sid),
            )
            updated += 1
        if updated:
            conn.commit()
    return updated

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
        "analysis_enabled": analyze.ANALYSIS_ENABLED and analyze.runtime_present(),
        **({"email": email} if email else {}),
    }

SESSION_FILTERS = {
    "checkout": '"order" IS NOT NULL',
    "purchased": "json_extract(\"order\", '$.purchased_at') IS NOT NULL",
    "rage": "json_extract(interactions, '$.rage') > 0",
}

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
    pool = resolve_pool(project)
    where, params = ["pool = ?"], [pool_key(pool)]
    if filter in SESSION_FILTERS:
        where.append(SESSION_FILTERS[filter])
    if q:
        like = f"%{q}%"
        where.append("(session_id LIKE ? OR visitor_id LIKE ? OR url LIKE ?)")
        params += [like, like, like]
    with _index_lock:
        conn = index_conn()
        (total,) = conn.execute(
            f"SELECT count(*) FROM sessions WHERE {' AND '.join(where)}", params
        ).fetchone()
        if cursor:
            try:
                seen, _, sid = cursor.partition(":")
                where.append("(last_seen < ? OR (last_seen = ? AND session_id < ?))")
                params += [int(seen), int(seen), sid]
            except ValueError:
                raise ApiError(400, "bad cursor")
        rows = conn.execute(
            f"""
            SELECT session_id, first_seen, last_seen, last_active, duration, windows, chunk_count, bytes,
                   visitor_id, url, "order", interactions, device, attribution
            FROM sessions WHERE {' AND '.join(where)}
            ORDER BY last_seen DESC, session_id DESC LIMIT ?
            """,
            params + [limit + 1],
        ).fetchall()
    more = len(rows) > limit
    sessions = []
    for sid, first, last, active, duration, windows, count, size, visitor, url, order, interactions, device, attribution in rows[:limit]:
        info = {
            "session_id": sid,
            "first_seen": first,
            "last_seen": last,
            "last_active": active or 0,
            "duration": duration,
            "windows": windows or 1,
            "chunk_count": count,
            "bytes": size,
        }
        if visitor:
            info["visitor_id"] = visitor
        if url:
            info["url"] = url
        for key, value in (
            ("order", order),
            ("interactions", interactions),
            ("device", device),
            ("attribution", attribution),
        ):
            if value:
                info[key] = json.loads(value)
        sessions.append(info)
    next_cursor = f"{sessions[-1]['last_seen']}:{sessions[-1]['session_id']}" if more else None
    return {"sessions": sessions, "total": total, "cursor": next_cursor}

@app.get("/api/sessions/{session_id}")
def get_session(request: Request, session_id: str, project: Optional[str] = Query(None)):
    require_read_access(request, project)
    info = session_info(resolve_session(session_id, project))
    if info is None:
        raise ApiError(404, "not found")
    return info

@app.delete("/api/sessions/{session_id}")
def delete_session(request: Request, session_id: str, project: Optional[str] = Query(None)):

    require_read_access(request, project)
    session_dir = resolve_session(session_id, project)
    freed = dir_bytes(session_dir)
    shutil.rmtree(session_dir, ignore_errors=True)
    index_forget(session_dir.parent, session_id)
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

_analysis_tasks: dict = {}
# Concurrency lives in analyze.py: one Chromium at a time for reconstruction,
# a few overlapping Gemini calls. Both gates are shared by every caller here.
_cohort_tasks: dict = {}

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
    key = _cohort_key(pool, cohort_id)
    try:
        cohort = analyze.read_cohort(pool, cohort_id)
        if not cohort:
            return
        cohort = {**cohort, "status": "running"}
        analyze.write_cohort(pool, cohort)

        pending = []
        for entry in cohort.get("sessions") or []:
            session_dir = pool / entry["session_id"]
            if not force and analyze.read_analysis(session_dir):
                entry["status"] = "done"
                entry["analysis"] = analyze.ANALYSIS_FILE
                entry.pop("error", None)
                continue
            entry["status"] = "queued"
            entry.pop("error", None)
            pending.append((entry, session_dir))
        analyze.write_cohort(pool, cohort)

        # Fan out and let analyze.py's semaphores pace the work — extraction
        # still runs one at a time, but a session waiting on Gemini no longer
        # blocks the next session's reconstruction.
        async def run_one(entry: dict, session_dir: Path) -> None:
            entry["status"] = "running"
            analyze.write_cohort(pool, cohort)
            try:
                await _await_session_analysis(session_dir, force=force)
                entry["status"] = "done"
                entry["analysis"] = analyze.ANALYSIS_FILE
                entry.pop("error", None)
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = getattr(exc, "message", None) or str(exc) or "analysis failed"
            analyze.write_cohort(pool, cohort)

        if pending:
            await asyncio.gather(
                *(run_one(entry, session_dir) for entry, session_dir in pending),
                return_exceptions=True,
            )

        final = analyze.enrich_cohort_status(pool, analyze.read_cohort(pool, cohort_id) or cohort)
        analyze.write_cohort(pool, final)
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
    if live.get("sessions") != cohort.get("sessions") or live.get("status") != cohort.get("status"):
        analyze.write_cohort(pool, live)
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


@app.post("/api/cohorts")
async def create_cohort(
    request: Request,
    project: Optional[str] = Query(None),
    purchased: int = Query(5, ge=0, le=50),
    all_n: int = Query(5, ge=0, le=50, alias="all"),
    force: bool = Query(False),
):
    """Sample a new cohort. Each call mints its own id — cohorts accumulate."""
    require_read_access(request, project)
    _require_analysis_runtime()
    pool = resolve_pool(project)

    with _index_lock:
        conn = index_conn()
        cohort = analyze.build_cohort(conn, pool_key(pool), purchased, all_n)

    if not cohort["sessions"]:
        raise ApiError(400, "no sessions matched the purchased/all sample")

    analyze.write_cohort(pool, {**cohort, "status": "running"})
    key = _cohort_key(pool, cohort["id"])
    task = asyncio.create_task(_run_cohort_job(pool, cohort["id"], force=force))
    _cohort_tasks[key] = task
    return JSONResponse(
        cohort_response(pool, analyze.enrich_cohort_status(pool, cohort)),
        status_code=202,
    )


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
        analyze.write_cohort(pool, analyze.enrich_cohort_status(pool, cohort))
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


def _attribution_map(pool: Path) -> dict:
    """session_id → attribution dict from the index."""
    with _index_lock:
        rows = index_conn().execute(
            "SELECT session_id, attribution FROM sessions WHERE pool = ?",
            (pool_key(pool),),
        ).fetchall()
    out = {}
    for sid, raw in rows:
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except Exception:
            continue
        if isinstance(value, dict) and value:
            out[sid] = value
    return out


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


def _session_facts(pool: Path) -> dict:
    """session_id → {attribution, device, outcome, page} straight from the index."""
    with _index_lock:
        rows = index_conn().execute(
            'SELECT session_id, attribution, device, "order", url '
            "FROM sessions WHERE pool = ?",
            (pool_key(pool),),
        ).fetchall()

    def _json(raw):
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    out = {}
    for sid, attribution, device, order, url in rows:
        out[sid] = {
            "attribution": _json(attribution),
            "device": analyze.device_class(_json(device)),
            "outcome": analyze.outcome_class(_json(order)),
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
    rows = _cut_rows(pool, cohort)
    counts = {
        "outcome": {},
        "device": {},
        "page": {},
        "utm_source": {},
        "utm_campaign": {},
    }
    for row in rows:
        counts["outcome"][row.get("outcome") or "rest"] = (
            counts["outcome"].get(row.get("outcome") or "rest", 0) + 1
        )
        if row.get("device"):
            counts["device"][row["device"]] = counts["device"].get(row["device"], 0) + 1
        if row.get("page"):
            counts["page"][row["page"]] = counts["page"].get(row["page"], 0) + 1
        attrs = row.get("attribution") or {}
        for key in ("utm_source", "utm_campaign"):
            value = str(attrs.get(key) or "").strip().lower()
            if value:
                counts[key][value] = counts[key].get(value, 0) + 1

    def _listed(field, order=None):
        items = counts[field]
        keys = order if order else sorted(items, key=lambda k: (-items[k], k))
        return [
            {"value": k, "count": items[k], "label": _facet_label(field, k)}
            for k in keys
            if items.get(k)
        ]

    return {
        "n": len(rows),
        "outcome": _listed("outcome", order=analyze.OUTCOME_VALUES),
        "device": _listed("device", order=analyze.DEVICE_VALUES),
        "page": _listed("page"),
        "utm_source": _listed("utm_source"),
        "utm_campaign": _listed("utm_campaign"),
    }


def _facet_label(field: str, value: str) -> str:
    if field == "outcome":
        return analyze.OUTCOME_LABELS.get(value, value)
    return value


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

    if RETENTION_DAYS <= 0:
        return 0
    cutoff_ms = (time.time() - RETENTION_DAYS * 86400) * 1000
    removed = 0
    for pool in session_pools():
        alive = 0
        for entry in pool.iterdir():
            if not entry.is_dir():
                continue
            chunks = chunk_files(entry)
            newest = chunk_timestamp(chunks[-1]) if chunks else 0
            if newest < cutoff_ms:
                shutil.rmtree(entry, ignore_errors=True)
                index_forget(pool, entry.name)
                removed += 1
            elif chunks:
                alive += 1
        key = pool_key(pool)
        with _index_lock:
            (indexed,) = index_conn().execute(
                "SELECT count(*) FROM sessions WHERE pool = ?", (key,)
            ).fetchone()
        if indexed != alive:
            print(
                f"index: drift in '{key or '(no project)'}' "
                f"({indexed} indexed, {alive} on disk) — rebuilt {rebuild_index(pool)}"
            )
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
        index_forget(pool)
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
    elif argv[:1] == ["reindex"]:
        for pool in session_pools():
            print(f"{pool_key(pool) or '(no project)'}: {rebuild_index(pool)} session(s)")
    else:
        print("usage: python server.py add-project <name>")
        print("       python server.py list-projects")
        print("       python server.py remove-project <token> [--purge]")
        print("       python server.py set-org <token> <workos_org_id>")
        print("       python server.py set-domains <token> [domain ...]")
        print("       python server.py invite <email> <project_token_or_org_id>")
        print("       python server.py reindex")
        print("run the server with: .venv/bin/uvicorn server:app --port 3000")
