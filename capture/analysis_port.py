"""The surface capture.server is allowed to use from capture.analyze.

capture.analyze belongs to the analysis refactor and is restructured on its own
schedule. Before this module, server.py reached into 51 of its names — including
`analyze._gates()`, a private — so any rename over there was a breaking change
to the web layer, and neither side could see the interface it was depending on.
Everything the web layer needs is declared here, and this is the only module in
capture/ outside the analyze package that may import it.

It also collapses three identity notions into one. capture.analyze addresses a
tenant three different ways depending on the function: `pool: Path` (a session
directory), `pool_key: str` (a project id), and `data_root: Path`. Every function
here takes a Project and does that conversion in one place, so the day the
analysis package stops being addressed by filesystem path, this is the file that
changes.

Two rules worth keeping if you edit this:

  * Call through `analyze.<name>` inside the function body rather than binding
    the attribute at import. Tests monkeypatch `analyze.ANALYSIS_ENABLED` in 17
    places and `analyze.stream_gemini_chat` in 5, and a bound copy would not see
    the patch.
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
    flip analyze.ANALYSIS_ENABLED, and a copy taken at import would miss it."""
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
    return analyze.read_analysis(_session_dir(project, session_id))


def clear_session_analysis(project: Project, session_id: str) -> None:
    analyze.clear_analysis(_session_dir(project, session_id))


def job(project: Project, session_id: str) -> dict | None:
    return analyze.read_job(_session_dir(project, session_id))


def set_job(project: Project, session_id: str, status: str, error: str | None = None) -> dict:
    return analyze.write_job(_session_dir(project, session_id), status, error=error)


def clear_job(project: Project, session_id: str) -> None:
    analyze.clear_job(_session_dir(project, session_id))


async def run_session(
    project: Project, session_id: str, chunk_names: list, *, force: bool = False
) -> dict:
    return await analyze.analyze_session(
        _session_dir(project, session_id), chunk_names, force=force
    )


def forget_session(project: Project, session_id: str) -> int:
    """Drop a deleted recording from the cohorts that held it."""
    return analyze.forget_session(project.pool, session_id)


# ------------------------------------------------------------------- cohorts


def cohort(project: Project, cohort_id: str) -> dict | None:
    return analyze.read_cohort(project.pool, cohort_id)


def save_cohort(project: Project, cohort: dict) -> dict:
    return analyze.write_cohort(project.pool, cohort)


def cohorts(project: Project) -> list:
    return analyze.list_cohorts(project.pool)


def latest_cohort(project: Project) -> dict | None:
    return analyze.latest_cohort(project.pool)


def import_legacy_cohorts(project: Project) -> None:
    """One-time import of the pre-Postgres cohort files. Bridge code."""
    analyze.migrate_legacy_cohort(project.pool)


def rename_cohort(project: Project, cohort_id: str, label: str) -> dict | None:
    return analyze.rename_cohort(project.pool, cohort_id, label)


def delete_cohort(project: Project, cohort_id: str) -> bool:
    return analyze.delete_cohort(project.pool, cohort_id)


def cohort_view(project: Project, cohort: dict) -> dict:
    """The cohort as the API reports it: stored fields plus derived status and
    progress. Called on nearly every cohort response, and the result is a view —
    persistable_cohort() is what strips the derived fields back off before a write."""
    return analyze.enrich_cohort_status(project.pool, cohort)


def storable_cohort(view: dict, stored: dict | None = None) -> dict:
    return analyze.persistable_cohort(view, stored)


def with_insights(project: Project, cohort: dict) -> dict:
    return analyze.attach_cohort_insights(project.pool, cohort)


def mark_paused(project: Project, cohort: dict) -> dict:
    return analyze.mark_cohort_paused(project.pool, cohort)


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
    return await analyze.run_cohort_job(project.pool, cohort, force=force)


async def resume_cohort(project: Project, cohort: dict) -> dict:
    return await analyze.resume_cohort_batch(project.pool, cohort)


async def cancel_batch(job_name: str) -> None:
    await analyze.cancel_gemini_batch(job_name)


def pending_batch_resumes() -> list:
    """(Project, cohort) pairs with a batch job still in flight, for startup.

    analyze reports these keyed by pool directory; resolving to a Project here
    means an unresolvable pool is skipped with a note rather than failing later
    inside the job, where it used to surface as a 404 from a background task.
    """
    from . import projects as _projects

    out = []
    for pool, cohort in analyze.cohorts_needing_batch_resume(paths.DATA_ROOT):
        found = next((p for p in _projects.all_projects() if p.pool == pool), None)
        if found is None:
            print(f"cohort resume: no project owns {pool}, skipping {cohort.get('id')}")
            continue
        out.append((found, cohort))
    return out


# ----------------------------------------------------------------------- cuts


def cut(project: Project, cohort_id: str, key: str) -> dict | None:
    return analyze.read_cut(project.pool, cohort_id, key)


def save_cut(project: Project, cohort_id: str, key: str, value: dict) -> dict:
    return analyze.write_cut(project.pool, cohort_id, key, value)


def clear_cut(project: Project, cohort_id: str, key: str) -> bool:
    return analyze.clear_cut(project.pool, cohort_id, key)


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
    """
    return {
        "device": analyze.device_class(device),
        "outcome": analyze.outcome_class(order),
        "page": analyze.page_path(url),
    }


# ---------------------------------------------------------------------- paths


def _session_dir(project: Project, session_id: str) -> Path:
    """The directory analyze reads a session's chunks and writes its results in.

    Scaffolding, not architecture: the analyser is file-based, so the web layer
    has to hand it a directory even though chunks live in S3 and the index lives
    in Postgres. server.chunks_on_disk() is what makes the directory real.
    Everything above funnels through here so there is one place to change.
    """
    return project.pool / session_id
