"""Reading recordings: the session list, one session, its chunks, its analysis.

All authenticated through the readable_project dependency, which resolves the
project and refuses it in one step. Declared in the signature rather than called
at the top of the body, so a handler cannot forget it and still have a project.

The index is the record of truth; legacy_sessions is the fallback for recordings
made before it existed. That fallback is bridge code — see capture/legacy_sessions.py.
"""

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .. import analysis_port as ap
from .. import jobs, legacy_sessions, projects, quota, sessionstore, storage
from ..attribution import parse_attribution
from ..errors import ApiError
from ..paths import SAFE_CHUNK, SAFE_ID, chunk_timestamp, chunk_window
from .deps import allowed_projects, readable_project, session_dir

router = APIRouter()


@router.get("/api/projects")
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


@router.get("/api/sessions")
def list_sessions(
    found: projects.Project = Depends(readable_project),
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = Query(None),
    filter: str | None = Query(None),
    q: str | None = Query(None),
):
    try:
        return sessionstore.listing(found.id, limit=limit, cursor=cursor, filter=filter, q=q)
    except ValueError as exc:
        raise ApiError(400, "bad cursor") from exc


@router.get("/api/sessions/{session_id}")
def get_session(session_id: str, found: projects.Project = Depends(readable_project)):
    info = sessionstore.get(found.id, session_id) or legacy_sessions.session_info(
        session_dir(found, session_id)
    )
    if info is None:
        raise ApiError(404, "not found")
    return info


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, found: projects.Project = Depends(readable_project)):
    # Called for its 404: deleting an unknown session must not report success.
    session_dir(found, session_id)
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
    quota.forget(found.id)
    print(f"deleted session {session_id} ({freed} bytes)")
    return {"status": "deleted", "session_id": session_id, "bytes_freed": freed}


@router.get("/api/sessions/{session_id}/chunks")
def session_chunks(session_id: str, found: projects.Project = Depends(readable_project)):
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


@router.get("/api/sessions/{session_id}/chunks/{chunk_name}")
def session_chunk(
    session_id: str,
    chunk_name: str,
    found: projects.Project = Depends(readable_project),
):
    if not SAFE_ID.match(session_id) or not SAFE_CHUNK.match(chunk_name):
        raise ApiError(400, "invalid id")
    prefix = found.storage_prefix

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


@router.get("/api/sessions/{session_id}/analysis")
def get_analysis(session_id: str, found: projects.Project = Depends(readable_project)):
    directory = session_dir(found, session_id)
    cached = ap.session_analysis(found, session_id)
    if cached:
        if "status" not in cached:
            cached = {**cached, "status": "done"}
        info = legacy_sessions.session_info(directory)
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


@router.post("/api/sessions/{session_id}/analysis")
async def create_analysis(
    session_id: str,
    force: bool = Query(False),
    found: projects.Project = Depends(readable_project),
):
    jobs.require_runtime()
    session_dir(found, session_id)
    result = await jobs.start_session(found, session_id, force=force)
    if result.get("accepted"):
        return JSONResponse({"status": "running"}, status_code=202)
    return result
