"""Ingest + replay server (FastAPI port of the original server.js).

The file-system analog of PostHog's pipeline: the recorder POSTs gzipped
event batches to /ingest, each batch is stored as an immutable chunk under
data/sessions/<session_id>/, and the replay UI reads sessions back through
a small JSON API. Swap the chunk store for S3/ClickHouse when it outgrows
a single box — the wire format stays the same.

Run: .venv/bin/uvicorn server:app --port 3000
"""

import asyncio
import gzip
import json
import os
import random
import re
import shutil
import string
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="capture")


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

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_CHUNK = re.compile(r"^\d{1,16}-[a-z0-9]{6}\.json$")
MAX_BODY_BYTES = 50 * 1024 * 1024

# Sessions whose newest chunk is older than this are deleted by the daily
# retention sweep. Set RETENTION_DAYS=0 to keep everything forever.
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))

# Remote recording config served to clients (PostHog's remote-config analog).
# Edit this file to change recording behavior fleet-wide — clients re-fetch
# after their cache TTL (1 h) without any page redeploy.
CONFIG_PATH = BASE_DIR / "data" / "config.json"


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@app.post("/ingest")
async def ingest(
    request: Request,
    session_id: str = Query(""),
    compression: Optional[str] = Query(None),
):
    if not SAFE_ID.match(session_id):
        return error(400, "invalid session_id")

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return error(413, "payload too large")

    try:
        raw = gzip.decompress(body) if compression == "gzip-js" else body
        payload = json.loads(raw)
    except Exception:
        return error(400, "bad payload")

    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return error(400, "no events")

    session_dir = DATA_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # Immutable chunk per batch; timestamp prefix keeps chunks ordered.
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    chunk_path = session_dir / f"{int(time.time() * 1000)}-{suffix}.json"
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

    return {"status": "ok", "stored": len(events)}


# ---------------------------------------------------------------------------
# Remote config
# ---------------------------------------------------------------------------


@app.get("/config")
def remote_config():
    """Recording settings for the client, from data/config.json. Missing or
    invalid file degrades to {} — a bad config must never stop recording."""
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        if not isinstance(cfg, dict):
            raise ValueError("config.json must be a JSON object")
    except FileNotFoundError:
        cfg = {}
    except Exception as exc:
        print(f"config: serving defaults, data/config.json unusable: {exc}")
        cfg = {}
    response = JSONResponse(cfg)
    response.headers["Cache-Control"] = "no-cache"  # clients cache via their own TTL
    return response


# ---------------------------------------------------------------------------
# Read API for the replay UI
# ---------------------------------------------------------------------------


def chunk_timestamp(path: Path) -> int:
    try:
        return int(path.name.split("-")[0])
    except ValueError:
        return 0


@app.get("/api/sessions")
def list_sessions():
    sessions = []
    for entry in DATA_DIR.iterdir():
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
def session_chunks(session_id: str):
    """Ordered chunk manifest for lazy loading — the player fetches chunks
    one by one instead of the server concatenating the whole session."""
    if not SAFE_ID.match(session_id):
        return error(400, "invalid session_id")
    session_dir = DATA_DIR / session_id
    if not session_dir.is_dir():
        return error(404, "not found")

    chunks = sorted(session_dir.glob("*.json"), key=chunk_timestamp)
    url = None
    if chunks:  # only the first chunk is opened; the rest stay on disk
        try:
            url = json.loads(chunks[0].read_text()).get("url")
        except Exception:
            pass
    return {
        "session_id": session_id,
        "url": url,
        "chunks": [
            {"name": c.name, "timestamp": chunk_timestamp(c), "bytes": c.stat().st_size}
            for c in chunks
        ],
    }


@app.get("/api/sessions/{session_id}/chunks/{chunk_name}")
def session_chunk(session_id: str, chunk_name: str):
    """A single immutable chunk, served straight from disk."""
    if not SAFE_ID.match(session_id) or not SAFE_CHUNK.match(chunk_name):
        return error(400, "invalid id")
    path = DATA_DIR / session_id / chunk_name
    if not path.is_file():
        return error(404, "not found")
    # Chunks are immutable (timestamp + random suffix names), so let the
    # browser cache them for replay scrubbing across visits.
    return FileResponse(
        path, media_type="application/json", headers={"Cache-Control": "max-age=86400"}
    )


@app.get("/api/sessions/{session_id}/events")
def session_events(session_id: str):
    if not SAFE_ID.match(session_id):
        return error(400, "invalid session_id")
    session_dir = DATA_DIR / session_id
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


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def sweep_expired_sessions() -> int:
    """Delete session directories whose newest chunk is past retention.
    Empty directories (no chunks) are treated as expired."""
    if RETENTION_DAYS <= 0:
        return 0
    cutoff_ms = (time.time() - RETENTION_DAYS * 86400) * 1000
    removed = 0
    for entry in DATA_DIR.iterdir():
        if not entry.is_dir():
            continue
        chunks = sorted(entry.glob("*.json"), key=chunk_timestamp)
        newest = chunk_timestamp(chunks[-1]) if chunks else 0
        if newest < cutoff_ms:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


@app.on_event("startup")
async def start_retention_loop():
    async def loop():
        while True:
            removed = await asyncio.to_thread(sweep_expired_sessions)
            if removed:
                print(f"retention: removed {removed} expired session(s)")
            await asyncio.sleep(24 * 3600)

    asyncio.create_task(loop())


# Registered last so /ingest and /api/* win; serves index.html at "/".
app.mount("/", StaticFiles(directory=BASE_DIR / "public", html=True), name="static")
