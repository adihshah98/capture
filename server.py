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
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="capture")

# The snippet runs on other people's pages, so ingest/config/bundles must be
# reachable cross-origin. A wildcard origin is safe here: the read side is
# gated by REPLAY_PASSWORD, which cross-origin callers can't present.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def replay_auth(request: Request, call_next):
    """The write side (/ingest, /config, bundles) stays open — the snippet
    runs on other people's pages. The read side (replay UI + /api) exposes
    recordings, so it requires REPLAY_PASSWORD: with none set it refuses to
    serve rather than falling open."""
    # Normalized so /Api/, //replay.html and friends can't slip past the check
    # on filesystems StaticFiles will still serve them from.
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
    """Browsers heuristically cache static assets with no Cache-Control, so
    pages would keep running stale bundles/markup after a deploy. no-cache
    forces a revalidation (cheap 304 when unchanged) on every load. Endpoints
    that set their own Cache-Control (immutable chunks) are left alone."""
    response = await call_next(request)
    is_static = request.url.path.endswith((".js", ".css", ".html")) or request.url.path == "/"
    if is_static and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "sessions"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Project registry: token -> {"name": ...}, managed by `python server.py
# add-project <name>`. No projects configured = single-tenant mode: tokens
# are ignored and everything lands in data/sessions.
PROJECTS_PATH = BASE_DIR / "data" / "projects.json"
PROJECTS_DIR = BASE_DIR / "data" / "projects"

# Password for the replay UI + read API (HTTP Basic, any username). Unset
# means the read side is refused entirely; REPLAY_AUTH_DISABLED=1 opts out
# for local development.
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
# Bounding the compressed body alone is not enough: a well-formed 50 MB gzip
# expands to gigabytes. A real batch is ~1 MB of JSON, so this is generous.
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


# Pool sizes are scanned once, then tracked as chunks are written — walking
# every chunk on each ingest would cost more than the write itself.
_pool_bytes: dict = {}


def pool_bytes(pool: Path) -> int:
    if pool not in _pool_bytes:
        _pool_bytes[pool] = sum(f.stat().st_size for f in pool.rglob("*.json") if f.is_file())
    return _pool_bytes[pool]

# Global remote recording config; editing it changes recording behavior
# fleet-wide without page redeploys (clients re-fetch after a 1 h TTL).
CONFIG_PATH = BASE_DIR / "data" / "config.json"


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@app.post("/ingest")
async def ingest(
    request: Request,
    session_id: str = Query(""),
    compression: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
):
    if not SAFE_ID.match(session_id):
        return error(400, "invalid session_id")

    pool = resolve_sessions_dir(token, load_projects())
    if pool is None:
        return error(401, "unknown project token")

    # Checked before the body is read so an abusive client is cheap to reject.
    # 507 is retryable (our problem, may clear); 429 tells the recorder to drop
    # the batch rather than retry into a quota that only retention will free.
    if MIN_FREE_DISK_BYTES and shutil.disk_usage(BASE_DIR).free < MIN_FREE_DISK_BYTES:
        return error(507, "server storage full")
    if MAX_PROJECT_BYTES:
        # Cached after the first request; only the initial scan leaves the loop.
        used = _pool_bytes.get(pool)
        if used is None:
            used = await asyncio.to_thread(pool_bytes, pool)
        if used >= MAX_PROJECT_BYTES:
            return error(429, "project storage quota reached")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return error(413, "payload too large")

    try:
        raw = gunzip(body, MAX_DECOMPRESSED_BYTES) if compression == "gzip-js" else body
        payload = json.loads(raw)
    except Exception:
        return error(400, "bad payload")

    if not isinstance(payload, dict):
        return error(400, "bad payload")

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return error(400, "no events")

    session_dir = pool / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Timestamp prefix keeps chunks ordered; the window-id prefix lets the
    # manifest group chunks per tab without opening any chunk file.
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
    pool = read_pool(project)
    if pool is None:
        return error(404, "unknown project")
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


