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

  * Call through `analyze.<name>` inside the function body rather than binding
    the attribute at import. Mutable config (`ANALYSIS_ENABLED`, gates) and the
    Gemini/session/cut/cohort edges live on owner submodules and are re-exported
    lazily; a bound copy taken at import would miss both live config and
    definition-site test patches.
  * The constants below are the exception: they are read at import time by
    FastAPI `Query()` defaults, and nothing patches them.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from . import analyze, paths
from .analyze.spec import CohortSpec as _CohortSpec
from .projects import Project

# Read at import by Query() defaults in the cohort routes. Not monkeypatched.
ANALYSIS_FILE = analyze.ANALYSIS_FILE
MODEL = analyze.GEMINI_MODEL

MAX_SESSIONS = analyze.COHORT_MAX_SESSIONS
DEFAULT_PURCHASED_N = analyze.COHORT_DEFAULT_PURCHASED_N
DEFAULT_ALL_N = analyze.COHORT_DEFAULT_ALL_N
MIN_DURATION_MS = analyze.COHORT_MIN_DURATION_MS
URL_CONTAINS = analyze.COHORT_URL_CONTAINS
ORDER_LATEST = analyze.COHORT_ORDER_LATEST
ORDERS = analyze.COHORT_ORDERS

# The selection rule as a value the web layer can build and hand back. Exported
# here rather than imported from the package directly, so the port stays the one
# door in: test_analysis_port enforces that.
CohortSpec = _CohortSpec

OUTCOME_VALUES = analyze.OUTCOME_VALUES
DEVICE_VALUES = analyze.DEVICE_VALUES


# ---------------------------------------------------------------- availability


def enabled() -> bool:
    """Whether a model is configured at all. A function, not a constant: tests
    flip analyze.runtime.ANALYSIS_ENABLED (reachable as analyze.ANALYSIS_ENABLED),
    and a copy taken at import would miss it."""
    return bool(analyze.ANALYSIS_ENABLED)


def runtime_present() -> bool:
    """Whether the extractor's Node/Chromium runtime is installed."""
    return analyze.runtime_present()


def model_gate() -> int:
    """How many model calls may be in flight. Was analyze._gates()[1] — reaching
    across the boundary for a private, which is why this method exists."""
    _, gate = analyze._gates()
    return gate


# ------------------------------------------------------------- per-session work


def session_analysis(project: Project, session_id: str) -> dict | None:
    return analyze.read_analysis(project.id, session_id)


def clear_session_analysis(project: Project, session_id: str) -> None:
    analyze.clear_analysis(
        project.id, session_id, session_dir=_session_dir(project, session_id)
    )


def job(project: Project, session_id: str) -> dict | None:
    return analyze.read_job(project.id, session_id)


def set_job(project: Project, session_id: str, status: str, error: str | None = None) -> dict:
    return analyze.write_job(project.id, session_id, status, error=error)


def clear_job(project: Project, session_id: str) -> None:
    analyze.clear_job(project.id, session_id)


async def run_session(
    project: Project, session_id: str, chunk_names: list, *, force: bool = False
) -> dict:
    return await analyze.analyze_session(
        project.id, _session_dir(project, session_id), chunk_names, force=force
    )


def forget_session(project: Project, session_id: str) -> int:
    """Drop a deleted recording from the cohorts that held it."""
    return analyze.forget_session(project.id, session_id)


# ------------------------------------------------------------------- cohorts


def cohort(project: Project, cohort_id: str) -> dict | None:
    return analyze.read_cohort(project.id, cohort_id)


def save_cohort(project: Project, cohort: dict) -> dict:
    return analyze.write_cohort(project.id, cohort)


def cohorts(project: Project) -> list:
    return analyze.list_cohorts(project.id)


def latest_cohort(project: Project) -> dict | None:
    return analyze.latest_cohort(project.id)


def import_legacy_cohorts(project: Project) -> None:
    """One-time import of the pre-Postgres cohort files. Bridge code.

    Moves cohort.json → cohorts/<id>.json, then imports that directory into
    Postgres and deletes the files. Safe to call on every list/read: existing
    stored cohorts win.
    """
    from .analyze import store as analyze_store

    analyze.migrate_legacy_cohort(project.pool)
    analyze_store.migrate_json_cohorts(
        analyze_store.connect(),
        project.id,
        analyze.cohorts_dir(project.pool),
        analyze.CUTS_SUFFIX,
    )


def rename_cohort(project: Project, cohort_id: str, label: str) -> dict | None:
    return analyze.rename_cohort(project.id, cohort_id, label)


def delete_cohort(project: Project, cohort_id: str) -> bool:
    return analyze.delete_cohort(project.id, cohort_id)


