"""Where analysis results and cohorts live, addressed by project_id.

Callers identify a tenant by its Postgres project UUID. Session write-ups and
cohort state live in Postgres; Path appears only for recording files (chunks)
and regenerable scratch under `.analysis/`.

It is deliberately leaf-ward. Nothing here reads a name the tests patch -- no
prompts, no model config, no `_gates` -- and nothing calls back into
orchestration. That is what makes it separable at all; see
`tests/server/test_patch_contract.py`.

The legacy JSON layouts (`cohort.json`, `cohorts/*.json`) are read only by the
migration helpers below, which run when explicitly requested. The steady state
is a directory that does not exist.
"""

import json
import re
import shutil
import time
from pathlib import Path

from .. import projects
from . import store as analyze_store

ANALYSIS_FILE = "analysis.json"
ANALYSIS_DIR = ".analysis"
# Pre-multi-cohort layout: one cohort.json per pool, migrated on first read.
COHORT_FILE = "cohort.json"
COHORTS_DIR = "cohorts"
CUTS_SUFFIX = ".cuts.json"
# Per-project pools live at <data root>/projects/<name>/sessions; the default
# pool is <data root>/sessions. Same layout server.py indexes by.
PROJECTS_DIR = "projects"
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
    """Where cohort JSON used to live, beside the sessions directory.

    Only the migration and Batch JSONL scratch read this now; cohort state is
    in Postgres.
    """
    return pool.parent / COHORTS_DIR


def _is_project_pool(pool: Path) -> bool:
    """`<root>/projects/<name>/sessions` rather than `<root>/sessions`.

    Tested on the grandparent, not the parent: a data root that happens to be
    named "projects" would otherwise read its own default pool as a project.
    """
    return pool.parent.parent.name == PROJECTS_DIR


def data_root_for(pool: Path) -> Path:
    """The data root owning this pool — where index.db used to live."""
    if _is_project_pool(pool):
        return pool.parent.parent.parent
    return pool.parent


def pool_key(pool: Path) -> str:
    """Stable filesystem handle for a sessions pool. Default is ""; a named
    project pool is its directory name. Not used for Postgres access — that
    is project_id — but still useful for layout tests and legacy migration."""
    return pool.parent.name if _is_project_pool(pool) else ""


def cohort_path(pool: Path, cohort_id: str) -> Path:
    """Legacy JSON path. Retained for the migration and for id validation."""
    return cohorts_dir(pool) / f"{safe_cohort_id(cohort_id)}.json"


def safe_cohort_id(cohort_id: str) -> str:
    """Cohort ids reach the filesystem, so keep them to a known-safe alphabet."""
    raw = str(cohort_id or "").strip()
    if not raw or not SAFE_COHORT_ID.match(raw):
        raise ValueError("bad cohort id")
    return raw


def _read_cohort_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def migrate_legacy_cohort(pool: Path) -> None:
    """Move a pre-multi-cohort cohort.json into the cohorts directory, once."""
    legacy = pool.parent / COHORT_FILE
    if not legacy.is_file():
        return
    data = _read_cohort_file(legacy)
    if data and isinstance(data.get("id"), str):
        try:
            target = cohort_path(pool, data["id"])
        except ValueError:
            legacy.unlink(missing_ok=True)
            return
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data, indent=2))
    legacy.unlink(missing_ok=True)


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


def cuts_path(pool: Path, cohort_id: str) -> Path:
    """Legacy cuts filename. The migration reads this shape; cuts live in the
    store now. Kept alongside cohort_path so both legacy names validate the id
    the same way."""
    return cohorts_dir(pool) / f"{safe_cohort_id(cohort_id)}{CUTS_SUFFIX}"


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
