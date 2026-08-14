"""The surface capture.server is allowed to use from capture.analyze.

capture.analyze belongs to the analysis refactor and is restructured on its own
schedule. Before this module, server.py reached into 51 of its names — including
`analyze._gates()`, a private — so any rename over there was a breaking change
to the web layer, and neither side could see the interface it was depending on.
Everything the web layer needs is declared here, and this is the only module in
capture/ outside the analyze package that may import it.

Project is the identity for every Postgres-backed call (`project.id`). Path is
only handed through where recording files or `.analysis/` scratch are required
(`project.pool` as sessions_root, or `project.pool / session_id`).

Two rules worth keeping if you edit this:

  * Call through the owner module inside the function body rather than binding
    the attribute at import. Mutable config (`ANALYSIS_ENABLED`, gates) and the
    Gemini/session/cut/cohort edges live on those modules; a bound copy taken at
    import would miss both live config and definition-site test patches.
  * The constants below are the exception: they are read at import time by
    FastAPI `Query()` defaults, and nothing patches them.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from . import paths
from .analyze import (
    cohort_state,
    cuts,
    gemini,
    labels,
    runtime,
    sampling,
    session,
    slices,
    spec,
    storage,
)
from .analyze import (
    cohorts as cohort_jobs,
)
from .analyze.spec import CohortSpec
from .projects import Project

# Read at import by Query() defaults in the cohort routes. Not monkeypatched.
ANALYSIS_FILE = storage.ANALYSIS_FILE
MODEL = runtime.GEMINI_MODEL

MAX_SESSIONS = spec.MAX_SESSIONS
DEFAULT_PURCHASED_N = spec.DEFAULT_PURCHASED_N
DEFAULT_ALL_N = spec.DEFAULT_ALL_N
MIN_DURATION_MS = spec.MIN_DURATION_MS
URL_CONTAINS = spec.URL_CONTAINS
ORDER_LATEST = spec.ORDER_LATEST
ORDERS = spec.ORDERS

# The selection rule as a value the web layer can build and hand back. Exported
# here rather than imported from the package directly, so the port stays the one
# door in: test_analysis_port enforces that.

OUTCOME_VALUES = slices.OUTCOME_VALUES
DEVICE_VALUES = slices.DEVICE_VALUES


# ---------------------------------------------------------------- availability


def enabled() -> bool:
    """Whether a model is configured at all. A function, not a constant: tests
    flip runtime.ANALYSIS_ENABLED, and a copy taken at import would miss it."""
    return bool(runtime.ANALYSIS_ENABLED)


def runtime_present() -> bool:
    """Whether the extractor's Node/Chromium runtime is installed."""
    return runtime.runtime_present()


def model_gate() -> int:
    """How many model calls may be in flight. Was analyze._gates()[1] — reaching
    across the boundary for a private, which is why this method exists."""
    _, gate = runtime._gates()
    return gate


# ------------------------------------------------------------- per-session work


def session_analysis(project: Project, session_id: str) -> dict | None:
    return storage.read_analysis(project.id, session_id)


def session_analyses(project: Project, session_ids) -> dict:
    """session_id → write-up for the ids that have one. One query, not N."""
    return storage.read_analyses(project.id, session_ids)


def clear_session_analysis(project: Project, session_id: str) -> None:
    storage.clear_analysis(project.id, session_id, session_dir=_session_dir(project, session_id))


def job(project: Project, session_id: str) -> dict | None:
    return storage.read_job(project.id, session_id)


def set_job(project: Project, session_id: str, status: str, error: str | None = None) -> dict:
    return storage.write_job(project.id, session_id, status, error=error)


def clear_job(project: Project, session_id: str) -> None:
    storage.clear_job(project.id, session_id)


async def run_session(
    project: Project, session_id: str, chunk_names: list, *, force: bool = False
) -> dict:
    return await session.analyze_session(
        project.id, _session_dir(project, session_id), chunk_names, force=force
    )


def forget_session(project: Project, session_id: str) -> int:
    """Drop a deleted recording from the cohorts that held it."""
    return storage.forget_session(project.id, session_id)


# ------------------------------------------------------------------- cohorts


def cohort(project: Project, cohort_id: str) -> dict | None:
    return storage.read_cohort(project.id, cohort_id)


def save_cohort(project: Project, cohort: dict) -> dict:
    """Create a cohort. Status and batch updates go through set_cohort_status."""
    return storage.write_cohort(project.id, cohort)


def set_cohort_status(
    project: Project,
    cohort: dict,
    status: str,
    *,
    batch: dict | None = None,
    clear_batch: bool = False,
) -> None:
    """Move a cohort's own status without replacing membership."""
    storage.set_cohort_status(project.id, cohort, status, batch=batch, clear_batch=clear_batch)


def mark_cohort_session(
    project: Project,
    cohort: dict,
    entry: dict,
    status: str,
    *,
    analysis: str | None = None,
    error: str | None = None,
) -> None:
    storage.mark_cohort_session(project.id, cohort, entry, status, analysis=analysis, error=error)


def persist_cohort_refresh(project: Project, stored: dict, live: dict) -> bool:
    """Write enrich's durable status without replacing membership."""
    return storage.persist_cohort_refresh(project.id, stored, live)