def cohort_view(project: Project, cohort: dict) -> dict:
    """The cohort as the API reports it: stored fields plus derived status and
    progress. Called on nearly every cohort response, and the result is a view —
    persistable_cohort() is what strips the derived fields back off before a write."""
    return analyze.enrich_cohort_status(project.id, cohort)


def storable_cohort(view: dict, stored: dict | None = None) -> dict:
    return analyze.persistable_cohort(view, stored)


def with_insights(project: Project, cohort: dict) -> dict:
    return analyze.attach_cohort_insights(project.id, cohort)


def mark_paused(project: Project, cohort: dict) -> dict:
    return analyze.mark_cohort_paused(project.id, cohort)


def build_cohort(conn, project: Project, selection: CohortSpec, *, label=None) -> dict:
    """Sample a cohort out of the session index. Takes the caller's connection
    because the sampling runs inside the request's transaction."""
    return analyze.build_cohort(conn, project.id, selection, label=label)


def pool_facets(conn, project: Project, *, min_duration_ms: int = 0) -> dict:
    return analyze.pool_facets(conn, project.id, min_duration_ms=min_duration_ms)


def facets_from_rows(rows: list) -> dict:
    return analyze.facets_from_rows(rows)


def clean_label(label: str | None) -> str:
    return analyze.clean_cohort_label(label)


# ------------------------------------------------------------------ batch jobs


async def run_cohort(project: Project, cohort: dict, *, force: bool = False) -> dict:
    return await analyze.run_cohort_job(
        project.id, cohort, sessions_root=project.pool, force=force
    )


async def resume_cohort(project: Project, cohort: dict) -> dict:
    return await analyze.resume_cohort_batch(
        project.id, cohort, sessions_root=project.pool
    )


async def cancel_batch(job_name: str) -> None:
    await analyze.cancel_gemini_batch(job_name)


def pending_batch_resumes() -> list:
    """(Project, cohort) pairs with a batch job still in flight, for startup.

    analyze reports these keyed by project_id; resolving to a Project here
    means an unresolvable id is skipped with a note rather than failing later
    inside the job, where it used to surface as a 404 from a background task.
    """
    from . import projects as _projects

    out = []
    for project_id, cohort in analyze.cohorts_needing_batch_resume(paths.DATA_ROOT):
        found = _projects.by_id(project_id)
        if found is None:
            print(f"cohort resume: no project {project_id}, skipping {cohort.get('id')}")
            continue
        out.append((found, cohort))
    return out


# ----------------------------------------------------------------------- cuts


def cut(project: Project, cohort_id: str, key: str) -> dict | None:
    return analyze.read_cut(project.id, cohort_id, key)


def save_cut(project: Project, cohort_id: str, key: str, value: dict) -> dict:
    return analyze.write_cut(project.id, cohort_id, key, value)


def clear_cut(project: Project, cohort_id: str, key: str) -> bool:
    return analyze.clear_cut(project.id, cohort_id, key)


def cut_key(slice_spec: dict | None) -> str:
    return analyze.cut_key(slice_spec)


async def build_cut(rows: list, slice_spec: dict | None) -> dict:
    return await analyze.build_cut_analysis(rows, slice_spec)


# ----------------------------------------------------------------------- chat


def clean_chat_messages(messages) -> list:
    return analyze.clean_chat_messages(messages)


async def chat_context(rows: list, slice_spec: dict | None) -> tuple:
    return await analyze.chat_context(rows, slice_spec)


def stream_chat(
    rows: list, label: str, given: str, messages: list, spec: dict | None = None
) -> AsyncIterator[str]:
    """Not async itself: returns analyze's async generator so the caller can
    `async for` over it. Looked up at call time — tests replace it."""
    return analyze.stream_gemini_chat(rows, label, given, messages, spec)


# ----------------------------------------------------------- row classifiers


def classify(device: dict | None, order: dict | None, url: str | None) -> dict:
    """The three index columns a cut slices on, named the way analyze names them.

    One call rather than three exported classifiers: the only caller wants all
    three together, and the shape of a cut row belongs on this side of the seam.
    Ingest writes the same labels via `capture.classify` onto sessions columns.
    """
    from . import classify as session_classify

    return {
        "device": session_classify.device_class(device),
        "outcome": session_classify.outcome_class(order),
        "page": session_classify.page_path(url),
    }


# ---------------------------------------------------------------------- paths


def _session_dir(project: Project, session_id: str) -> Path:
    """The directory holding a session's chunk recordings and extract scratch.

    Chunks live on disk (or are staged there); write-ups live in Postgres keyed
    by project.id + session_id. server.chunks_on_disk() is what makes the
    directory real. Everything above funnels through here so there is one place
    to change.
    """
    return project.pool / session_id
