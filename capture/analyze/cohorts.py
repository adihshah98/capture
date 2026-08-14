"""Cohort lifecycle and the Batch/sync analysis job.

Owns sampling into a stored cohort (`build_cohort` / rename / delete), pause and
Batch resume, and `run_cohort_job` (extract → Batch submit/collect, with sync
Gemini fallback). Orchestrators call through this module so tests can
`mock.patch("capture.analyze.cohorts._cohort_sync_model", ...)` at the
definition site.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

from . import gemini, runtime, session, spec
from . import store as analyze_store
from .cohort_state import enrich_cohort_status
from .labels import _auto_cohort_label, clean_cohort_label, cohort_selection_text
from .sampling import sample_cohort
from .slices import normalize_slice
from .storage import (
    ANALYSIS_FILE,
    clear_analysis,
    clear_job,
    cohorts_dir,
    mark_cohort_session,
    persist_cohort_refresh,
    read_analyses,
    read_cohort,
    read_jobs,
    safe_cohort_id,
    set_cohort_label,
    set_cohort_status,
    write_job,
)


def build_cohort(
    conn: spec.Connection,
    project_id: str,
    selection: spec.CohortSpec,
    *,
    label: str | None = None,
) -> dict:
    """Sample a cohort and record how it was sampled.

    The returned dict is the cohort as it goes to storage: its membership, plus
    every field of the selection rule, because a cohort is immutable once
    sampled and the rule is the only record of what it represents.

    `project_id` is the sampling identity (project UUID); do not confuse with
    the old filesystem pool handle.
    """
    now = int(time.time() * 1000)
    selection = replace(
        selection.normalised(),
        slice_spec=normalize_slice(selection.slice_spec),
        slice_b=normalize_slice(selection.slice_b),
    )
    result = sample_cohort(conn, project_id, selection)
    stamp = time.strftime("%b %-d, %-I:%M %p", time.localtime(now / 1000))
    chosen = clean_cohort_label(label) or _auto_cohort_label(selection, stamp=stamp)
    return {
        "id": f"cohort-{now}",
        # Chosen name, or auto label when none was provided.
        "label": chosen,
        "selection": cohort_selection_text(selection),
        "created_at": now,
        "updated_at": now,
        # Sampled only — analysis starts via POST .../analyze.
        "status": "idle",
        "a_n": selection.a_n,
        "b_n": selection.b_n,
        "min_duration_ms": selection.min_duration_ms,
        "url_contains": (selection.url_contains or "").strip(),
        "start_ms": selection.start_ms,
        "end_ms": selection.end_ms,
        "order": selection.order,
        "slice": selection.slice_spec,
        "slice_b": selection.slice_b if selection.b_n > 0 else {},
        # How the request resolved against the candidate window. `matched` can
        # be below `requested` when the pool is thin or the scan hit
        # CANDIDATE_CAP; see sampling.SampleStats.
        "sample": result.sample.as_dict(),
        "sessions": result.sessions,
    }


def rename_cohort(project_id: str, cohort_id: str, label: str) -> dict | None:
    """Set a cohort's display label. Membership is untouched."""
    cohort = read_cohort(project_id, cohort_id)
    if not cohort:
        return None
    clean = clean_cohort_label(label)
    if not clean:
        raise ValueError("label cannot be empty")
    if not set_cohort_label(project_id, cohort_id, clean):
        return None
    return {**cohort, "label": clean}


def delete_cohort(project_id: str, cohort_id: str) -> bool:
    """Remove a cohort and its cached cuts. Session write-ups stay."""
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return False
    return analyze_store.delete_cohort(analyze_store.connect(), project_id, cohort_id)


def _session_snapshot(cohort: dict) -> list:
    """Comparable session rows taken before in-place mutation."""
    return [dict(e) for e in (cohort.get("sessions") or [])]


def _persist_session_edits(project_id: str, cohort: dict, before: list) -> None:
    persist_cohort_refresh(project_id, {**cohort, "sessions": before}, cohort)


