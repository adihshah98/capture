"""Ingest + replay server (FastAPI port of the original server.js).

The file-system analog of PostHog's pipeline: the recorder POSTs gzipped
event batches to /ingest, each batch is stored as an immutable chunk under
data/sessions/<session_id>/, and the replay UI reads sessions back through
a small JSON API. Swap the chunk store for S3/ClickHouse when it outgrows
a single box — the wire format stays the same.

Run: .venv/bin/uvicorn server:app --port 3000
"""

import gzip
import json
import random
import re
import string
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="capture")


@app.middleware("http")
async def revalidate_bundles(request: Request, call_next):
    """Browsers cache script bundles indefinitely by default, so instrumented
    pages would keep running a stale recorder after a deploy. no-cache forces
    a revalidation (cheap 304 when unchanged) on every load."""
    response = await call_next(request)
    if request.url.path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "sessions"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY_BYTES = 50 * 1024 * 1024


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


# Registered last so /ingest and /api/* win; serves index.html at "/".
app.mount("/", StaticFiles(directory=BASE_DIR / "public", html=True), name="static")
