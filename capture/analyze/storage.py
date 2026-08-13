"""Where analysis results and cohorts live, addressed by project_id.

Callers identify a tenant by its Postgres project UUID. Session write-ups and
cohort state live in Postgres; Path appears only for recording files (chunks)
and regenerable scratch under `.analysis/`.

It is deliberately leaf-ward. Nothing here reads a name the tests patch -- no
prompts, no model config, no `_gates` -- and nothing calls back into
orchestration. That is what makes it separable at all; see
`tests/server/test_patch_contract.py`.
"""

import re
import shutil
import time
from pathlib import Path

from .. import projects
from . import store as analyze_store

ANALYSIS_FILE = "analysis.json"
ANALYSIS_DIR = ".analysis"
# Scratch beside the sessions pool (Batch JSONL). Not live cohort state.
COHORTS_DIR = "cohorts"
# Cohort ids are ours to mint ("cohort-<ms>"), but they land in filenames, so
# validate rather than trust anything arriving from a URL.
SAFE_COHORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def analysis_paths(session_dir: Path) -> tuple:
    return session_dir / ANALYSIS_FILE, session_dir / ANALYSIS_DIR


def sessions_root(project_id: str) -> Path:
    """Filesystem root for this project's session recordings."""
    project = projects.by_id(project_id)
    if project is None:
        raise LookupError(f"no project {project_id!r}")
    return project.pool


def session_dir(project_id: str, session_id: str) -> Path:
    return sessions_root(project_id) / session_id


def read_analysis(project_id: str, session_id: str) -> dict | None:
    return analyze_store.read_analysis(analyze_store.connect(), project_id, session_id)


def write_analysis(project_id: str, session_id: str, result: dict) -> dict:
    return analyze_store.write_analysis(analyze_store.connect(), project_id, session_id, result)


def forget_analyses(project_id: str) -> int:
    """Drop every write-up in a project. For a whole-project reset."""
    return analyze_store.forget_analyses(analyze_store.connect(), project_id)


def read_job(project_id: str, session_id: str) -> dict | None:
    """In-progress or failed async analysis job, if any."""
    return analyze_store.read_job(analyze_store.connect(), project_id, session_id)


def write_job(project_id: str, session_id: str, status: str, error: str | None = None) -> dict:
    job = {"status": status, "updated_at": int(time.time() * 1000)}
    if error:
        job["error"] = error
    return analyze_store.write_job(analyze_store.connect(), project_id, session_id, job)


def clear_job(project_id: str, session_id: str) -> None:
    analyze_store.clear_job(analyze_store.connect(), project_id, session_id)


def clear_analysis(project_id: str, session_id: str, *, session_dir: Path | None = None) -> None:
    """Drop a finished result so a re-analyze starts clean.

    Also clears local `.analysis` scratch (and best-effort legacy sidecars) when
    the session directory exists on disk.
    """
    analyze_store.clear_analysis(analyze_store.connect(), project_id, session_id)
    root = session_dir
    if root is None:
        try:
            root = sessions_root(project_id) / session_id
        except LookupError:
            return
    if not root.is_dir():
        return
    for legacy in (ANALYSIS_FILE, "analysis.insights.json", "analysis.qa.json"):
        (root / legacy).unlink(missing_ok=True)
    shutil.rmtree(root / ANALYSIS_DIR, ignore_errors=True)


def cohorts_dir(pool: Path) -> Path:
    """Scratch beside the sessions directory. Batch JSONL lands here."""
    return pool.parent / COHORTS_DIR


def safe_cohort_id(cohort_id: str) -> str:
    """Cohort ids reach the filesystem, so keep them to a known-safe alphabet."""
    raw = str(cohort_id or "").strip()
    if not raw or not SAFE_COHORT_ID.match(raw):
        raise ValueError("bad cohort id")
    return raw


def read_cohort(project_id: str, cohort_id: str) -> dict | None:
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return None
    return analyze_store.read_cohort(analyze_store.connect(), project_id, cohort_id)


def write_cohort(project_id: str, cohort: dict) -> dict:
    return analyze_store.write_cohort(
        analyze_store.connect(), project_id, {**cohort, "id": safe_cohort_id(cohort["id"])}
    )


def list_cohorts(project_id: str) -> list:
    """Every cohort in this project, newest first. Membership only — no write-ups."""
    return analyze_store.list_cohorts(analyze_store.connect(), project_id)


def latest_cohort(project_id: str) -> dict | None:
    cohorts = list_cohorts(project_id)
    return cohorts[0] if cohorts else None


def read_cuts(project_id: str, cohort_id: str) -> dict:
    """All cached cut analyses for a cohort, keyed by cut_key()."""
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return {}
    return analyze_store.read_cuts(analyze_store.connect(), project_id, cohort_id)


def read_cut(project_id: str, cohort_id: str, key: str) -> dict | None:
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return None
    return analyze_store.read_cut(analyze_store.connect(), project_id, cohort_id, key)


def write_cut(project_id: str, cohort_id: str, key: str, cut: dict) -> dict:
    """Cache one cut. A done cohort is immutable, so this only needs busting
    when the question changes (a reworded prompt), never when the data does.

    One row per cut: two cuts of the same cohort finishing together no longer
    race through a read-mutate-write of the whole map.
    """
    return analyze_store.write_cut(
        analyze_store.connect(), project_id, safe_cohort_id(cohort_id), key, cut
    )


def clear_cut(project_id: str, cohort_id: str, key: str) -> bool:
    """Drop one cached cut so the next run re-asks the model. Returns whether
    anything was actually dropped."""
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return False
    return analyze_store.clear_cut(analyze_store.connect(), project_id, cohort_id, key)


def forget_session(project_id: str, session_id: str) -> int:
    """Remove a deleted session from every cohort in this project.

    Call this wherever a session's recording is removed. A membership row
    points at a write-up keyed by session_id, so once that is gone the row is
    unsupportable — and `enrich_cohort_status` trusts the stored status when it
    finds nothing, so the row would keep counting as done.
    """
    return analyze_store.forget_session(analyze_store.connect(), project_id, str(session_id))


def forget_pool_cohorts(project_id: str) -> int:
    """Drop every cohort in a project, for a whole-project index reset."""
    return analyze_store.forget_pool(analyze_store.connect(), project_id)


def mark_cohort_session(
    project_id: str,
    cohort: dict,
    entry: dict,
    status: str,
    *,
    analysis: str | None = None,
    error: str | None = None,
) -> None:
    """Record one session's status, in the cohort and in the caller's dict.

    The pipeline runs N coroutines that each own one session but share the
    cohort dict. Writing through `write_cohort` meant every mark rewrote all N
    rows from the writer's copy, so two marks landing together silently lost
    one — last writer wins, carrying its stale view of everyone else.

    This writes the single row that changed and keeps `entry` in step for the
    caller's own bookkeeping, which is what the surrounding code reads.
    """
    entry["status"] = status
    if analysis is None:
        entry.pop("analysis", None)
    else:
        entry["analysis"] = analysis
    if error is None:
        entry.pop("error", None)
    else:
        entry["error"] = error

    analyze_store.update_session_status(
        analyze_store.connect(),
        project_id,
        str(cohort["id"]),
        str(entry["session_id"]),
        status=status,
        analysis=analysis,
        error=error,
    )
