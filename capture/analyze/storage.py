"""Where analysis results and cohorts live, addressed the way callers address them.

Every caller here identifies a session by its directory and a tenant by its pool
path, because the analyser is file-based. The results themselves moved into
Postgres, so this module is the translation layer: `(pool, session_dir)` in,
`(pool_key, session_id)` out, `store.py` underneath.

It is deliberately leaf-ward. Nothing here reads a name the tests patch -- no
prompts, no model config, no `_gates` -- and nothing calls back into the
orchestration in `__init__.py`. That is what makes it separable at all; see the
patching contract at the foot of `__init__.py` and
`tests/server/test_patch_contract.py`.

The legacy JSON layouts (`cohort.json`, `cohorts/*.json`) are read only by the
migration below, which consumes them on import. The steady state is a directory
that does not exist.
"""

import json
import re
import shutil
import threading
import time
from pathlib import Path

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


def _session_ref(session_dir: Path) -> tuple:
    """(pool_key, session_id) for a session directory. The results moved into
    the database, but every caller still identifies a session by its path."""
    return pool_key(session_dir.parent), session_dir.name


def read_analysis(session_dir: Path) -> dict | None:
    key, sid = _session_ref(session_dir)
    return analyze_store.read_analysis(analyze_store.connect(), key, sid)


def write_analysis(session_dir: Path, result: dict) -> dict:
    key, sid = _session_ref(session_dir)
    return analyze_store.write_analysis(analyze_store.connect(), key, sid, result)


def forget_analyses(pool: Path) -> int:
    """Drop every write-up in a pool. For a whole-pool reset."""
    return analyze_store.forget_analyses(analyze_store.connect(), pool_key(pool))


def read_job(session_dir: Path) -> dict | None:
    """In-progress or failed async analysis job, if any."""
    key, sid = _session_ref(session_dir)
    return analyze_store.read_job(analyze_store.connect(), key, sid)


def write_job(session_dir: Path, status: str, error: str | None = None) -> dict:
    job = {"status": status, "updated_at": int(time.time() * 1000)}
    if error:
        job["error"] = error
    key, sid = _session_ref(session_dir)
    return analyze_store.write_job(analyze_store.connect(), key, sid, job)


def clear_job(session_dir: Path) -> None:
    key, sid = _session_ref(session_dir)
    analyze_store.clear_job(analyze_store.connect(), key, sid)


def clear_analysis(session_dir: Path) -> None:
    """Drop a finished result so a re-analyze starts clean."""
    key, sid = _session_ref(session_dir)
    analyze_store.clear_analysis(analyze_store.connect(), key, sid)
    # Sidecars from the retired theme/QA pass, and the pre-database result —
    # inert now, but a re-analyze is the natural moment to stop carrying them.
    for legacy in (ANALYSIS_FILE, "analysis.insights.json", "analysis.qa.json"):
        (session_dir / legacy).unlink(missing_ok=True)
    # The frame cache is regenerable scratch for one run, so it stays local.
    shutil.rmtree(session_dir / ANALYSIS_DIR, ignore_errors=True)


def cohorts_dir(pool: Path) -> Path:
    """Where cohort JSON used to live, beside the sessions directory.

    Only the migration reads this now; cohort state is in Postgres.
    """
    return pool.parent / COHORTS_DIR


def _is_project_pool(pool: Path) -> bool:
    """`<root>/projects/<name>/sessions` rather than `<root>/sessions`.

    Tested on the grandparent, not the parent: a data root that happens to be
    named "projects" would otherwise read its own default pool as a project.
    """
    return pool.parent.parent.name == PROJECTS_DIR


def data_root_for(pool: Path) -> Path:
    """The data root owning this pool — where index.db lives."""
    if _is_project_pool(pool):
        return pool.parent.parent.parent
    return pool.parent


def pool_key(pool: Path) -> str:
    """Stable pool identifier for the store. Matches server.pool_key: the
    default pool is "", a project pool is its directory name."""
    return pool.parent.name if _is_project_pool(pool) else ""


def cohort_store(pool: Path):
    """(connection, pool_key) for this pool, migrating legacy JSON once."""
    conn = analyze_store.connect(data_root_for(pool))
    key = pool_key(pool)
    _migrate_cohorts_once(conn, pool, key)
    return conn, key


_migrated: set = set()
_migrate_lock = threading.Lock()