def cohorts(project: Project) -> list:
    return storage.list_cohorts(project.id)


def latest_cohort(project: Project) -> dict | None:
    return storage.latest_cohort(project.id)


def rename_cohort(project: Project, cohort_id: str, label: str) -> dict | None:
    return cohort_jobs.rename_cohort(project.id, cohort_id, label)


def delete_cohort(project: Project, cohort_id: str) -> bool:
    return cohort_jobs.delete_cohort(project.id, cohort_id)


def cohort_view(project: Project, cohort: dict) -> dict:
    """The cohort as the API reports it: stored fields plus derived status and
    progress. Durable bits of this view go back through persist_cohort_refresh
    (and set_cohort_status for the batch handle), not write_cohort."""
    return cohort_state.enrich_cohort_status(project.id, cohort)


def storable_cohort(view: dict, stored: dict | None = None) -> dict:
    return cohort_state.persistable_cohort(view, stored)


def with_insights(project: Project, cohort: dict) -> dict:
    return cohort_state.attach_cohort_insights(project.id, cohort)


def mark_paused(project: Project, cohort: dict) -> dict:
    return cohort_jobs.mark_cohort_paused(project.id, cohort)


def build_cohort(conn, project: Project, selection: CohortSpec, *, label=None) -> dict:
    """Sample a cohort out of the session index. Takes the caller's connection
    because the sampling runs inside the request's transaction."""
    return cohort_jobs.build_cohort(conn, project.id, selection, label=label)


def pool_facets(conn, project: Project, *, min_duration_ms: int = 0) -> dict:
    return sampling.pool_facets(conn, project.id, min_duration_ms=min_duration_ms)


def facets_from_rows(rows: list) -> dict:
    return sampling.facets_from_rows(rows)


def classified_rows(conn, project: Project, session_ids) -> dict:
    """Materialised class columns for these sessions, named the way a cut slices.

    Sampling already filtered on the same columns. The caller passes the cohort's
    ids so this is a point lookup, not a project scan, and does not re-derive
    labels from raw JSON.
    """
    return sampling.classified_rows(conn, project.id, session_ids)


def clean_label(label: str | None) -> str:
    return labels.clean_cohort_label(label)


# ------------------------------------------------------------------ batch jobs


async def run_cohort(project: Project, cohort: dict, *, force: bool = False) -> dict:
    return await cohort_jobs.run_cohort_job(
        project.id, cohort, sessions_root=project.pool, force=force
    )


async def resume_cohort(project: Project, cohort: dict) -> dict:
    return await cohort_jobs.resume_cohort_batch(project.id, cohort, sessions_root=project.pool)


async def cancel_batch(job_name: str) -> None:
    await gemini.cancel_gemini_batch(job_name)


def pending_batch_resumes() -> list:
    """(Project, cohort) pairs with a batch job still in flight, for startup.

    analyze reports these keyed by project_id; resolving to a Project here
    means an unresolvable id is skipped with a note rather than failing later
    inside the job, where it used to surface as a 404 from a background task.
    """
    from . import projects as _projects

    out = []
    for project_id, cohort in cohort_jobs.cohorts_needing_batch_resume(paths.DATA_ROOT):
        found = _projects.by_id(project_id)
        if found is None:
            print(f"cohort resume: no project {project_id}, skipping {cohort.get('id')}")
            continue
        out.append((found, cohort))
    return out


# ----------------------------------------------------------------------- cuts


def cut(project: Project, cohort_id: str, key: str) -> dict | None:
    return storage.read_cut(project.id, cohort_id, key)


def save_cut(project: Project, cohort_id: str, key: str, value: dict) -> dict:
    return storage.write_cut(project.id, cohort_id, key, value)


def clear_cut(project: Project, cohort_id: str, key: str) -> bool:
    return storage.clear_cut(project.id, cohort_id, key)


def cut_key(slice_spec: dict | None) -> str:
    return cuts.cut_key(slice_spec)


async def build_cut(rows: list, slice_spec: dict | None) -> dict:
    return await cuts.build_cut_analysis(rows, slice_spec)


# ----------------------------------------------------------------------- chat


def clean_chat_messages(messages) -> list:
    return cuts.clean_chat_messages(messages)


async def chat_context(rows: list, slice_spec: dict | None) -> tuple:
    return await cuts.chat_context(rows, slice_spec)


def stream_chat(
    rows: list, label: str, given: str, messages: list, spec: dict | None = None
) -> AsyncIterator[str]:
    """Not async itself: returns analyze's async generator so the caller can
    `async for` over it. Looked up at call time — tests replace it."""
    return cuts.stream_gemini_chat(rows, label, given, messages, spec)


# ---------------------------------------------------------------------- paths


def _session_dir(project: Project, session_id: str) -> Path:
    """The directory holding a session's chunk recordings and extract scratch.

    Chunks live on disk (or are staged there); write-ups live in Postgres keyed
    by project.id + session_id. server.chunks_on_disk() is what makes the
    directory real. Everything above funnels through here so there is one place
    to change.
    """
    return project.pool / session_id