@app.get("/api/sessions/{session_id}/chunks")
def session_chunks(session_id: str, project: Optional[str] = Query(None)):
    """Ordered chunk manifest; the player fetches chunks one by one."""
    if not SAFE_ID.match(session_id):
        return error(400, "invalid session_id")
    pool = read_pool(project)
    if pool is None:
        return error(404, "unknown project")
    session_dir = pool / session_id
    if not session_dir.is_dir():
        return error(404, "not found")

    chunks = sorted(session_dir.glob("*.json"), key=chunk_timestamp)
    url = None
    if chunks:  # only the first chunk is opened; the rest stay on disk
        try:
            url = json.loads(chunks[0].read_text()).get("url")
        except Exception:
            pass
    windows = []  # ordered by first appearance; None = pre-window chunks
    for c in chunks:
        w = chunk_window(c)
        if w not in windows:
            windows.append(w)
    return {
        "session_id": session_id,
        "url": url,
        "windows": windows,
        "chunks": [
            {
                "name": c.name,
                "timestamp": chunk_timestamp(c),
                "bytes": c.stat().st_size,
                "window": chunk_window(c),
            }
            for c in chunks
        ],
    }


@app.get("/api/sessions/{session_id}/chunks/{chunk_name}")
def session_chunk(session_id: str, chunk_name: str, project: Optional[str] = Query(None)):
    if not SAFE_ID.match(session_id) or not SAFE_CHUNK.match(chunk_name):
        return error(400, "invalid id")
    pool = read_pool(project)
    if pool is None:
        return error(404, "unknown project")
    path = pool / session_id / chunk_name
    if not path.is_file():
        return error(404, "not found")
    # Chunks are immutable, so the browser may cache them for scrubbing
    # re-visits.
    return FileResponse(
        path, media_type="application/json", headers={"Cache-Control": "max-age=86400"}
    )


@app.get("/api/sessions/{session_id}/events")
def session_events(session_id: str, project: Optional[str] = Query(None)):
    if not SAFE_ID.match(session_id):
        return error(400, "invalid session_id")
    pool = read_pool(project)
    if pool is None:
        return error(404, "unknown project")
    session_dir = pool / session_id
    if not session_dir.is_dir():
        return error(404, "not found")

    events = []
    url = None
    for chunk in sorted(session_dir.glob("*.json"), key=chunk_timestamp):
        try:
            parsed = json.loads(chunk.read_text())
        except Exception:
            continue  # skip corrupt chunks rather than failing the session
        if url is None and parsed.get("url"):
            url = parsed["url"]
        chunk_events = parsed.get("events")
        if isinstance(chunk_events, list):
            events.extend(chunk_events)
    events.sort(key=lambda e: e.get("timestamp", 0))

    return {"session_id": session_id, "url": url, "count": len(events), "events": events}


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


@app.on_event("startup")
async def start_retention_loop():
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

    asyncio.create_task(loop())


# Registered last so /ingest and /api/* win; serves index.html at "/".
app.mount("/", StaticFiles(directory=BASE_DIR / "public", html=True), name="static")


def add_project(name: str) -> None:
    """Create a project token and print the one-tag snippet for the brand."""
    token = "cap_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=24))
    projects = load_projects()
    projects[token] = {"name": name, "created_at": int(time.time() * 1000)}
    PROJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROJECTS_PATH.write_text(json.dumps(projects, indent=2))
    project_sessions_dir(token).mkdir(parents=True, exist_ok=True)
    print(f"project '{name}' created — token: {token}")
    print("\nAdd this tag to the brand's site (replace the host with yours):\n")
    print(f'  <script src="https://capture.example.com/capture.js" data-token="{token}"></script>')
    print(
        "\nOptional per-project recording config: "
        f"data/projects/{token}/config.json (falls back to data/config.json)"
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "add-project":
        add_project(sys.argv[2])
    else:
        print("usage: python server.py add-project <name>")
        print("run the server with: .venv/bin/uvicorn server:app --port 3000")