def _migrate_cohorts_once(conn, pool: Path, key: str) -> None:
    """Import this pool's cohort files into the store.

    Two legacy layouts feed in: the pre-multi-cohort `cohort.json`, and the
    `cohorts/*.json` directory. Both are consumed on import, so the steady
    state is a directory that does not exist.

    Only the directory scan is latched per process — the hundreds of store
    calls a cohort run makes must not each glob it. `cohort.json` is a single
    is_file() on a path that normally does not exist, and it can appear after
    this pool was first touched (an older deployment writing beside a running
    server), so that check stays live. The import is idempotent either way: an
    existing row wins, so a second process cannot double-import.
    """
    try:
        migrate_legacy_cohort(pool)
    except Exception as exc:
        print(f"legacy cohort migration failed for pool {key or '(default)'}: {exc}")

    directory = cohorts_dir(pool)
    token = (str(data_root_for(pool).resolve()), key)
    with _migrate_lock:
        latched = token in _migrated
        if latched and not directory.is_dir():
            return
        _migrated.add(token)
    try:
        analyze_store.migrate_json_cohorts(conn, key, directory, CUTS_SUFFIX)
    except Exception as exc:
        # A failed import must not take down the request that triggered it, but
        # it must be visible — the cohorts would silently read as absent.
        print(f"cohort migration failed for pool {key or '(default)'}: {exc}")


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


def read_cohort(pool: Path, cohort_id: str) -> dict | None:
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return None
    conn, key = cohort_store(pool)
    return analyze_store.read_cohort(conn, key, cohort_id)


def write_cohort(pool: Path, cohort: dict) -> dict:
    conn, key = cohort_store(pool)
    return analyze_store.write_cohort(
        conn, key, {**cohort, "id": safe_cohort_id(cohort["id"])}
    )


def list_cohorts(pool: Path) -> list:
    """Every cohort in this pool, newest first. Membership only — no write-ups."""
    conn, key = cohort_store(pool)
    return analyze_store.list_cohorts(conn, key)


def latest_cohort(pool: Path) -> dict | None:
    cohorts = list_cohorts(pool)
    return cohorts[0] if cohorts else None


def cuts_path(pool: Path, cohort_id: str) -> Path:
    """Legacy cuts filename. The migration reads this shape; cuts live in the
    store now. Kept alongside cohort_path so both legacy names validate the id
    the same way."""
    return cohorts_dir(pool) / f"{safe_cohort_id(cohort_id)}{CUTS_SUFFIX}"


def read_cuts(pool: Path, cohort_id: str) -> dict:
    """All cached cut analyses for a cohort, keyed by cut_key()."""
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return {}
    conn, key = cohort_store(pool)
    return analyze_store.read_cuts(conn, key, cohort_id)


def read_cut(pool: Path, cohort_id: str, key: str) -> dict | None:
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return None
    conn, pkey = cohort_store(pool)
    return analyze_store.read_cut(conn, pkey, cohort_id, key)


def write_cut(pool: Path, cohort_id: str, key: str, cut: dict) -> dict:
    """Cache one cut. A done cohort is immutable, so this only needs busting
    when the question changes (a reworded prompt), never when the data does.

    One row per cut: two cuts of the same cohort finishing together no longer
    race through a read-mutate-write of the whole map.
    """
    conn, pkey = cohort_store(pool)
    return analyze_store.write_cut(conn, pkey, safe_cohort_id(cohort_id), key, cut)


def clear_cut(pool: Path, cohort_id: str, key: str) -> bool:
    """Drop one cached cut so the next run re-asks the model. Returns whether
    anything was actually dropped."""
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return False
    conn, pkey = cohort_store(pool)
    return analyze_store.clear_cut(conn, pkey, cohort_id, key)


def forget_session(pool: Path, session_id: str) -> int:
    """Remove a deleted session from every cohort in this pool.

    Call this wherever a session's directory is removed. A membership row
    points at a write-up under the session directory, so once that is gone the
    row is unsupportable — and `enrich_cohort_status` trusts the stored status
    when it finds nothing on disk, so the row would keep counting as done.
    """
    conn, key = cohort_store(pool)
    return analyze_store.forget_session(conn, key, str(session_id))


def forget_pool_cohorts(pool: Path) -> int:
    """Drop every cohort in a pool, for a whole-pool index reset."""
    conn, key = cohort_store(pool)
    return analyze_store.forget_pool(conn, key)


def mark_cohort_session(
    pool: Path,
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

    conn, key = cohort_store(pool)
    analyze_store.update_session_status(
        conn,
        key,
        str(cohort["id"]),
        str(entry["session_id"]),
        status=status,
        analysis=analysis,
        error=error,
    )