def _persist_finished(project_id: str, live: dict) -> dict:
    """Write enrich's durable status and drop the batch name. Membership stays."""
    live.pop("batch", None)
    final = enrich_cohort_status(project_id, live)
    set_cohort_status(project_id, final, final.get("status") or "idle", clear_batch=True)
    persist_cohort_refresh(project_id, live, final)
    return final


def mark_cohort_paused(project_id: str, cohort: dict) -> dict:
    """Persist paused status and return in-flight session rows to queued."""
    before = _session_snapshot(cohort)
    ids = [e["session_id"] for e in before if e.get("session_id")]
    analyses = read_analyses(project_id, ids)
    jobs = read_jobs(project_id, ids)
    for entry in cohort.get("sessions") or []:
        sid = entry["session_id"]
        if analyses.get(sid):
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            continue
        if (
            entry.get("status") in ("running", "queued")
            or (jobs.get(sid) or {}).get("status") == "running"
        ):
            entry["status"] = "queued"
            entry.pop("error", None)
            clear_job(project_id, sid)
    set_cohort_status(project_id, cohort, "paused", clear_batch=True)
    _persist_session_edits(project_id, cohort, before)
    return enrich_cohort_status(project_id, cohort)


async def resume_cohort_batch(project_id: str, cohort: dict, *, sessions_root: Path) -> dict:
    """Finish a cohort that already has a Gemini Batch job name persisted."""
    batch = cohort.get("batch") or {}
    job_name = batch.get("name")
    if not job_name:
        return enrich_cohort_status(project_id, cohort)

    prepared = []
    wanted = set(batch.get("session_ids") or [])
    before = _session_snapshot(cohort)
    analyses = read_analyses(project_id, [e.get("session_id") for e in before])
    for entry in cohort.get("sessions") or []:
        sid = entry.get("session_id")
        if wanted and sid not in wanted:
            continue
        session_path = sessions_root / sid
        if analyses.get(sid):
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            continue
        digest = session.load_session_digest(session_path) or {"frames": [], "warnings": []}
        entry["status"] = "running"
        write_job(project_id, sid, "running")
        prepared.append((entry, digest))
    _persist_session_edits(project_id, cohort, before)

    if prepared:
        # Same rule as the first attempt: a batch we could not collect keeps
        # its name and stays resumable. Letting this raise would strand the
        # cohort as "running" with nothing watching it, since the startup
        # resume task is never awaited.
        if not await _collect_cohort_batch(
            project_id, cohort, prepared, job_name, sessions_root=sessions_root
        ):
            return enrich_cohort_status(project_id, read_cohort(project_id, cohort["id"]) or cohort)
    live = read_cohort(project_id, cohort["id"]) or cohort
    return _persist_finished(project_id, live)


