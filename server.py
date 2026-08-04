"""Ingest + replay server: the recorder POSTs gzipped event batches to
/ingest, each batch is stored as an immutable chunk on disk, and the replay
UI reads sessions back through a small JSON API.

Run: .venv/bin/uvicorn server:app --port 3000
"""

import asyncio
import base64
import json
import os
import random
import re
import secrets
import shutil
import string
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

@asynccontextmanager
async def lifespan(_app):
    """Runs at startup, so it may reference module state defined further down."""
    if REPLAY_AUTH_DISABLED:
        print(
            "warning: REPLAY_AUTH_DISABLED=1 — the replay UI and read API are "
            "open to anyone who can reach this server"
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
    """The read side requires REPLAY_PASSWORD; with none set it refuses to
    serve rather than falling open. The write side stays open for the snippet."""
    # Normalized so /Api/ and //replay.html can't slip past a check that
    # StaticFiles would still serve them from.
    path = re.sub(r"/+", "/", request.url.path).lower()
    protected = path.startswith("/api/") or path == "/replay.html"
    if protected and not REPLAY_AUTH_DISABLED:
        if not REPLAY_PASSWORD:
            return Response(
                "Replay access is unconfigured. Set REPLAY_PASSWORD to a secret, "
                "or REPLAY_AUTH_DISABLED=1 to serve recordings to anyone.\n",
                status_code=503,
                media_type="text/plain",
            )
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
    checks["replay_auth_enabled"] = bool(REPLAY_PASSWORD) and not REPLAY_AUTH_DISABLED

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


def read_pool(project: Optional[str]) -> Optional[Path]:
    """Sessions directory for a read-API request; None means unknown project.
    No param reads the legacy single-tenant pool."""
    if not project:
        return DATA_DIR
    if SAFE_ID.match(project) and project in load_projects():
        return project_sessions_dir(project)
    return None


@app.get("/api/projects")
def list_projects():
    """Projects for the replay UI's selector. The legacy pool shows up as a
    blank token when it still holds recordings, so pre-project sessions
    stay reachable after tokens are introduced."""
    projects = [
        {"token": token, "name": (meta or {}).get("name") or token}
        for token, meta in load_projects().items()
    ]
    if projects and any(entry.is_dir() for entry in DATA_DIR.iterdir()):
        projects.append({"token": "", "name": "(no project)"})
    return {"projects": projects}


@app.get("/api/sessions")
def list_sessions(project: Optional[str] = Query(None)):
    pool = resolve_pool(project)
    sessions = []
    for entry in pool.iterdir() if pool.is_dir() else []:
        if not entry.is_dir():
            continue
        chunks = sorted(entry.glob("*.json"), key=chunk_timestamp)
        if not chunks:
            continue
        sessions.append(
            {
                "session_id": entry.name,
                "first_seen": chunk_timestamp(chunks[0]),
                "last_seen": chunk_timestamp(chunks[-1]),
                "chunk_count": len(chunks),
                "bytes": sum(c.stat().st_size for c in chunks),
            }
        )
    sessions.sort(key=lambda s: s["last_seen"], reverse=True)
    return {"sessions": sessions}


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, project: Optional[str] = Query(None)):
    """Irreversible. DELETE is absent from the CORS allowlist, so only the
    replay UI's own origin can reach it."""
    session_dir = resolve_session(session_id, project)
    freed = dir_bytes(session_dir)
    shutil.rmtree(session_dir, ignore_errors=True)
    _pool_bytes.pop(session_dir.parent, None)  # freed bytes belong back in the quota
    print(f"deleted session {session_id} ({freed} bytes)")
    return {"status": "deleted", "session_id": session_id, "bytes_freed": freed}


@app.get("/api/sessions/{session_id}/chunks")
def session_chunks(session_id: str, project: Optional[str] = Query(None)):
    """Ordered chunk manifest; the player fetches chunks one by one."""
    chunks = sorted(resolve_session(session_id, project).glob("*.json"), key=chunk_timestamp)
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
def session_chunk(session_id: str, chunk_name: str, project: Optional[str] = Query(None)):
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
            chunks = sorted(entry.glob("*.json"), key=chunk_timestamp)
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
        print(f"{token}  {name}  —  {count} session(s), {used / 1024**2:.1f} MB")


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
    else:
        print("usage: python server.py add-project <name>")
        print("       python server.py list-projects")
        print("       python server.py remove-project <token> [--purge]")
        print("run the server with: .venv/bin/uvicorn server:app --port 3000")
