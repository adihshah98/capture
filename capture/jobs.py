"""Background analysis work: the task registry and the disk staging it needs.

Not a router. The lifespan starts and stops these, and both the sessions and the
cohorts routers queue into them, so it can live with none of the three — that
shared ownership is why this module exists at all.

Two things here are load-bearing and look incidental:

  * Tasks are keyed on (project_id, session_id) or (project_id, cohort_id), not on
    a resolved directory. The registry used to rebuild a Path from its own dict key
    to mark a job failed.
  * chunks_on_disk stages only what is missing and removes only what it staged. The
    analyser is a Node subprocess whose entire input contract is "a directory with
    chunk files in it", while the chunks live in S3. Widening the cleanup to
    "delete the directory's chunks" turns it into data loss for local installs, and
    adopting files it did not create lets two overlapping cohort runs delete each
    other's input mid-extraction. See docs/deep-dive/01-ingest-analyze-seam.md.

Everything is process-local, which is what keeps this server on one worker: a
second one would hold its own registry and double-run every job.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import NamedTuple

from . import analysis_port as ap
from . import projects, storage
from .errors import ApiError
from .fsutil import atomic_write_bytes


class _Running(NamedTuple):
    """The project is carried alongside the task so shutdown can report a job as
    failed without looking anything up — at shutdown the database may already be
    going away, and the registry used to reconstruct a Path from its own key."""

    project: projects.Project
    task: asyncio.Task


# Keyed by (project_id, session_id) rather than a resolved directory: the tenant
# is a project, and two projects must never collide on a session id.
analysis_tasks: dict = {}
# Concurrency lives in the analysis package: one Chromium at a time for
# reconstruction, a few overlapping Gemini calls. Both gates are shared here.
cohort_tasks: dict = {}


async def stop_all() -> None:
    """On a clean stop, leave no job claiming to be running with no task behind it."""
    pending = [r.task for r in analysis_tasks.values() if not r.task.done()]
    pending += [t for t in cohort_tasks.values() if not t.done()]
    for (_project_id, session_id), running in analysis_tasks.items():
        if not running.task.done():
            ap.set_job(running.project, session_id, "error", error="server restarted mid-analysis")
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        print(f"shutdown: cancelled {len(pending)} background job(s)")


def analysis_key(project: projects.Project, session_id: str) -> tuple:
    return (project.id, session_id)


def cohort_key(project: projects.Project, cohort_id: str) -> tuple:
    return (project.id, cohort_id)


def _stage_chunks(project, session_ids: list) -> list:
    staged = []
    for session_id in session_ids:
        directory = project.pool / session_id
        for chunk in storage.list_chunks(project.storage_prefix, session_id):
            path = directory / chunk.name
            if path.exists():
                continue
            body = storage.open_chunk(project.storage_prefix, session_id, chunk.name)
            if body is None:
                continue
            atomic_write_bytes(path, body)
            staged.append(path)
    return staged


def _unstage_chunks(paths: list) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


@asynccontextmanager
async def chunks_on_disk(project, session_ids: list):
    """analyze.py reads chunks as files, and writes analysis.json, the job status
    and the frame cache back into the same directory. Staging into a temp dir
    would carry those results off with it when the directory was cleaned up, so
    chunks are fetched into the real session directory and only the files this
    fetched are removed afterwards. A no-op when chunks are already local."""
    if not storage.configured():
        yield
        return
    staged = await asyncio.to_thread(_stage_chunks, project, list(session_ids))
    try:
        yield
    finally:
        await asyncio.to_thread(_unstage_chunks, staged)


async def _run_analysis_job(
    project: projects.Project, session_id: str, chunk_names: list, force: bool = True
) -> None:
    ap.set_job(project, session_id, "running")
    try:
        async with chunks_on_disk(project, [session_id]):
            await ap.run_session(project, session_id, chunk_names, force=force)
    except Exception as exc:
        ap.set_job(project, session_id, "error", error=str(exc))
    finally:
        analysis_tasks.pop(analysis_key(project, session_id), None)


def require_runtime() -> None:
    if not ap.enabled():
        raise ApiError(503, "analysis is not configured on this server")
    if not ap.runtime_present():
        raise ApiError(503, "analysis runtime is unavailable on this server")


async def start_session(project: projects.Project, session_id: str, force: bool = False) -> dict:
    """Queue (or return) per-session analysis — shared by the single-session API and cohorts."""
    chunks = storage.list_chunks(project.storage_prefix, session_id)
    if not chunks:
        raise ApiError(400, "this session has no chunks to analyze")

    if not force:
        cached = ap.session_analysis(project, session_id)
        if cached:
            if "status" not in cached:
                cached = {**cached, "status": "done"}
            return cached

    key = analysis_key(project, session_id)
    existing = analysis_tasks.get(key)
    if existing and not existing.task.done():
        return {"status": "running", "accepted": True}

    job = ap.job(project, session_id)
    if job and job.get("status") == "running" and (existing is None or existing.task.done()):
        # Stale status from a crashed worker — allow a fresh start.
        ap.clear_job(project, session_id)

    if force:
        ap.clear_session_analysis(project, session_id)

    ap.set_job(project, session_id, "running")
    task = asyncio.create_task(
        _run_analysis_job(project, session_id, [c.name for c in chunks], force=True)
    )
    analysis_tasks[key] = _Running(project, task)
    return {"status": "running", "accepted": True}


async def await_session(project: projects.Project, session_id: str, force: bool = False) -> None:
    """Run one session through the shared analysis queue and wait until it settles."""
    result = await start_session(project, session_id, force=force)
    if result.get("status") == "done":
        return
    running = analysis_tasks.get(analysis_key(project, session_id))
    if running is not None:
        await running.task
    job = ap.job(project, session_id)
    if job and job.get("status") == "error":
        raise RuntimeError(job.get("error") or "analysis failed")
    if not ap.session_analysis(project, session_id):
        raise RuntimeError("analysis finished without a result")


def cohort_session_ids(cohort: dict) -> list:
    return [
        s["session_id"]
        for s in (cohort.get("sessions") or [])
        if isinstance(s, dict) and s.get("session_id")
    ]


async def run_cohort(project: projects.Project, cohort_id: str, force: bool = False) -> None:
    """Analyze one cohort in the background. Other cohorts are untouched."""
    key = cohort_key(project, cohort_id)
    try:
        cohort = ap.cohort(project, cohort_id)
        if not cohort:
            return
        async with chunks_on_disk(project, cohort_session_ids(cohort)):
            await ap.run_cohort(project, cohort, force=force)
    except asyncio.CancelledError:
        cohort = ap.cohort(project, cohort_id)
        if cohort:
            batch_name = (cohort.get("batch") or {}).get("name")
            if batch_name:
                await ap.cancel_batch(batch_name)
            ap.mark_paused(project, cohort)
        raise
    finally:
        cohort_tasks.pop(key, None)


async def resume_cohort(project: projects.Project, cohort_id: str) -> None:
    """Re-attach to a Batch job after a process restart."""
    key = cohort_key(project, cohort_id)
    try:
        cohort = ap.cohort(project, cohort_id)
        if not cohort:
            return
        async with chunks_on_disk(project, cohort_session_ids(cohort)):
            await ap.resume_cohort(project, cohort)
    finally:
        cohort_tasks.pop(key, None)
