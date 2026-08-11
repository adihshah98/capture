"""Recording endpoints: the two the visitor's browser posts to.

Unauthenticated by design — a recorder on a customer's page cannot hold a secret.
The project token in the query string plus the allowed-domain check are what tie a
batch to a tenant.

Both refuse outright when the server is READ_ONLY: a mirror pointed at someone
else's database must not write to it.
"""

import asyncio
import json
import random
import re
import shutil
import string
import time
import urllib.parse
import zlib

from fastapi import APIRouter, Query, Request

from .. import config, paths, projects, quota, sessionstore, storage
from ..attribution import parse_attribution
from ..errors import ApiError
from ..events import count_clicks, event_span, last_active_scan, read_device
from ..paths import SAFE_ID

router = APIRouter()


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


@router.post("/ingest")
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
    if config.MAX_PROJECT_BYTES and quota.used(project) >= config.MAX_PROJECT_BYTES:
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
    quota.add(project.id, size)

    return {"status": "ok", "stored": len(events)}


@router.post("/order")
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