async def run_cohort_job(
    project_id: str, cohort: dict, *, sessions_root: Path, force: bool = False
) -> dict:
    """Extract cohort sessions, then analyze via Batch (or sync fallback).

    Mutates and persists the cohort as work progresses. Returns the enriched
    final cohort dict. Never deletes or overwrites other cohorts.

    `project_id` addresses Postgres; `sessions_root` is where chunk files and
    `.analysis/` scratch live (`sessions_root / session_id`).
    """
    try:
        if (cohort.get("batch") or {}).get("name") and not force:
            return await resume_cohort_batch(project_id, cohort, sessions_root=sessions_root)

        set_cohort_status(project_id, cohort, "running", clear_batch=True)

        before = _session_snapshot(cohort)
        pending_entries = []
        analyses = {} if force else read_analyses(project_id, [e.get("session_id") for e in before])
        for entry in cohort.get("sessions") or []:
            sid = entry["session_id"]
            if not force and analyses.get(sid):
                entry["status"] = "done"
                entry["analysis"] = ANALYSIS_FILE
                entry.pop("error", None)
                continue
            entry["status"] = "queued"
            entry.pop("error", None)
            pending_entries.append(entry)
        _persist_session_edits(project_id, cohort, before)

        if not pending_entries:
            return enrich_cohort_status(project_id, read_cohort(project_id, cohort["id"]) or cohort)

        prepared = []  # (entry, digest)

        async def extract_one(entry: dict) -> None:
            sid = entry["session_id"]
            session_path = sessions_root / sid
            write_job(project_id, sid, "running")
            # One row, not the whole document: these coroutines run
            # concurrently and each owns exactly one session.
            mark_cohort_session(project_id, cohort, entry, "running")
            try:
                # Reuse a prior reconstruction unless the caller asked to redo.
                # Without this, a retry after a Batch OOM would wipe digests and
                # spend another hour in Playwright before talking to Gemini.
                if not force:
                    digest = session.load_session_digest(session_path)
                    if digest and gemini._usable_frames(digest.get("frames") or []):
                        prepared.append((entry, digest))
                        return
                chunks = session.session_chunk_names(session_path)
                if not chunks:
                    raise RuntimeError("no chunks to analyze")
                if force:
                    clear_analysis(project_id, sid, session_dir=session_path)
                digest = await session.extract_session_digest(session_path, chunks)
                prepared.append((entry, digest))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc) or "analysis failed"
                write_job(project_id, sid, "error", error=message)
                mark_cohort_session(project_id, cohort, entry, "error", error=message)

        await asyncio.gather(*(extract_one(e) for e in pending_entries), return_exceptions=True)

        if not prepared:
            return enrich_cohort_status(project_id, read_cohort(project_id, cohort["id"]) or cohort)

        use_batch = runtime.ANALYZE_BATCH and runtime.ANALYSIS_ENABLED
        job_name = None
        if use_batch:
            # Only submission falls back to sync. Once a job exists it is
            # billable, so a later failure must keep its name and be resumed
            # rather than re-running every session at interactive prices.
            try:
                job_name = await gemini.submit_prepared_gemini_batch(
                    prepared,
                    display_name=f"capture-{cohort['id']}",
                    work_dir=cohorts_dir(sessions_root),
                    project_id=project_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No job was created, so nothing has been paid for: falling
                # back keeps a Batch outage from stranding an extracted cohort.
                print(f"cohort batch submit failed ({exc}); falling back to sync Gemini")
                set_cohort_status(
                    project_id,
                    cohort,
                    cohort.get("status") or "running",
                    clear_batch=True,
                )
                await _cohort_sync_model(project_id, cohort, prepared, sessions_root=sessions_root)

        if job_name:
            batch = {
                "name": job_name,
                "submitted_at": int(time.time() * 1000),
                "session_ids": [e["session_id"] for e, _ in prepared],
            }
            set_cohort_status(project_id, cohort, cohort.get("status") or "running", batch=batch)
            if not await _collect_cohort_batch(
                project_id, cohort, prepared, job_name, sessions_root=sessions_root
            ):
                # Still in flight and no longer being watched. The name stays
                # on disk so startup resume — or a Start from the UI — picks
                # the same job back up.
                return enrich_cohort_status(
                    project_id, read_cohort(project_id, cohort["id"]) or cohort
                )
        elif not use_batch:
            await _cohort_sync_model(project_id, cohort, prepared, sessions_root=sessions_root)

        live = read_cohort(project_id, cohort["id"]) or cohort
        return _persist_finished(project_id, live)
    except asyncio.CancelledError:
        live = read_cohort(project_id, cohort["id"]) or cohort
        batch_name = (live.get("batch") or {}).get("name")
        if batch_name:
            await gemini.cancel_gemini_batch(batch_name)
        mark_cohort_paused(project_id, live)
        raise


async def _collect_cohort_batch(
    project_id: str,
    cohort: dict,
    prepared: list,
    job_name: str,
    *,
    sessions_root: Path,
) -> bool:
    """Apply a submitted batch. False when it is still in flight.

    Everything past submission is recoverable: the job exists and has been paid
    for, so a batch we simply stopped watching must leave its name on disk for
    a resume. Only a batch that genuinely failed marks its sessions errored.
    """
    try:
        await apply_cohort_batch(
            project_id, cohort, prepared, job_name=job_name, sessions_root=sessions_root
        )
        return True
    except asyncio.CancelledError:
        raise
    except gemini.BatchStillRunning as exc:
        print(f"cohort batch {job_name} not collected ({exc}); leaving it to resume")
        _mark_prepared_still_running(project_id, cohort, prepared)
        return False
    except Exception as exc:
        # Reading or writing the results failed, not the job. The name stays so
        # a resume can retry the collection instead of re-billing the cohort.
        message = str(exc) or "could not apply batch results"
        print(f"cohort batch {job_name} apply failed ({message}); leaving it to resume")
        _mark_prepared_still_running(project_id, cohort, prepared)
        return False


def _mark_prepared_still_running(project_id: str, cohort: dict, prepared: list) -> None:
    """Return the batch's sessions to `running` so a resume picks them up.

    Row at a time rather than one `write_cohort`: apply_cohort_batch may
    already have marked some of these done before it failed, and pushing the
    whole in-memory dict back would overwrite those completions with this
    copy's stale view of them.
    """
    for entry, _ in prepared:
        if entry.get("status") == "done":
            continue
        mark_cohort_session(project_id, cohort, entry, "running")


async def apply_cohort_batch(
    project_id: str,
    cohort: dict,
    prepared: list,
    *,
    job_name: str | None = None,
    sessions_root: Path,
) -> None:
    """Wait for a batch job and write per-session analysis results."""
    name = job_name or (cohort.get("batch") or {}).get("name")
    if not name:
        raise RuntimeError("cohort has no batch job to apply")

    by_id = {entry["session_id"]: (entry, digest) for entry, digest in prepared}
    job = await gemini.wait_gemini_batch(name)
    state = gemini._batch_state_name(job)
    if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
        err = getattr(job, "error", None)
        message = getattr(err, "message", None) if err else None
        message = message or f"Gemini batch {state}"
        for entry, _ in prepared:
            write_job(project_id, entry["session_id"], "error", error=message)
            mark_cohort_session(project_id, cohort, entry, "error", error=message)
        return

    results = gemini.parse_batch_results(job)
    for sid, (entry, digest) in by_id.items():
        hit = results.get(sid)
        if not hit:
            message = "missing from batch results"
            write_job(project_id, sid, "error", error=message)
            mark_cohort_session(project_id, cohort, entry, "error", error=message)
        elif hit.get("error"):
            write_job(project_id, sid, "error", error=hit["error"])
            mark_cohort_session(project_id, cohort, entry, "error", error=hit["error"])
        else:
            session.write_analysis_result(
                project_id,
                sid,
                hit["text"],
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            mark_cohort_session(project_id, cohort, entry, "done", analysis=ANALYSIS_FILE)


async def _cohort_sync_model(
    project_id: str, cohort: dict, prepared: list, *, sessions_root: Path
) -> None:
    """Interactive Gemini fallback used when Batch is off or unavailable."""

    async def one(entry: dict, digest: dict) -> None:
        sid = entry["session_id"]
        write_job(project_id, sid, "running")
        mark_cohort_session(project_id, cohort, entry, "running")
        try:
            _, model_gate = runtime._gates()
            async with model_gate:
                text = await gemini.call_gemini(
                    digest,
                    digest.get("frames") or [],
                    gemini.session_metadata(project_id, sid),
                )
            session.write_analysis_result(
                project_id,
                sid,
                text,
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            mark_cohort_session(project_id, cohort, entry, "done", analysis=ANALYSIS_FILE)
        except Exception as exc:
            message = str(exc) or "analysis failed"
            write_job(project_id, sid, "error", error=message)
            mark_cohort_session(project_id, cohort, entry, "error", error=message)

    await asyncio.gather(
        *(one(entry, digest) for entry, digest in prepared),
        return_exceptions=True,
    )


def cohorts_needing_batch_resume() -> list:
    """(project_id, cohort) pairs with an in-flight Batch job name.

    An index lookup over every project. Identity is the project UUID;
    sessions_root is `project.pool` when a caller needs the filesystem.
    """
    return analyze_store.resumable_cohorts(analyze_store.connect())
