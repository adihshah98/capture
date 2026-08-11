"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.

Also owns cohorts: sample latest purchased + non-purchase sessions (or any
matching set, up to 500 total; default 50+50, ashwamag-01, ≥5s), persist each
sample under `cohorts/<id>.json` without touching prior cohorts, reconstruct
pages locally, then run write-ups through the Gemini Batch API (interactive
generate_content remains for single-session Analyze). A cohort is immutable
once sampled, so a "cut" across it can be analysed once and cached beside it
(`refresh=1` busts that cache).
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import List, Optional, Tuple

import analyze_store

BASE_DIR = Path(__file__).resolve().parent

ANALYSIS_FILE = "analysis.json"
ANALYSIS_STATUS_FILE = "analysis.status.json"
ANALYSIS_DIR = ".analysis"
# Pre-multi-cohort layout: one cohort.json per pool, migrated on first read.
COHORT_FILE = "cohort.json"
COHORTS_DIR = "cohorts"
CUTS_SUFFIX = ".cuts.json"
# Per-project pools live at <data root>/projects/<name>/sessions; the default
# pool is <data root>/sessions. Same layout server.py indexes by.
PROJECTS_DIR = "projects"
# Same shape as server.SAFE_CHUNK — keep local so the CLI does not import server.
SAFE_CHUNK = re.compile(r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$")
# Cohort ids are ours to mint ("cohort-<ms>"), but they land in filenames, so
# validate rather than trust anything arriving from a URL.
SAFE_COHORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ANALYZE_TIMEOUT_S = float(os.environ.get("ANALYZE_TIMEOUT_S", "300"))

# Frames come one per second of activity; dead air is skipped, so a long
# session with a little interaction stays cheap.
ANALYZE_FPS = float(os.environ.get("ANALYZE_FPS", "1"))
# Capped by default: activity-based sampling keeps a typical session well under
# this, but a continuously-active one grows a frame per second without bound
# (~200 frames for 200s), and at cohort scale those JPEGs are the OOM the batch
# path was rewritten to survive. Set 0 to disable the cap.
ANALYZE_MAX_FRAMES = int(os.environ.get("ANALYZE_MAX_FRAMES", "90"))

ANALYSIS_ENABLED = bool(GEMINI_API_KEY)

# Extraction and model calls contend for different resources, so they get
# different gates. Reconstruction drives a headless Chromium and is memory
# bound — one at a time, or a small box starts swapping. The Gemini calls are
# pure network wait, so a few can overlap and the cohort loop stops spending
# most of its wall clock idle.
ANALYZE_EXTRACT_CONCURRENCY = max(1, int(os.environ.get("ANALYZE_EXTRACT_CONCURRENCY", "1")))
ANALYZE_MODEL_CONCURRENCY = max(1, int(os.environ.get("ANALYZE_MODEL_CONCURRENCY", "3")))

# Insights cohort defaults: latest ashwamag-01 sessions ≥5s, 50/50 split.
COHORT_DEFAULT_PURCHASED_N = 50
COHORT_DEFAULT_ALL_N = 50
COHORT_MAX_SESSIONS = 500
COHORT_MIN_DURATION_MS = 5000
COHORT_URL_CONTAINS = "/pages/ashwamag-01"
# How many newest index rows to scan when applying cut-style slice filters.
COHORT_CANDIDATE_CAP = 5000
COHORT_ORDER_LATEST = "latest"
COHORT_ORDER_EARLIEST = "earliest"
COHORT_ORDERS = (COHORT_ORDER_LATEST, COHORT_ORDER_EARLIEST)

# Cohort write-ups go through Gemini Batch (≈50% cheaper). Single-session
# Analyze stays on the interactive API. Set ANALYZE_BATCH=0 to force sync.
ANALYZE_BATCH = os.environ.get("ANALYZE_BATCH", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
ANALYZE_BATCH_POLL_S = float(os.environ.get("ANALYZE_BATCH_POLL_S", "15"))
# Batch is slow by design (the 50% discount buys a queue, not a latency SLA),
# so this is a stuck-job backstop rather than a deadline anyone should hit.
# The job name stays on disk when it trips, so a resume picks the job back up
# instead of re-billing the cohort. 0 waits forever, as before.
ANALYZE_BATCH_MAX_WAIT_S = float(os.environ.get("ANALYZE_BATCH_MAX_WAIT_S", str(24 * 3600)))
# A poll that raises is usually a blip, and escalating one would cost a re-run
# of the whole cohort, so tolerate a few in a row before giving up.
ANALYZE_BATCH_POLL_MAX_ERRORS = max(
    1, int(os.environ.get("ANALYZE_BATCH_POLL_MAX_ERRORS", "5"))
)
# Inline batch payloads cap around 20MB; above this we upload a JSONL file.
ANALYZE_BATCH_INLINE_MAX_BYTES = int(
    os.environ.get("ANALYZE_BATCH_INLINE_MAX_BYTES", str(15 * 1024 * 1024))
)

# Created lazily: a Semaphore binds to the running loop, and this module is
# imported before the server's loop exists (and reused by the sync CLI).
_extract_gate: Optional[asyncio.Semaphore] = None
_model_gate: Optional[asyncio.Semaphore] = None
_gate_loop: Optional[asyncio.AbstractEventLoop] = None


EXTRACTOR = BASE_DIR / "src" / "analyze" / "extract.mjs"
REPLAYER_BUNDLE = BASE_DIR / "src" / "analyze" / "replayer.bundle.js"


def _gates() -> tuple:
    """(extract, model) semaphores bound to the running loop."""
    global _extract_gate, _model_gate, _gate_loop
    loop = asyncio.get_running_loop()
    if _gate_loop is not loop or _extract_gate is None or _model_gate is None:
        _extract_gate = asyncio.Semaphore(ANALYZE_EXTRACT_CONCURRENCY)
        _model_gate = asyncio.Semaphore(ANALYZE_MODEL_CONCURRENCY)
        _gate_loop = loop
    return _extract_gate, _model_gate

SYSTEM_PROMPT = """You are a senior consumer behavior and marketing analyst analyzing one recorded website session for a consumer brand.

You receive:
•⁠  ⁠session metadata: the entry URL and landing page, any utm_* / fbclid attribution on it, the purchase outcome, and order value and products when the session bought. Fields that are absent are unknown — a session with no attribution may be direct, organic, or simply untagged, so never assert a traffic source that is not given
•⁠  ⁠a measured session digest
•⁠  ⁠chronological replay frames labelled [MM:SS]

Tie the behavior back to how they arrived. A visitor who landed from a
campaign arrived with an expectation the ad set, and the landing page either
met it or did not — that gap is often the whole story of the session. Where
the metadata gives you a campaign or landing page, say what the behavior
suggests about the match between the two.

Your job is not just to describe what happened. Reconstruct the visitor’s likely decision-making process and infer what their behavior reveals about them as a consumer.

Be aggressive but evidence-based in making hypotheses about:
•⁠  ⁠purchase intent and shopping mode
•⁠  ⁠what they care about most
•⁠  ⁠motivations and decision criteria
•⁠  ⁠objections, anxieties, or unanswered questions
•⁠  ⁠price sensitivity, trust, and need for social proof
•⁠  ⁠why intent strengthened or weakened
•⁠  ⁠likely reason for purchase or abandonment

Look at sequences of behavior, not isolated actions. Ask what the visitor appears to be trying to learn, validate, compare, or resolve.

Clearly distinguish observed behavior from interpretation. Use language like “likely,” “appears,” and “suggests” for inferred motivations. Do not infer sensitive personal characteristics.

Ground claims in the frames, digest, and metadata. Never invent actions, elements, numbers, or information not shown. Frames prove what was on screen, not what the visitor actually read or looked at. A long pause may indicate consideration, distraction, or inactivity, so interpret it using surrounding behavior.

Write these sections:

## Consumer read
2 to 4 sentences describing who this shopper appears to be, their intent, what matters to them, their main hesitation, and what likely drove the outcome.

## Decision journey
Explain the key moments in how their decision process evolved, with [MM:SS] citations. Focus on moments where intent strengthened, weakened, or a new question emerged. Skip insignificant activity.

## What they care about
Identify the 2 to 4 strongest inferred purchase drivers, questions, or objections and explain the behavioral evidence for each.

## Conversion diagnosis
Explain the most likely reason they purchased or did not purchase. If several explanations are plausible, rank them. Distinguish actual friction from simple low intent.

## Marketing implications
Give 2 to 4 specific implications for messaging, PDPs, offers, merchandising, personalization, acquisition, retargeting, or lifecycle marketing. Avoid generic CRO advice. Focus on insights that would matter if the same pattern appears across many sessions. When the session carries a campaign or landing page, make at least one implication about that pairing — whether the page delivered what the campaign promised, and what to change on whichever side is failing.

## Friction
Briefly identify concrete UX issues such as failed clicks, rage clicks, confusion, backtracking, or technical problems. Do not mistake normal consideration for friction.

A checkout screen that sits unchanged for a long time is NOT a technical bug. Shopify hosts checkout outside the storefront, so recording stops there and the last frames often freeze on checkout while the visitor continues on Shopify's page. Do not flag that freeze, a "stuck" checkout UI, or a long idle at checkout as a site failure or tech issue.

End with:

Shopper type: [description] | Intent: [low/medium/high] | Primary driver: [driver] | Main barrier: [barrier] | Outcome: [short explanation]

Keep the analysis concise, opinionated, and inference-heavy. Prefer explaining why the visitor behaved this way over recounting everything they did. """

def runtime_present() -> bool:
    """Whether the headless reconstruction can run: node, the extractor, the bundle."""
    return bool(
        shutil.which("node") and EXTRACTOR.is_file() and REPLAYER_BUNDLE.is_file()
    )


def analysis_paths(session_dir: Path) -> tuple:
    return session_dir / ANALYSIS_FILE, session_dir / ANALYSIS_DIR


def status_path(session_dir: Path) -> Path:
    return session_dir / ANALYSIS_STATUS_FILE


def _session_ref(session_dir: Path) -> tuple:
    """(pool_key, session_id) for a session directory. The results moved into
    the database, but every caller still identifies a session by its path."""
    return pool_key(session_dir.parent), session_dir.name


def read_analysis(session_dir: Path) -> Optional[dict]:
    key, sid = _session_ref(session_dir)
    return analyze_store.read_analysis(analyze_store.connect(), key, sid)


def write_analysis(session_dir: Path, result: dict) -> dict:
    key, sid = _session_ref(session_dir)
    return analyze_store.write_analysis(analyze_store.connect(), key, sid, result)


def forget_analyses(pool: Path) -> int:
    """Drop every write-up in a pool. For a whole-pool reset."""
    return analyze_store.forget_analyses(analyze_store.connect(), pool_key(pool))


def read_job(session_dir: Path) -> Optional[dict]:
    """In-progress or failed async analysis job, if any."""
    key, sid = _session_ref(session_dir)
    return analyze_store.read_job(analyze_store.connect(), key, sid)


def write_job(session_dir: Path, status: str, error: Optional[str] = None) -> dict:
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

    Only the migration reads this now; cohort state is in SQLite.
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


def _read_cohort_file(path: Path) -> Optional[dict]:
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


def read_cohort(pool: Path, cohort_id: str) -> Optional[dict]:
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


def latest_cohort(pool: Path) -> Optional[dict]:
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


def read_cut(pool: Path, cohort_id: str, key: str) -> Optional[dict]:
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
    analysis: Optional[str] = None,
    error: Optional[str] = None,
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


class CohortState:
    """The statuses a cohort can hold. Exactly the six that are persisted.

    The wider vocabulary the refactor notes sketch (extracting / submitted /
    applying) is not here on purpose: those are phases of `run_cohort_job`'s
    control flow that nothing writes down, so declaring them would invent
    states no code can produce or observe.
    """

    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    ERROR = "error"


COHORT_STATES = (
    CohortState.IDLE,
    CohortState.QUEUED,
    CohortState.RUNNING,
    CohortState.PAUSED,
    CohortState.DONE,
    CohortState.ERROR,
)

# Which moves are legitimate. Read as "from: {allowed to}".
#
# DONE and ERROR are not frozen. A terminal status is recomputed from the
# session rows, because the stored value goes stale — a crashed runner leaves
# a cohort marked running, a retried session turns an errored cohort back into
# a finished one. The rows are the truth; the stored status is a cache.
COHORT_TRANSITIONS = {
    CohortState.IDLE: {CohortState.QUEUED, CohortState.RUNNING, CohortState.PAUSED,
                       CohortState.DONE, CohortState.ERROR, CohortState.IDLE},
    CohortState.QUEUED: {CohortState.RUNNING, CohortState.PAUSED, CohortState.DONE,
                         CohortState.ERROR, CohortState.QUEUED},
    CohortState.RUNNING: {CohortState.DONE, CohortState.ERROR, CohortState.PAUSED,
                          CohortState.RUNNING},
    CohortState.PAUSED: {CohortState.RUNNING, CohortState.QUEUED, CohortState.DONE,
                         CohortState.PAUSED},
    # Terminal, but recomputed: see above. QUEUED is reachable from both
    # because a write-up can go missing after the fact (rule 5), which returns
    # the cohort to needing work.
    CohortState.DONE: {CohortState.DONE, CohortState.ERROR, CohortState.RUNNING,
                       CohortState.QUEUED},
    CohortState.ERROR: {CohortState.ERROR, CohortState.DONE, CohortState.RUNNING,
                        CohortState.QUEUED},
}


def cohort_transition(current: str, nxt: str, *, why: str = "") -> str:
    """Record a status move, reporting any the table does not declare.

    Deliberately permissive: this runs inside a live cohort run with a paid
    Gemini batch in flight, so an undeclared move must not raise and strand it.
    The log is the signal — `tests/test_cohort_state.py` drives the real
    pipeline and asserts nothing is logged, so the table is verified without
    the enforcement being able to break a run.
    """
    if current not in COHORT_TRANSITIONS:
        return nxt
    if nxt not in COHORT_TRANSITIONS[current]:
        print(f"cohort status {current} -> {nxt} is not a declared transition ({why})")
        _undeclared_transitions.append((current, nxt, why))
    return nxt


# Test hook: the pipeline should never append here. Not a log sink — bounded
# so a long-running server cannot accumulate.
_undeclared_transitions: list = []


# A row that claims to be analyzed but has no write-up behind it. Not an
# error (nothing failed) and not queued (nothing scheduled it): the result was
# removed after the fact — `clear_analysis` on a re-analyze, a deleted file, a
# crash between clearing and re-queuing. Named so the cohort can decline to
# count it as done instead of reporting a result it cannot show.
SESSION_STALE = "stale"


def project_cohort_status(stored: str, tally: dict, *, total: int) -> str:
    """Decide a cohort's status from its session tallies.

    A projection, not an inference: everything it needs is in `tally`, and the
    filesystem is not consulted. The four rules, in priority order:

      1. Nothing to do (no sessions, or all done) — the cohort is done.
      2. IDLE and PAUSED are sticky. Sampled-but-not-started stays that way
         until the user starts it, and paused stays paused even if a stale job
         still looks alive. Only genuine in-flight work overrides them.
      3. Work outstanding — running. A queued row revives only a cohort that
         was already RUNNING or QUEUED, so an errored cohort is not silently
         resurrected by one leftover row; the user has to retry.
      4. Everything settled with at least one error — done if anything
         succeeded, error if nothing did.

    A `stale` row (claims done, has no write-up) counts as outstanding
    throughout: it blocks rule 1, and rule 4 will not call the cohort settled
    while one is present. The cohort must not report a completed analysis it
    cannot substantiate.
    """
    current = stored if stored in COHORT_STATES else CohortState.IDLE
    done, running, queued, errors = (
        tally["done"], tally["running"], tally["queued"], tally["error"],
    )
    stale = tally.get("stale", 0)

    # 1. Nothing left to do.
    if not total or (done >= total and not stale):
        return cohort_transition(current, CohortState.DONE, why="all sessions accounted for")

    # 2. Sticky states.
    if current in (CohortState.IDLE, CohortState.PAUSED):
        if running:
            return cohort_transition(current, CohortState.RUNNING, why="work already in flight")
        return current

    # 3. Work outstanding.
    if running or (queued and current in (CohortState.RUNNING, CohortState.QUEUED)):
        return cohort_transition(current, CohortState.RUNNING, why="sessions still to analyze")

    # 4. Settled with errors.
    if errors and done + errors >= total and not queued and not running and not stale:
        settled = CohortState.ERROR if done == 0 else CohortState.DONE
        return cohort_transition(current, settled, why="every session settled")

    # 5. Stale rows with nothing in flight. Not done — the write-ups those
    # rows claim are gone — but nothing is working on them either, so the
    # cohort needs a re-analyze rather than a wait. `queued` says exactly
    # that: work outstanding, nothing running.
    if stale and current in (CohortState.DONE, CohortState.ERROR):
        return cohort_transition(current, CohortState.QUEUED, why="write-ups missing, needs re-analysis")

    return current


def enrich_cohort_status(pool: Path, cohort: dict) -> dict:
    """Refresh per-session status from analysis.json / analysis.status.json on disk."""
    sessions = []
    done = running = queued = errors = stale = 0
    for entry in cohort.get("sessions") or []:
        row = dict(entry)
        # Insights text is attached on read for the API — never persist it here.
        row.pop("text", None)
        row.pop("standout", None)
        row.pop("model", None)
        row.pop("frame_count", None)
        # Derived from the index on read — never frozen into the cohort file.
        row.pop("attribution", None)
        row.pop("device", None)
        row.pop("outcome", None)
        session_dir = pool / row["session_id"]
        cached = read_analysis(session_dir)
        job = read_job(session_dir)
        if cached:
            row["status"] = "done"
            row["analysis"] = ANALYSIS_FILE
            row.pop("error", None)
            done += 1
        elif job and job.get("status") == "running":
            row["status"] = "running"
            running += 1
        elif job and job.get("status") == "error":
            row["status"] = "error"
            row["error"] = job.get("error") or row.get("error") or "analysis failed"
            errors += 1
        elif row.get("status") == "error":
            errors += 1
        elif row.get("status") == "done":
            # Claims done, but the branches above found no analysis.json. The
            # write-up was removed after the fact (clear_analysis on a
            # re-analyze, a deleted file, a crash between the two). Counting it
            # done would have the cohort report a result with nothing behind it.
            row["status"] = SESSION_STALE
            row["error"] = row.get("error") or "write-up is missing — re-analyze this session"
            stale += 1
        else:
            row["status"] = "queued"
            queued += 1
        sessions.append(row)

    progress = {
        "total": len(sessions),
        "done": done,
        "running": running,
        "queued": queued,
        "error": errors,
        "stale": stale,
    }
    status = project_cohort_status(
        cohort.get("status") or CohortState.IDLE, progress, total=len(sessions)
    )

    selection = cohort.get("selection")
    if not selection:
        selection = cohort_selection_text(
            int(cohort.get("purchased_n") or 0),
            int(cohort.get("all_n") or 0),
            any_n=int(cohort.get("any_n") or 0),
            a_n=int(cohort.get("a_n") or 0),
            b_n=int(cohort.get("b_n") or 0),
            min_duration_ms=int(
                cohort.get("min_duration_ms")
                if cohort.get("min_duration_ms") is not None
                else COHORT_MIN_DURATION_MS
            ),
            url_contains=str(cohort.get("url_contains") or ""),
            slice_spec=cohort.get("slice"),
            slice_b=cohort.get("slice_b"),
            start_ms=cohort.get("start_ms"),
            end_ms=cohort.get("end_ms"),
            order=str(cohort.get("order") or COHORT_ORDER_LATEST),
        )

    return {
        **cohort,
        "selection": selection,
        "status": status,
        "sessions": sessions,
        "progress": progress,
    }


# Computed for the API response and never written to the cohort file.
# `progress` is a tally of `sessions`, so persisting it leaves a second copy
# that any other writer updates out from under.
COHORT_VIEW_ONLY_FIELDS = ("progress",)
# Per-session fields the response layers on: write-ups from analysis.json
# (attach_cohort_insights) and facts from the index (server.stamp_cohort_
# attribution). enrich_cohort_status already pops these on the way in; stripping
# them again on the way out means the guard holds whatever ran before it.
COHORT_VIEW_ONLY_SESSION_FIELDS = (
    "text",
    "standout",
    "model",
    "frame_count",
    "attribution",
    "device",
    "outcome",
)


def persistable_cohort(enriched: dict, stored: Optional[dict] = None) -> dict:
    """Strip an enriched cohort back to what belongs on disk.

    The status refresh itself is a durable fact — a crashed runner leaves rows
    saying "running" that only a re-read of analysis.json corrects — so callers
    do need to write it back. What they must not write is the view laid over it.

    `selection` is the trap. enrich backfills it from whatever sampling fields
    are present, so for a cohort sampled by any_n / a_n (where purchased_n and
    all_n are 0) the guess reads "latest 0 purchased + 0 non-purchase". Since
    the backfill only fires when the field is missing, writing that guess makes
    it permanent — in the one field that records how the cohort was built.
    `stored` is the cohort as it exists on disk: it says whether `selection`
    was genuinely recorded (keep it) or invented by this pass (drop it, and let
    each read re-derive from live fields).
    """
    out = {k: v for k, v in enriched.items() if k not in COHORT_VIEW_ONLY_FIELDS}
    if stored is not None and not stored.get("selection"):
        out.pop("selection", None)
    sessions = out.get("sessions")
    if isinstance(sessions, list):
        out["sessions"] = [
            {
                k: v
                for k, v in row.items()
                if k not in COHORT_VIEW_ONLY_SESSION_FIELDS
            }
            if isinstance(row, dict)
            else row
            for row in sessions
        ]
    return out


def standout_line(text: str) -> str:
    """First useful line under ## What stands out, else the first non-heading line."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]
    in_standout = False
    for line in lines:
        if re.match(r"^#{1,4}\s+what stands out\s*$", line, re.I):
            in_standout = True
            continue
        if in_standout:
            if re.match(r"^#{1,4}\s+", line):
                break
            bullet = re.match(r"^[-*]\s+(.*)$", line)
            if bullet and bullet.group(1).strip():
                return bullet.group(1).strip()
            if line:
                return line
    for line in lines:
        if line and not re.match(r"^#{1,4}\s+", line):
            return re.sub(r"^[-*]\s+", "", line)
    return ""


def attach_cohort_insights(pool: Path, cohort: dict) -> dict:
    """Attach analysis write-ups — response-only, never written to the cohort file."""
    sessions = []
    for entry in cohort.get("sessions") or []:
        row = dict(entry)
        if row.get("status") == "done":
            session_dir = pool / row["session_id"]
            cached = read_analysis(session_dir)
            if cached and isinstance(cached.get("text"), str) and cached["text"].strip():
                row["text"] = cached["text"]
                row["standout"] = standout_line(cached["text"])
                if cached.get("model"):
                    row["model"] = cached["model"]
                if cached.get("frame_count") is not None:
                    row["frame_count"] = cached["frame_count"]
        sessions.append(row)
    return {**cohort, "sessions": sessions}


META_SOURCES = frozenset({"facebook", "meta", "fb", "instagram", "ig"})

# Same threshold the session detail panel uses, so one session is never called
# mobile in one place and desktop in another.
MOBILE_MAX_WIDTH = 768
DEVICE_VALUES = ("mobile", "desktop")

# purchased ⊃ checkout: a purchase implies checkout was reached, so "checkout"
# here means reached checkout and did NOT purchase — the three are disjoint.
OUTCOME_VALUES = ("purchased", "checkout", "rest")


def device_class(device: Optional[dict]) -> Optional[str]:
    """mobile/desktop from the indexed viewport, or None when unknown."""
    if not isinstance(device, dict):
        return None
    width = device.get("width")
    if not isinstance(width, int) or width <= 0:
        return None
    return "mobile" if width < MOBILE_MAX_WIDTH else "desktop"


def outcome_class(order: Optional[dict]) -> str:
    """purchased | checkout (reached, did not buy) | rest."""
    if not isinstance(order, dict):
        return "rest"
    if order.get("purchased_at"):
        return "purchased"
    if order.get("checkout_started_at"):
        return "checkout"
    return "rest"


def is_meta_attribution(attribution: Optional[dict]) -> bool:
    attrs = attribution if isinstance(attribution, dict) else {}
    if attrs.get("fbclid"):
        return True
    source = str(attrs.get("utm_source") or "").strip().lower()
    return source in META_SOURCES


# A hostname we would accept on an entry URL that arrived without a scheme.
# Deliberately strict: urlparse will read a host out of almost anything, and
# whatever is not a host is not a landing page.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?::\d{1,5})?$")


def _unquote_segments(path: str) -> str:
    """Percent-decode each segment, keeping encoded separators encoded.

    Decoding the whole path turns %2F into a separator, so `/a%2Fb` — one
    segment containing a slash — would read as the two-segment `/a/b` and merge
    two distinct pages into one facet. Decoding per segment is not enough on
    its own either, since the decoded text can reintroduce the separator, so a
    slash produced by decoding is put back as %2F. The path is only ever used
    as an identity key (facet value, filter match, display label), so keeping
    it unambiguous matters more than showing the prettiest spelling.
    """
    return "/".join(
        urllib.parse.unquote(part).replace("/", "%2F") for part in path.split("/")
    )


def page_path(url: Optional[str]) -> Optional[str]:
    """Landing-page path from an entry URL, or None when there is not one.

    Pathname only: no query or hash. Trailing slashes are stripped (except `/`)
    so `/p` and `/p/` share a facet. Query strings stay on attribution — putting
    them here would explode the picker with one option per UTM combo.

    None means unknown, and every consumer already handles it: row_matches_slice
    refuses it against a page constraint, cross-tabs leave it out, and the facet
    picker skips it. So anything unreadable as an http(s) page — junk, "null",
    about:blank, a data: URI — is None rather than a guess. Guessing lands it on
    `/`, where it is indistinguishable from a real homepage visit and gets
    offered to the user as a filter, counted in a measured split, and stated to
    the model as fact.
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("/") and "://" not in raw:
            # Already a path, so there is no host to check.
            path = _unquote_segments(raw.split("?", 1)[0].split("#", 1)[0])
        else:
            bare = "://" not in raw
            parsed = urllib.parse.urlparse(f"https://{raw}" if bare else raw)
            if parsed.scheme not in ("http", "https"):
                return None
            if not parsed.hostname or not _HOSTNAME_RE.match(parsed.netloc):
                return None
            if bare and "." not in parsed.hostname and not parsed.path:
                # A scheme-less single word with no path ("null", "undefined").
                # A real bare host reaching here carries either a dot or a
                # path; "localhost" alone is not an entry URL we can place.
                return None
            # A real host with an empty path is the homepage.
            path = _unquote_segments(parsed.path) or "/"
    except Exception:
        return None
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or None


def normalize_slice(spec: Optional[dict]) -> dict:
    """Normalize a slice: any combination of outcome, device, page and attribution.

    Keys: outcome (list of purchased|checkout|rest), device (list of
    mobile|desktop), page (landing path), utm_source, utm_medium, utm_campaign,
    utm_content, fbclid. utm_source="meta" matches Meta traffic (fbclid or a
    known Meta source).

    Constraints are ANDed across keys and ORed within a key, so
    {"outcome": ["purchased", "checkout"], "device": ["mobile"]} reads as
    "reached checkout on mobile, bought or not".

    Selecting every value of a dimension drops the constraint only where the
    dimension is total — see `_multi`.
    """
    if not isinstance(spec, dict):
        return {}
    out = {}

    def _multi(key, allowed, *, total: bool):
        """Clean one multi-valued key.

        `total` says whether every session has a value here. Selecting every
        value is the same as no constraint only when it is: outcome_class()
        always returns one of its three, but device_class() returns None for an
        unindexed viewport, and row_matches_slice refuses those unknowns
        against any device constraint. Collapsing "mobile or desktop" to no
        filter would therefore widen the selection to include them.
        """
        raw = spec.get(key)
        if raw is None or raw == "":
            return
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        clean = []
        for value in values:
            text = str(value or "").strip().lower()
            # "all" is the legacy spelling of "everyone who did not purchase".
            if key == "outcome" and text == "all":
                for legacy in ("checkout", "rest"):
                    if legacy not in clean:
                        clean.append(legacy)
                continue
            if text in allowed and text not in clean:
                clean.append(text)
        if clean and (not total or len(clean) < len(allowed)):
            out[key] = sorted(clean)

    _multi("outcome", OUTCOME_VALUES, total=True)
    _multi("device", DEVICE_VALUES, total=False)

    page = page_path(spec.get("page")) if spec.get("page") not in (None, "") else None
    if page:
        out["page"] = page

    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip().lower()
    if "fbclid" in spec and spec["fbclid"] is not None:
        out["fbclid"] = bool(spec["fbclid"])
    return out


def slice_is_empty(spec: dict) -> bool:
    return not spec


def row_matches_slice(row: dict, spec: dict) -> bool:
    if not spec:
        return True
    want_outcome = spec.get("outcome")
    if want_outcome and (row.get("outcome") or "rest") not in want_outcome:
        return False
    want_device = spec.get("device")
    if want_device:
        # Unknown device can't satisfy a device constraint.
        if (row.get("device") or "") not in want_device:
            return False
    want_page = spec.get("page")
    if want_page:
        # Unknown / missing landing page can't satisfy a page constraint.
        if (row.get("page") or "") != want_page:
            return False
    attrs = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
    if "fbclid" in spec:
        has = bool(attrs.get("fbclid"))
        if spec["fbclid"] and not has:
            return False
        if not spec["fbclid"] and has:
            return False
    for key in ("utm_medium", "utm_campaign", "utm_content"):
        want = spec.get(key)
        if not want:
            continue
        got = str(attrs.get(key) or "").strip().lower()
        if got != want:
            return False
    want_source = spec.get("utm_source")
    if want_source:
        if want_source == "meta":
            if not is_meta_attribution(attrs):
                return False
        elif want_source in ("google", "non_meta"):
            if want_source == "non_meta":
                if is_meta_attribution(attrs):
                    return False
            else:
                got = str(attrs.get("utm_source") or "").strip().lower()
                if got != want_source:
                    return False
        else:
            got = str(attrs.get("utm_source") or "").strip().lower()
            if got != want_source:
                return False
    return True


OUTCOME_LABELS = {
    "purchased": "purchased",
    "checkout": "reached checkout",
    "rest": "did not reach checkout",
}


def slice_label(spec: dict) -> str:
    if not spec:
        return "everyone else"
    parts = []

    outcome = spec.get("outcome") or []
    if outcome:
        # The common "bought or not, but got to checkout" pair reads badly as
        # a raw join, so name the union directly.
        if set(outcome) == {"purchased", "checkout"}:
            parts.append("reached checkout")
        elif set(outcome) == {"checkout", "rest"}:
            parts.append("did not purchase")
        else:
            parts.append(" or ".join(OUTCOME_LABELS[o] for o in outcome))

    device = spec.get("device") or []
    if device:
        parts.append(" or ".join(device))

    if spec.get("page"):
        parts.append(spec["page"])

    if spec.get("utm_source") == "meta" or spec.get("fbclid") is True:
        parts.append("Meta")
    elif spec.get("utm_source") == "non_meta":
        parts.append("non-Meta")
    elif spec.get("utm_source"):
        parts.append(spec["utm_source"])
    if spec.get("fbclid") is False and spec.get("utm_source") != "non_meta":
        parts.append("no fbclid")
    for key, label in (
        ("utm_medium", "medium"),
        ("utm_campaign", "campaign"),
        ("utm_content", "content"),
    ):
        if spec.get(key):
            parts.append(f"{label}={spec[key]}")
    if not parts:
        return "all sessions"
    return " · ".join(parts)


def prompt_version(text: str) -> str:
    """Short content hash of a prompt, for cache keys.

    Content-addressed rather than a hand-maintained constant: a version number
    someone has to remember to bump is exactly the mechanism that rots. Eight
    hex characters is ample to separate revisions of one prompt — this is cache
    invalidation, not integrity.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def cut_key(slice_spec: Optional[dict]) -> str:
    """Stable cache key for a cut: the filter plus the prompt that answers it.

    A cut's answer is a function of which sessions were selected *and* what the
    model was asked. Keying on the filter alone meant a reworded CUT_PROMPT
    kept serving answers generated by the old one, with no invalidation path
    and no signal — the response still said `cached: true`. Since prompt work
    is most of this module's history, that was the common case, not an edge.

    The model is deliberately not in the key. Switching GEMINI_MODEL is rare
    and intentional, and every cached cut already records the `model` that
    produced it, so it stays traceable without invalidating every cohort's
    cache at once on an env-var change.

    Cuts cached before this carried no prompt hash, so their keys no longer
    match and they are simply re-asked once.
    """
    return json.dumps(
        {
            "filter": normalize_slice(slice_spec),
            "prompt": prompt_version(CUT_PROMPT),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def select_rows_by_cut(rows: list, slice_spec: Optional[dict]) -> tuple:
    """(matching_rows, spec). An empty filter selects the whole cohort."""
    spec = normalize_slice(slice_spec)
    return [row for row in rows if row_matches_slice(row, spec)], spec


CUT_PROMPT = """You turn recorded web sessions into insights a MARKETING team can
act on: what these visitors responded to, where they lost interest, which
messaging and offers carried weight, and what the team should do next.

You receive per-session write-ups, each already grounded in that session's
replay. Work only from what the write-ups say — never invent a page, a click,
a number, or an outcome that is not written there.

## Write for a marketer, not an engineer

Your reader plans campaigns, landing pages, offers and creative. They do not
read replays and will not act on a raw log of clicks and timestamps. Every
item must answer "so what?" — what does this tell us about these customers,
and what should we do about it?

  BAD   "Clicked the supply selector at [01:12] and chose '30-Night Supply'."
  GOOD  "44% (4 of 9) switched off the default '90-Night Supply' to the
         cheapest option after ~40s on the guarantee — the 90-night default
         may be anchoring too high for first-time buyers.
         Action: test '30-Night' as the default, or lead with the guarantee
         earlier to justify the larger pack."

  BAD   "Sessions from mobile scrolled to the ingredient breakdown."
  GOOD  "Mobile visitors spent 25-60s on the ingredient breakdown, roughly
         twice as long as desktop — the scientific detail is doing real work
         for this segment.
         Action: keep the ingredient depth on mobile and consider leading
         Meta creative with the science rather than the discount."

Lead with the customer behaviour and its share, then the reading, then the
action. Timestamps and quoted labels are still required as EVIDENCE inside
the observation — they prove the claim — but they are never the point of it.

## Use percentages, not just counts

Marketers think in shares. Give the percentage first with the raw count in
brackets: "44% (4 of 9)". Round to whole numbers. With fewer than 5 sessions
a percentage overstates precision, so use "4 of 9 sessions" alone and say the
evidence is directional.

Every denominator is the size of THIS group, stated under "Already known"
below. The examples here use 9 only as an illustration — never copy 9, and
never use a total from anywhere else. The write-ups below are numbered, and
the last number is the group size: no count you write may exceed it.

## The single most important rule

The group was SELECTED by a filter, stated below as "This group is".
Everything entailed by that filter is already known and is NOT a finding.

Before you write any item, apply this test:

  "Could I have predicted this from the group's definition alone,
   without reading a single write-up?"

If yes, DELETE IT. It is not an observation, it is the filter talking.

Worked example. If the group is "purchased", every one of these is banned:
  BANNED  "clicked 'Buy now' or 'Add to cart' (6 of 7)"
  BANNED  "proceeded to checkout"
  BANNED  "completed a purchase"
  BANNED  "actively interacted with purchase options"
  BANNED  "showed strong purchase intent"
Buyers buy. That is what makes them buyers. Reporting it tells the team nothing.

The same test kills "none of them clicked a CTA" when the group is defined as
not converting, and "sessions came from campaign X" when X is the filter.

What IS a finding, for the same group:
  KEEP  "57% (4 of 7) switched off the default '90-Night Supply' before
         buying — the default may be anchoring too high."
  KEEP  "Nobody reached 'Choose your supply' by scrolling; all 4 who got
         there were carried by the sticky CTA — the offer is effectively
         invisible without it."
  KEEP  "Clicked the cart icon at [00:17] and nothing happened (1 of 5) —
         a broken path to checkout."

The difference: a finding tells the team something they did not already know
about these customers, and points at a decision — an offer, a default, a
message, a placement, a channel, a broken step. If your item does not change
what someone would do next week, it is not worth writing.

Route unavoidable outcome facts into how they happened, never that they
happened.

## Be concrete, not abstract

The write-ups are specific. Your output must stay at that same resolution.
You are not summarising them — you are pulling the specific, checkable
evidence out of them and grouping it.

Bad (abstract, unusable):  "Visitors engaged extensively with detailed
                            product information."
Good (concrete, actionable): "Scrolled past the ingredient breakdown without
                            stopping, then stalled 40s on the '30 nights or
                            every penny back' guarantee (3 of 5)."

In every item, wherever the write-ups give it, include:
- what was actually on screen — quote section headings, button labels,
  product names, and price or offer wording verbatim from the write-up
- where in the page it happened — scroll depth in px, or "near the FAQ",
  "above the fold", "the third testimonial"
- how long it took — timestamps [MM:SS] or durations
- what the click did, especially when it did nothing or did the wrong thing

Numbers and quoted labels are what makes an item worth reading. An item with
none of them is almost certainly too vague to keep.

You are reading several write-ups at once, so you can see what one session
alone cannot: the same stall on the same section, the same dead click, the
same abandoned step. Name those specifically. Resist the pull toward a tidy
summary — the team already knows the shape of their funnel, and a sentence
that would be true of any e-commerce site tells them nothing.

## Look for clusters, not a single average session

A group is rarely uniform. The sessions in it usually fall into two or three
distinct behaviours, and those splits are the most useful thing you can find.
Look for them deliberately before you write anything.

Never report "no common behaviour" or "the sessions varied" and stop there.
That is the finding — keep going and say HOW they split, and what separates
the groups. A split you can name beats a similarity you had to stretch for.

Typical splits worth naming:
- Some reached a section and others never got there
- Some used one route in (sticky CTA, nav, search) and others another
- Some stalled where others moved straight through
- Some hit a broken interaction and others never touched it
- Some moved fast and decisively, others looped back repeatedly

Write a split as ONE item that names both sides with counts and evidence:

  GOOD  "Split on how they reached 'Choose your supply': 4 arrived via the
         sticky CTA at [00:31]-[00:44] and bought; the other 5 scrolled past
         it entirely and none reached checkout."
  GOOD  "3 sessions rage-clicked the cart icon at ~[00:17] and abandoned; the
         other 6 never touched it and moved to checkout in under 90s."
  BAD   "Behaviour varied across sessions."
  BAD   "Some users engaged more than others."

A cluster of 3 in a group of 9 is a real pattern, not an anecdote — report it
as a split with its count, not as a caveat. The one-session rule below applies
to genuinely isolated sessions, not to a minority that behaves consistently.

### Split on the session labels too

Every session below is labelled with its outcome, device, landing page, traffic
source and campaign. Test each as a candidate split — these are the ones the
team can act on directly. (Any dimension named under "Already known" is the
same for every session here and cannot split anything — skip it and use the
rest.)
- outcome (purchased / reached checkout / did not reach checkout): what did
  the sessions that converted do differently on the page from those that did
  not? This is the most valuable split in the set.
- device (mobile / desktop): the same page often works very differently on
  each, and mobile and desktop usually deserve different creative.
- page (landing path, e.g. /products/cream): visitors who landed on different
  URLs arrived with different context. A page whose visitors behave differently
  is a landing-page or merchandising decision.
- utm_source and utm_campaign: visitors from different sources land with
  different expectations. A source whose visitors behave differently is a
  targeting or creative decision, and one of the most actionable splits here.

State the behavioural difference, never the label itself. The labels are given
to you — that the purchasers purchased, or that the mobile sessions were on
mobile, is not a finding (see the rule above). What they DID differently is.

  GOOD  "All 4 purchasers switched off the default '90-Night Supply'; the 5
         who did not buy left it untouched — the pack size, not the product,
         looks like the deciding factor."
  BAD   "Purchased sessions completed a purchase, unlike the others."
  GOOD  "Meta visitors went straight to price and left within 40s; Google
         visitors read through the ingredients first — the two channels are
         arriving with different questions."
  BAD   "Mobile and desktop sessions behaved differently."

When a metadata split shows no behavioural difference, say that plainly — "the
mobile and desktop sessions did the same things" is a real, useful result.

When the group really is uniform, say so plainly and move on — but only after
looking. "They all did X (9 of 9)" is a strong finding when it is true.

### Use the measured splits when they are given

Some groups arrive with a "Measured splits" block above the write-ups. Those
counts were computed directly from the session labels, not read out of the
write-ups. They are exact.

- Quote them as given. Do not recompute, re-round or restate them differently,
  and never write a share that contradicts one.
- A split listed there is real. Your job is to explain it: go to the write-ups
  for those sessions and say what they DID that the others did not. A row
  repeated back without that explanation is not a finding.
- A split NOT listed there did not clear the reporting floor. Do not assert it
  from your own reading of the write-ups — if the block says a dimension was
  examined and nothing cleared, treat that as settled and move on.
- The block tells you WHAT differs. Only the write-ups tell you WHY. An item
  built on a measured split must carry page evidence — a quoted label, an
  [MM:SS], a duration — exactly like any other item.
- When no block appears, or a dimension is missing from it, fall back to
  reading the labels yourself and hedge accordingly.

### Cross the dimensions, not just each one alone

The strongest findings usually sit at an intersection: not "mobile visitors"
and not "Meta visitors" but "Meta visitors on mobile". One channel can work on
one device and fail on another, and a landing page can suit one campaign's
traffic and mismatch another's. Those are the splits a team can act on
immediately, because each names a specific place to spend or stop spending.

Test combinations wherever the group is big enough to support them:
- traffic source × device — is the creative wrong, or just the mobile page?
- campaign × landing page — did the page deliver what the ad promised?
- source or campaign × outcome — which spend actually converts?

  GOOD  "Meta traffic on mobile bounced from the price block within 30s
         (5 of 20), while Meta on desktop sat 40s+ on the guarantee and
         converted (4 of 6) — the mobile page buries the proof the ad
         promised."
  BAD   "Meta and mobile sessions both underperformed."

The same rules apply: a combination needs enough sessions behind it to be
worth naming, and the intersection of two labels is never itself a finding —
what those sessions did differently is.

## Citing sessions

Each write-up below is headed "--- SESSIONS session N ---". When an item points
at specific sessions, cite them as S<N> using that same N, immediately followed
by a timestamp from that session: "S4 [01:09]". The UI turns these into links
that open the replay at that moment, so the format must be exact — "S4 [01:09]",
never "Session 4", "#4", or "S4 at 1:09".

- Use only the N of a write-up actually shown below. Never invent one, never
  cite a number above the group size, and never renumber the sessions.
- The [MM:SS] must be a timestamp that appears in THAT session's write-up.
  Do not carry a timestamp from one session over to another.
- Include the timestamp whenever the claim is about a moment. When it is a
  counting claim and no single moment applies, a bare "S1, S2, S4" is right —
  do not invent a timestamp to decorate it.
- Cite the sessions the claim rests on, in brackets after the point:
  "Four sessions stalled on the guarantee (S1 [00:13], S4 [01:09],
  S6 [00:17], S8 [00:08]) — the promise is doing the selling."
  "4 of 7 kept the default '90-Night Supply' (S1, S2, S4, S7)."
- Citations are evidence, not the finding. They never replace the sentence,
  and a bare list of citations is not an observation.

## Output

Return a single JSON object with this exact shape. Every item in journey /
attention / intent / blockers is an OBJECT with three fields — never a bare string.
"recommendations" and "caveats" are plain string lists.
{
  "headline": "one sentence a marketer could put at the top of a deck: the sharpest finding, with its share and what it means",
  "journey": [
    {
      "observation": "what these visitors did, with the evidence — quoted labels, [MM:SS], durations — and one hedged reading of what it suggests",
      "share": "44% (4 of 9)",
      "action": "what the team should do about it: an offer, a message, a placement, a channel, a test"
    }
  ],
  "attention": [{"observation": "...", "share": "...", "action": "..."}],
  "intent": [{"observation": "...", "share": "...", "action": "..."}],
  "blockers": [{"observation": "...", "share": "...", "action": "..."}],
  "caveats": ["plain strings here — what these sessions cannot tell you, and any single-session oddity worth a look"],
  "recommendations": [
    "concrete next steps drawn from the findings above — offer, message, placement, channel, or test. 3 to 6 items. Shown last in the UI."
  ]
}

What each section is for:
- "journey" — how they moved through the site and where they dropped off.
  Which entry points and routes worked, where each cluster stopped, and what
  that says about the landing experience.
- "attention" — what held them and what lost them. Which sections, claims or
  offers they sat on, and for how long. This is where messaging and creative
  insight lives: a 40s stall on a guarantee tells you what is doing the
  selling.
- "intent" — the moments that show buying intent or hesitation: the options
  they compared, the defaults they changed, the controls they used, what they
  did just before converting or leaving.
- "blockers" — what stopped them: dead clicks, rage clicks, unexpected
  redirects, modals, repeated attempts, confusing steps. Every one a write-up
  mentions belongs here, because each is money left on the table. An empty
  blockers list must mean the write-ups describe none — not that you did not
  look.
- "caveats" — plain strings, no objects. Sample limits and one-off oddities.
- "recommendations" — the marketer's punch list, last. Plain strings only. Every
  item must be a concrete next step (test this default, move this proof
  point up, lead this channel with that message, fix this broken step).
  Pull them from the actions on the findings above; do not invent steps the
  evidence does not support. Deduplicate: one recommendation per decision.
  Never "investigate further" or "monitor this".

Writing the three fields:
- "observation" carries the evidence AND the reading. It must contain at least
  one concrete anchor from the write-ups — a quoted label or heading, an
  [MM:SS], a duration, or a scroll position — followed by what it suggests.
  It must also cite the sessions it rests on, in the S<N> form below.
- "share" is the size of the pattern: "44% (4 of 9)". Below 5 sessions use
  "4 of 9 sessions" with no percentage. When a pattern covers the whole group,
  write "N of N" using this group's actual size, not the 9 in this example.
  For a split, describe the side this item is about.
- "action" is a concrete next step for a marketing team — test this default,
  move this proof point up, lead this channel with that message, fix this
  broken step. Never "investigate further" or "monitor this": if the evidence
  does not support a real recommendation, leave the action as an empty string
  and keep the observation.

Rules:
- Every item's "share" must reflect only the sessions whose write-up actually
  supports the point. Do not round a minority up into "most".
- With fewer than 3 sessions, say the evidence is thin in the observation
  rather than asserting a pattern. Small samples are the normal case here.
- A behaviour visible in only 1 session is an anecdote — put it in caveats
  rather than stating it as a group pattern. A specific, surprising anecdote
  is worth more there than a vague pattern would be anywhere else. Friction is
  the exception: a single dead click or rage click is worth reporting in
  friction on its own. This rule is about isolated sessions only — 2 or more
  sessions behaving the same way is a cluster and belongs in its section with
  a count, however small a share of the group it is.
- Keep observation and interpretation separate, and mark which is which.
  The write-ups describe what was on screen, not where anyone looked, so state
  the OBSERVED part in those terms: "reached", "was shown", "scrolled past",
  "sat 40s on" — never "read", "noticed", or "was interested in" as if it were
  fact. You may then INFER what it suggests, provided the inference is hedged
  and follows the evidence:
    OK      "Sat 40s on the ingredient breakdown (56%) — suggests the
             scientific detail is carrying weight for this segment."
    OK      "...may indicate the price is the sticking point, not the claim."
    NOT OK  "Users read the ingredients and were convinced by the science."
  One hedged reading per item is plenty. An item that is all inference and no
  evidence is worthless — the evidence is what makes the reading credible.
- Aim for 3-8 items in each section, as many as the write-ups genuinely
  support. Prefer more specific items over fewer general ones: two precise
  observations are worth more than one that merges them and loses the detail.
  Never merge two distinct observations into one item to save room.
- Every "observation" must carry at least one [MM:SS] timestamp, a quoted
  label or heading, a duration, or a scroll position. An observation with none
  of these is too vague to keep — cut it and write a sharper one.
- Two different items must not recommend the same action in different words.
  If two observations lead to one action, merge them into a single item.
- A section may be empty only when the write-ups genuinely say nothing about
  it. Empty because you ran out of room is not acceptable.
- No preamble, no markdown — JSON only."""


def _cut_side_text(rows: list, label: str) -> tuple:
    """(text, citations) — the group's write-ups, numbered for citation.

    The 1-based number in each header is what the model cites as S<n>. The
    `citations` map returned alongside is that same numbering as data —
    {"1": session_id, ...} — built in this loop, so the numbers the model was
    shown and the ids the UI resolves cannot drift apart. Callers must store
    what they get back rather than rebuilding it from the row list.
    """
    if not rows:
        return f"### {label}\n(no sessions in this group)\n", {}
    citations = {}
    parts = [f"### {label} — {len(rows)} session(s)\n"]
    for i, row in enumerate(rows, 1):
        citations[str(i)] = row.get("session_id")
        attrs = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
        # Every clusterable field is stated on every session, "unknown"
        # included: a missing label reads as a difference the model would
        # otherwise cluster on by accident.
        bits = [f"outcome: {OUTCOME_LABELS.get(row.get('outcome'), 'unknown')}"]
        bits.append(f"device: {row.get('device') or 'unknown'}")
        bits.append(f"page: {row.get('page') or 'unknown'}")
        for key in ("utm_source", "utm_campaign"):
            bits.append(f"{key}: {attrs.get(key) or 'none'}")
        if attrs.get("fbclid"):
            bits.append("meta click id present")
        text = (row.get("text") or "").strip() or "(no write-up)"
        parts.append(f"--- {label} session {i} ({'; '.join(bits)}) ---\n{text}\n")
    return "\n".join(parts), citations


def _given_facts(spec: dict, n: int) -> str:
    """Spell out what the filter already guarantees, so it is not 'found'.

    `n` is the size of the filtered group. Every premise below states it, so
    the only session count in the model's context is the right one: given a
    bare "every session in this group", the model has been seen to attach the
    cohort-wide total instead ("All 46 sessions landed on /x" for a group of 9).
    """
    lines = [
        f"- This group contains exactly {n} session(s). {n} is the ONLY total "
        f"you may use. Every share you write is out of {n} — never out of any "
        f"larger number, and never out of the whole cohort."
    ]

    if spec.get("outcome"):
        joined = " or ".join(OUTCOME_LABELS.get(v, v) for v in spec["outcome"])
        lines.append(
            f"- All {n} session(s) in this group {joined}. Do not report this, "
            f"or any step it entails (clicking buy, reaching checkout, "
            f"converting), as a finding."
        )
    if spec.get("device"):
        lines.append(
            f"- All {n} session(s) in this group were on "
            f"{' or '.join(spec['device'])}. Do not report the device itself "
            f"as a finding."
        )
    if spec.get("page"):
        lines.append(
            f"- All {n} session(s) in this group landed on {spec['page']}. Do "
            f"not report the landing page itself as a finding."
        )
    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
        if spec.get(key):
            lines.append(
                f"- All {n} session(s) in this group came from "
                f"{key}={spec[key]}. Do not report the traffic source as a "
                f"finding."
            )

    # A filtered field is constant here, so it cannot split the group. Point
    # the clustering at the dimensions that still vary.
    free = [
        name
        for name, key in (
            ("outcome", "outcome"),
            ("device", "device"),
            ("landing page", "page"),
            ("traffic source", "utm_source"),
            ("campaign", "utm_campaign"),
        )
        if not spec.get(key)
    ]
    # len(lines) > 1 means the filter actually pinned a field: the count line
    # is always present and pins nothing on its own.
    if len(lines) > 1 and free:
        lines.append(
            "- These are fixed for every session here, so they cannot separate "
            "one cluster from another. Cluster on " + ", ".join(free) + " and "
            "on what the sessions actually did on the page."
        )

    return "## Already known — never report these as findings\n\n" + "\n".join(lines)


# --- Measured splits -------------------------------------------------------
#
# The model is unreliable at counting across dozens of write-ups: it invents
# plausible shares and, per the note in _given_facts, has attached a
# cohort-wide total to a nine-session group. Rather than instruct against
# that, compute the splits here and hand over exact numbers to quote.
#
# Only pairings a marketing team can act on are crossed. The full product of
# five dimensions is ten pairs, and at cohort sizes of 9-50 most cells land at
# n=1 — precisely the noise the floor below exists to suppress.
CROSS_TAB_PAIRS = (
    ("utm_source", "outcome"),
    ("utm_campaign", "outcome"),
    ("page", "outcome"),
    ("device", "outcome"),
    ("utm_source", "device"),
    ("page", "utm_source"),
)

# A cell must clear all three gates. Count alone lets 3-of-200 through;
# coverage alone lets a large but unremarkable cell through; lift alone lets a
# two-session fluke through.
CROSS_TAB_MIN_CELL = 3
CROSS_TAB_MIN_COVERAGE = 0.20
CROSS_TAB_MIN_LIFT_PTS = 15.0

CROSS_TAB_DIMENSIONS = {
    "outcome": "outcome",
    "device": "device",
    "page": "landing page",
    "utm_source": "traffic source",
    "utm_campaign": "campaign",
}


def _dimension_value(row: dict, dimension: str) -> Optional[str]:
    """One row's value on a cross-tab dimension, or None when unknown.

    Unknown is not a value: a session with no utm_source may be direct,
    organic or untagged, and bucketing those together would invent a segment.
    """
    if dimension in ("utm_source", "utm_campaign"):
        attrs = row.get("attribution")
        attrs = attrs if isinstance(attrs, dict) else {}
        value = attrs.get(dimension)
        if not value and dimension == "utm_source" and attrs.get("fbclid"):
            # An fbclid with no utm_source is still unambiguously Meta.
            return "meta (fbclid)"
        return str(value).strip().lower() or None if value else None
    if dimension == "outcome":
        return OUTCOME_LABELS.get(row.get("outcome") or "rest")
    value = row.get(dimension)
    return str(value).strip() or None if value else None


def _coverage(rows: list, dimension: str) -> tuple:
    """(known_count, total) for a dimension across the group."""
    known = sum(1 for row in rows if _dimension_value(row, dimension))
    return known, len(rows)


def _purchase_rate(rows: list) -> Optional[float]:
    """Share of rows that purchased, as a percentage, or None for no rows."""
    if not rows:
        return None
    bought = sum(1 for row in rows if (row.get("outcome") or "rest") == "purchased")
    return 100.0 * bought / len(rows)


def cross_tab_cells(rows: list, left: str, right: str) -> dict:
    """Measured split of `rows` by `left`, reported against `right`.

    Returns {"baseline", "kept", "suppressed", "coverage", "graded"}. `kept`
    holds only cells clearing every gate; `suppressed` counts what the gates
    removed, so the prompt can say a dimension was examined rather than leave
    the model to read silence as uniformity.

    When every session shares one outcome — a "purchased" cut, say — purchase
    rate is constant and lift is 0 for every cell by construction. Grading on
    it there would suppress the whole block, including splits that are real
    and visible. Such a group is graded on size alone and `graded` says so,
    so the prompt can describe what the numbers mean.
    """
    known, total = _coverage(rows, left)
    outcomes = {(row.get("outcome") or "rest") for row in rows}
    graded = "lift" if len(outcomes) > 1 else "composition"
    out = {
        "left": left,
        "right": right,
        "coverage": (known, total),
        "baseline": _purchase_rate(rows),
        "graded": graded,
        "kept": [],
        "suppressed": 0,
    }
    if not rows or not known:
        return out

    buckets: dict = {}
    for row in rows:
        value = _dimension_value(row, left)
        if value:
            buckets.setdefault(value, []).append(row)

    baseline = out["baseline"]
    for value, bucket in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(bucket)
        coverage = n / total if total else 0.0
        rate = _purchase_rate(bucket)
        lift = None if rate is None or baseline is None else rate - baseline
        too_small = n < CROSS_TAB_MIN_CELL or coverage < CROSS_TAB_MIN_COVERAGE
        # Only grade on lift where outcome actually varies; see the docstring.
        too_flat = graded == "lift" and (
            lift is None or abs(lift) < CROSS_TAB_MIN_LIFT_PTS
        )
        if too_small or too_flat:
            out["suppressed"] += 1
            continue
        breakdown = {}
        for member in bucket:
            key = _dimension_value(member, right) or "unknown"
            breakdown[key] = breakdown.get(key, 0) + 1
        out["kept"].append(
            {
                "value": value,
                "n": n,
                "share": 100.0 * coverage,
                "rate": rate,
                "lift": lift,
                "breakdown": breakdown,
            }
        )
    return out


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def cross_tab_block(rows: list, spec: Optional[dict] = None) -> str:
    """Exact, pre-computed splits for the group — empty when nothing clears.

    Dimensions pinned by the filter are skipped: they are constant across the
    group and cannot separate anything, which is the same reasoning
    _given_facts applies to the premises.
    """
    spec = spec or {}
    if len(rows) < CROSS_TAB_MIN_CELL:
        return ""

    pinned = {key for key in CROSS_TAB_DIMENSIONS if spec.get(key)}
    if spec.get("fbclid") is not None:
        pinned.add("utm_source")

    lines: list = []
    notes: list = []
    for left, right in CROSS_TAB_PAIRS:
        if left in pinned or right in pinned:
            continue
        result = cross_tab_cells(rows, left, right)
        known, total = result["coverage"]
        title = f"{CROSS_TAB_DIMENSIONS[left]} × {CROSS_TAB_DIMENSIONS[right]}"
        if not known:
            notes.append(f"- {title}: no session in this group carries a {left}.")
            continue
        if known < total:
            coverage_note = f" ({known} of {total} sessions carry a {left})"
        else:
            coverage_note = ""
        if not result["kept"]:
            notes.append(
                f"- {title}{coverage_note}: examined, nothing clears the "
                f"reporting floor."
            )
            continue
        baseline = result["baseline"]
        if result["graded"] == "lift":
            lines.append(
                f"\n{title}{coverage_note} — group baseline "
                f"{_pct(baseline)} purchased ({len(rows)} sessions)"
            )
        else:
            # Outcome is fixed by the filter, so a purchase rate here would
            # just restate it. These rows say how the group is made up.
            lines.append(
                f"\n{title}{coverage_note} — how the group breaks down "
                f"({len(rows)} sessions, all the same outcome)"
            )
        for cell in result["kept"]:
            parts = ", ".join(
                f"{k} {v}" for k, v in sorted(cell["breakdown"].items(), key=lambda kv: -kv[1])
            )
            head = (
                f"  {cell['value']}: {cell['n']} sessions "
                f"({_pct(cell['share'])} of the group)"
            )
            if result["graded"] == "lift":
                sign = "+" if cell["lift"] >= 0 else "−"
                head += (
                    f" · {_pct(cell['rate'])} purchased · "
                    f"{sign}{abs(cell['lift']):.0f}pts vs baseline"
                )
            lines.append(f"{head} · {parts}")
        if result["suppressed"]:
            lines.append(
                f"  (below the floor, not shown: {result['suppressed']} more)"
            )

    if not lines and not notes:
        return ""

    outcomes = {(row.get("outcome") or "rest") for row in rows}
    if len(outcomes) > 1:
        criterion = (
            f"Only splits meeting all of: at least {CROSS_TAB_MIN_CELL} "
            f"sessions, at least {_pct(CROSS_TAB_MIN_COVERAGE * 100)} of the "
            f"group, and at least {CROSS_TAB_MIN_LIFT_PTS:.0f} points away "
            "from the group's purchase rate."
        )
    else:
        criterion = (
            "Every session here shares one outcome, so there is no purchase "
            "rate to compare against. These rows show how the group is made "
            f"up: only splits of at least {CROSS_TAB_MIN_CELL} sessions and "
            f"at least {_pct(CROSS_TAB_MIN_COVERAGE * 100)} of the group."
        )
    header = (
        "## Measured splits — computed, not inferred\n\n"
        "These counts were calculated directly from the group's session "
        "labels. They are EXACT. Quote them as given; never recompute, round "
        "differently, or contradict them. Where your reading of the write-ups "
        "disagrees with a number here, the number is right.\n\n"
        f"{criterion} A split absent here is one the data does not support — "
        "do not assert it from the write-ups alone.\n\n"
        "A row below tells you WHAT differs, never WHY. The reason lives in "
        "the write-ups: pair each number with what those sessions actually "
        "did on the page, and never restate a row as a finding on its own."
    )
    body = "\n".join(lines)
    if notes:
        # Named either way: silence would let the model read "no split shown"
        # as "the group is uniform on that dimension".
        body += "\n\nExamined and not reported:\n" + "\n".join(notes)
    return f"{header}\n{body}".strip()


# Insight sections hold {observation, share, action} objects; recommendations
# and caveats stay plain string lists (a punch list / sample limit has no share).
CUT_INSIGHT_SECTIONS = ("journey", "attention", "intent", "blockers")
CUT_SECTIONS = CUT_INSIGHT_SECTIONS + ("caveats",)
# The prompt asks for 3-8 items; keep a little headroom so a good extra item
# is kept rather than silently dropped.
CUT_ITEMS_PER_SECTION = 10
CUT_RECOMMENDATIONS_CAP = 8


def empty_cut_analysis(error: Optional[str] = None) -> dict:
    out = {
        "headline": "",
        "recommendations": [],
        **{key: [] for key in CUT_SECTIONS},
    }
    if error:
        out["error"] = error
    return out


def _clean_text(value, limit: int) -> str:
    """Trimmed string, or "" for anything that is not usable text."""
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else ""


def _insight_item(item) -> Optional[dict]:
    """One {observation, share, action}, or None when there is no observation.

    A bare string is accepted as the observation: the model occasionally falls
    back to the old flat shape, and an item with no action still carries the
    finding. Share and action are optional and render only when present.
    """
    if isinstance(item, str):
        observation = _clean_text(item, 900)
        return {"observation": observation, "share": "", "action": ""} if observation else None
    if not isinstance(item, dict):
        return None
    # Generous limits: a good observation quotes labels and cites timestamps,
    # so it runs long. Truncating it would strip exactly the useful part.
    observation = _clean_text(item.get("observation"), 900)
    if not observation:
        return None
    return {
        "observation": observation,
        "share": _clean_text(item.get("share"), 60),
        "action": _clean_text(item.get("action"), 400),
    }


def _recommendation_line(item) -> str:
    """One punch-list line: a bare string, or the action field of an object."""
    if isinstance(item, str):
        return _clean_text(item, 400)
    if isinstance(item, dict):
        for key in ("action", "recommendation", "text"):
            line = _clean_text(item.get(key), 400)
            if line:
                return line
    return ""


def _recommendations_from_actions(out: dict) -> list:
    """Fallback punch list: unique non-empty actions from the insight items.

    Older cached cuts predate the top-level field; deriving from actions keeps
    the Recommendations block populated without a re-ask.
    """
    seen = set()
    lines = []
    for key in CUT_INSIGHT_SECTIONS:
        for item in out.get(key) or []:
            if not isinstance(item, dict):
                continue
            line = _clean_text(item.get("action"), 400)
            if not line or line.lower() in seen:
                continue
            seen.add(line.lower())
            lines.append(line)
            if len(lines) >= CUT_RECOMMENDATIONS_CAP:
                return lines
    return lines


# "4 of 9", "44% (4 of 9)", "9 of 9 sessions" — the denominator is the group
# size, and the model is told it is the only total it may use.
_SHARE_DENOM_RE = re.compile(r"\b(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)


def _share_denominator_ok(share: str, n: Optional[int]) -> bool:
    """False when a share cites a total that is not the group size.

    The model has been seen to write the cohort-wide total into a share for a
    much smaller filtered group. A share whose denominator is not `n` is not
    describing this group, so it is dropped rather than shown as fact.
    """
    if n is None or not share:
        return True
    found = _SHARE_DENOM_RE.findall(share)
    if not found:
        return True
    # A numerator above the group size is impossible here too.
    return all(int(den) == n and int(num) <= n for num, den in found)


def normalize_cut_analysis(raw, n: Optional[int] = None) -> dict:
    """Coerce the model's JSON into the documented shape, dropping anything else.

    `n` is the group size when known. Shares citing a different total are
    dropped: the observation still carries the finding, and a missing share
    renders as nothing, which beats rendering a wrong one.
    """
    data = raw if isinstance(raw, dict) else {}
    out = empty_cut_analysis()
    out["headline"] = _clean_text(data.get("headline"), 600)
    for key in CUT_SECTIONS:
        items = data.get(key)
        if not isinstance(items, list):
            continue
        clean = []
        for item in items:
            # Caveats stay plain strings; the insight sections are objects.
            entry = _clean_text(item, 900) if key == "caveats" else _insight_item(item)
            if entry:
                if isinstance(entry, dict) and not _share_denominator_ok(
                    entry["share"], n
                ):
                    entry["share"] = ""
                clean.append(entry)
            if len(clean) >= CUT_ITEMS_PER_SECTION:
                break
        out[key] = clean
    recs = data.get("recommendations")
    if isinstance(recs, list):
        clean = []
        seen = set()
        for item in recs:
            line = _recommendation_line(item)
            if not line or line.lower() in seen:
                continue
            seen.add(line.lower())
            clean.append(line)
            if len(clean) >= CUT_RECOMMENDATIONS_CAP:
                break
        out["recommendations"] = clean
    if not out["recommendations"]:
        out["recommendations"] = _recommendations_from_actions(out)
    return out


def cut_context(
    rows: list, label: str, given: str = "", spec: Optional[dict] = None
) -> tuple:
    """(header, side_text, citations) — the evidence a group's questions run on.

    Shared by the cut write-up and the chat, so the two always reason over the
    same numbered write-ups, the same "already known" premises and the same
    measured splits. Anything that changes here changes both, which is the
    point: a chat that saw a different context than the cut it sits under
    would answer questions about a group the user is not looking at.
    """
    header = f"This group is: {label}\n"
    if given:
        header += "\n" + given + "\n"
    measured = cross_tab_block(rows, spec)
    if measured:
        header += "\n" + measured + "\n"
    header += "\nThe write-ups for this group follow."
    side_text, citations = _cut_side_text(rows, "SESSIONS")
    return header, side_text, citations


async def call_gemini_cut(
    rows: list,
    label: str,
    given: str = "",
    spec: Optional[dict] = None,
) -> tuple:
    """(analysis, citations) — one text-only pass over the group's write-ups.

    The citation map comes back from the same call that numbered the write-ups,
    so what the caller stores is exactly what the model was shown.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    header, side_text, citations = cut_context(rows, label, given, spec)
    parts = [
        types.Part.from_text(text=header),
        types.Part.from_text(text=side_text),
    ]
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=CUT_PROMPT,
            # Concrete items carry timestamps, px depths and quoted labels, so
            # they run far longer than abstracted one-liners. Five sections of
            # them did not fit in the old 4096 — the model hit the ceiling and
            # summarised to fit, which is exactly the failure being fixed.
            # Thinking is budgeted separately from the answer, so both grew.
            max_output_tokens=12288,
            thinking_config=types.ThinkingConfig(thinking_budget=4096),
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    analysis = normalize_cut_analysis(_parse_json_object(response.text or ""), len(rows))
    return analysis, citations


async def build_cut_analysis(rows: list, slice_spec: Optional[dict]) -> dict:
    """Filter the cohort to one group and ask the model what it did."""
    if not ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    selected, spec = select_rows_by_cut(rows, slice_spec)
    label = slice_label(spec) if spec else "all sessions"

    base = {
        "cut": cut_key(slice_spec),
        "label": label,
        "n": len(selected),
        "session_ids": [r.get("session_id") for r in selected],
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
    }

    # Nothing to read — say so rather than spending a call on an empty group.
    if not selected:
        empty = empty_cut_analysis()
        empty["headline"] = f"No analyzed sessions match {label} in this cohort."
        return {**base, "analysis": empty, "citations": {}}

    given = _given_facts(spec, len(selected))
    _, model_gate = _gates()
    async with model_gate:
        analysis, citations = await call_gemini_cut(selected, label, given, spec)
    # Stored, not derived: this is the numbering the model actually saw, so a
    # later change to row ordering cannot repoint the links in this cut.
    return {**base, "analysis": analysis, "citations": citations}


CHAT_PROMPT = """You answer a marketer's questions about a group of recorded web
sessions, working only from the per-session write-ups you are given.

Each write-up is one session's analysis, already grounded in that session's
replay. They are the ONLY evidence you have. You cannot see the recordings, the
page, the analytics, or anything outside this group.

## What you can and cannot answer

You know what the write-ups say. You do not know anything else.

When a question asks for something the write-ups do not cover, say so plainly
and distinguish WHICH kind of "no" it is — this matters more than anything else
you will do here, because the two are easy to confuse and mean opposite things:

  "The write-ups don't mention scroll depth"     ← absent from the EVIDENCE
  "None of these sessions scrolled past the fold" ← absent from the SESSIONS

The first means you cannot tell. The second is a finding. Never let the first
read as the second. If a write-up simply does not discuss something, the honest
answer is that it was not recorded in the write-up — not that it did not happen.

You also cannot answer questions about:
- Sessions outside this group. The group is the whole world here.
- Precise metrics — session duration, click counts, scroll pixels, revenue —
  unless a write-up states the number, or the "Measured splits" block below
  gives it. Outside those, you are reading prose, not a database.
- Trends over time, or comparisons to other periods, cohorts or benchmarks.
- What would happen if something changed. You can say what these visitors did;
  you cannot run the experiment.

Say "the write-ups don't tell us" and, where you can, name what would answer it.
A clean "I can't tell you that" beats a confident guess every time.

## Counting

You can see every write-up in the group, so counting claims are exact — make
them. Give the percentage first with the raw count in brackets: "44% (4 of 9)".
Round to whole numbers. Below 5 sessions use "4 of 9 sessions" with no
percentage and say the evidence is directional.

The group size is stated under "Already known" below. It is the ONLY total you
may use. Never cite a denominator that is not the group size.

Some groups also arrive with a "Measured splits" block: counts computed
directly from the session labels rather than read out of the write-ups. Those
numbers are exact and they win. Quote them as given, never contradict one with
a count of your own, and treat a split the block does not list as unsupported
rather than arguing it from the write-ups. The block says WHAT differs; the
write-ups are still the only place that says WHY, so pair any number you quote
from it with what those sessions actually did.

## The filter is not a finding

The group was SELECTED by a filter, stated below as "This group is". Anything
entailed by that filter is already known: if the group is "purchased", that they
bought is the definition, not a discovery. Answer what was actually asked
instead of restating the selection criteria back.

## Citing sessions

Each write-up is headed "--- SESSIONS session N ---". Cite as S<N>, optionally
followed by a timestamp from THAT session's write-up: "S4 [01:09]". The format
must be exact — never "Session 4", "#4", or "S4 at 1:09". The UI turns these
into links that open the replay at that moment.

- Only cite an N that appears below. Never invent one, never exceed the group
  size, never renumber.
- A [MM:SS] must come from that session's own write-up. Do not carry a stamp
  from one session to another.
- Include a stamp when the claim is about a moment. For counting claims a bare
  "S1, S2, S4" is correct — do not invent a timestamp to decorate it.
- Cite the sessions a claim rests on. Citations are evidence, not the answer:
  a bare list of ordinals is not a response.

## How to answer

Answer the question that was asked, at the length it deserves — a counting
question takes a sentence, an open-ended one may take several paragraphs. No
preamble, no restating the question, no closing offer of further help.

Write for a marketer: what these visitors did, what it suggests, and where it
points. Lead with the answer, then the evidence.

Separate what you observed from what you infer. The write-ups describe what was
on screen, not where anyone looked, so say "reached", "was shown", "scrolled
past", "sat 40s on" — never "read" or "noticed" as if it were fact. You may
then infer, hedged: "suggests", "may indicate", "appears to". One reading per
claim is plenty; an answer that is all inference and no evidence is worthless.

Markdown is fine — short paragraphs, bullets where they help. No headings
unless the answer genuinely has sections."""

# A turn is one call, so history is the only thing that grows. Cap it: a long
# thread would otherwise re-send the whole write-up block plus an unbounded
# transcript on every question.
CHAT_MAX_TURNS = 12
CHAT_MAX_MESSAGE_CHARS = 4000


def clean_chat_messages(messages) -> list:
    """Trim history to the recent turns, as {role, content} with role user/model.

    The client owns the transcript, so this is untrusted input: anything that is
    not a usable turn is dropped rather than forwarded to the model.
    """
    if not isinstance(messages, list):
        return []
    clean = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        # The UI speaks in assistant/user; Gemini wants model/user.
        if role in ("assistant", "model"):
            role = "model"
        elif role != "user":
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        clean.append({"role": role, "content": content.strip()[:CHAT_MAX_MESSAGE_CHARS]})
    # Keep the most recent turns; the question is always the last one.
    return clean[-CHAT_MAX_TURNS:]


async def stream_gemini_chat(
    rows: list, label: str, given: str, messages: list, spec: Optional[dict] = None
):
    """Yield answer text chunks for the latest question in `messages`.

    The evidence goes in as the first user turn with a canned acknowledgement
    after it, so the conversation that follows is ordinary chat over a context
    the model has already "read". That keeps the write-up block a stable prefix
    across turns, which is what makes implicit caching possible.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    header, side_text, _ = cut_context(rows, label, given, spec)
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=header),
                types.Part.from_text(text=side_text),
            ],
        ),
        types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=(
                        f"I have read the {len(rows)} write-up(s) for this group. "
                        "Ask me anything about them."
                    )
                )
            ],
        ),
    ]
    for message in messages:
        contents.append(
            types.Content(
                role=message["role"],
                parts=[types.Part.from_text(text=message["content"])],
            )
        )

    stream = await client.aio.models.generate_content_stream(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=CHAT_PROMPT,
            # Answers are prose, not five sections of insight objects, so this
            # sits well below the cut's ceiling. Thinking still gets room to
            # count across the write-ups before answering.
            max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
            temperature=0.2,
        ),
    )
    async for chunk in stream:
        text = getattr(chunk, "text", None)
        if text:
            yield text


async def chat_context(rows: list, slice_spec: Optional[dict]) -> tuple:
    """(selected_rows, label, given, citations, spec) for a group.

    Mirrors build_cut_analysis's setup so a chat answers about exactly the group
    the cut above it describes. `spec` comes back so the caller can hand it to
    stream_gemini_chat: without it the chat would rebuild its context without
    the measured splits and answer from a thinner view than the cut on screen.
    """
    if not ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")
    selected, spec = select_rows_by_cut(rows, slice_spec)
    label = slice_label(spec) if spec else "all sessions"
    given = _given_facts(spec, len(selected))
    _, _, citations = cut_context(selected, label, given, spec)
    return selected, label, given, citations, spec


def _parse_index_json(raw) -> Optional[dict]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def index_row_facts(
    url,
    order_raw,
    device_raw,
    attribution_raw,
) -> dict:
    """Slice fields for one index row — same vocabulary as cut matching."""
    order = _parse_index_json(order_raw)
    device = _parse_index_json(device_raw)
    attribution = _parse_index_json(attribution_raw) or {}
    return {
        "outcome": outcome_class(order),
        "device": device_class(device),
        "page": page_path(url),
        "attribution": attribution,
        "bought": bool(order and order.get("purchased_at")),
    }


def _facet_counts_listed(counts: dict, field: str, order=None) -> list:
    items = counts[field]
    keys = order if order else sorted(items, key=lambda k: (-items[k], k))
    return [
        {
            "value": k,
            "count": items[k],
            "label": OUTCOME_LABELS.get(k, k) if field == "outcome" else k,
        }
        for k in keys
        if items.get(k)
    ]


def facets_from_rows(rows: list) -> dict:
    """Build the cut-picker facet payload from slice-shaped rows."""
    counts = {
        "outcome": {},
        "device": {},
        "page": {},
        "utm_source": {},
        "utm_campaign": {},
    }
    for row in rows:
        outcome = row.get("outcome") or "rest"
        counts["outcome"][outcome] = counts["outcome"].get(outcome, 0) + 1
        if row.get("device"):
            counts["device"][row["device"]] = counts["device"].get(row["device"], 0) + 1
        if row.get("page"):
            counts["page"][row["page"]] = counts["page"].get(row["page"], 0) + 1
        attrs = row.get("attribution") or {}
        for key in ("utm_source", "utm_campaign"):
            value = str(attrs.get(key) or "").strip().lower()
            if value:
                counts[key][value] = counts[key].get(value, 0) + 1

    return {
        "n": len(rows),
        "outcome": _facet_counts_listed(counts, "outcome", order=OUTCOME_VALUES),
        "device": _facet_counts_listed(counts, "device", order=DEVICE_VALUES),
        "page": _facet_counts_listed(counts, "page"),
        "utm_source": _facet_counts_listed(counts, "utm_source"),
        "utm_campaign": _facet_counts_listed(counts, "utm_campaign"),
    }


def pool_facets(
    conn: sqlite3.Connection,
    pool: str,
    *,
    min_duration_ms: int = 0,
) -> dict:
    """Facet values present in the pool index (for the New cohort dialog)."""
    where_sql, params = _cohort_where(pool, min_duration_ms=min_duration_ms, url_contains="")
    rows = conn.execute(
        f"""
        SELECT url, "order", device, attribution
        FROM sessions
        WHERE {where_sql}
        """,
        params,
    ).fetchall()
    facts = [index_row_facts(url, order, device, attr) for url, order, device, attr in rows]
    return facets_from_rows(facts)


def _cohort_where(
    pool: str,
    *,
    min_duration_ms: int,
    url_contains: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Tuple[str, List]:
    """Shared WHERE clause for cohort sampling.

    Time bounds use `last_seen` so a window means “sessions that were active
    (or finished) in this period,” matching the recency sort key.
    """
    where = [
        "pool = ?",
        "chunk_count > 0",
        "(last_seen - first_seen) >= ?",
    ]
    params: List = [pool, int(min_duration_ms)]
    needle = (url_contains or "").strip()
    if needle:
        where.append("url LIKE ?")
        params.append(f"%{needle}%")
    if start_ms is not None:
        where.append("last_seen >= ?")
        params.append(int(start_ms))
    if end_ms is not None:
        where.append("last_seen <= ?")
        params.append(int(end_ms))
    return " AND ".join(where), params


def _cohort_order_sql(order: str) -> str:
    activity = "coalesce(json_extract(interactions, '$.clicks'), 0)"
    if order == COHORT_ORDER_EARLIEST:
        return f"last_seen ASC, {activity} DESC, session_id ASC"
    return f"last_seen DESC, {activity} DESC, session_id DESC"


def _sample_candidates(
    conn: sqlite3.Connection,
    pool: str,
    *,
    min_duration_ms: int,
    url_contains: str,
    limit: int,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = COHORT_ORDER_LATEST,
) -> list:
    """Index rows (with slice facts) that clear the base duration/path/time gates."""
    where_sql, base_params = _cohort_where(
        pool,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    order_sql = _cohort_order_sql(order if order in COHORT_ORDERS else COHORT_ORDER_LATEST)
    rows = conn.execute(
        f"""
        SELECT session_id, url, "order", device, attribution
        FROM sessions
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT ?
        """,
        (*base_params, max(0, int(limit))),
    ).fetchall()
    out = []
    for sid, url, order_raw, device, attr in rows:
        fact = index_row_facts(url, order_raw, device, attr)
        fact["session_id"] = sid
        out.append(fact)
    return out


def _sample_matching_rows(
    conn: sqlite3.Connection,
    pool: str,
    n: int,
    *,
    min_duration_ms: int,
    url_contains: str,
    slice_spec: Optional[dict],
    exclude_ids: Optional[set] = None,
    tag: str = "a",
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = COHORT_ORDER_LATEST,
) -> list:
    """Sessions matching a cut-style slice, tagged for the sample arm."""
    if n <= 0:
        return []
    spec = normalize_slice(slice_spec)
    exclude = exclude_ids or set()
    candidate_limit = min(
        COHORT_CANDIDATE_CAP,
        max(n * 20, n + len(exclude) + 200, 500),
    )
    path_needle = "" if spec.get("page") else url_contains
    candidates = _sample_candidates(
        conn,
        pool,
        min_duration_ms=min_duration_ms,
        url_contains=path_needle,
        limit=candidate_limit,
        start_ms=start_ms,
        end_ms=end_ms,
        order=order,
    )
    out = []
    for row in candidates:
        sid = row["session_id"]
        if sid in exclude:
            continue
        if not row_matches_slice(row, spec):
            continue
        out.append({"session_id": sid, "filter": tag, "status": "queued"})
        if len(out) >= n:
            break
    return out


def sample_cohort_rows(
    conn: sqlite3.Connection,
    pool: str,
    purchased_n: int,
    all_n: int,
    *,
    any_n: int = 0,
    a_n: int = 0,
    b_n: int = 0,
    min_duration_ms: int = COHORT_MIN_DURATION_MS,
    url_contains: str = COHORT_URL_CONTAINS,
    slice_spec: Optional[dict] = None,
    slice_b: Optional[dict] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = COHORT_ORDER_LATEST,
) -> list:
    """Sessions that match the filters and optional cut-style slice.

    Preferred: `a_n` (+ optional `b_n`) with independent slices for groups A/B.
    Legacy: `any_n` for one mixed pool, or `purchased_n` + `all_n` for a
    purchase vs non-purchase split (still tagged purchased/all).

    `order` is latest (default) or earliest by `last_seen`. Optional `start_ms` /
    `end_ms` bound that same clock. Prefers sessions with more clicks when
    timestamps tie.
    """
    sort = order if order in COHORT_ORDERS else COHORT_ORDER_LATEST
    sample_kw = dict(
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        start_ms=start_ms,
        end_ms=end_ms,
        order=sort,
    )
    if a_n > 0 or b_n > 0:
        sessions = _sample_matching_rows(
            conn,
            pool,
            a_n,
            slice_spec=slice_spec,
            tag="a",
            **sample_kw,
        )
        if b_n > 0:
            sessions.extend(
                _sample_matching_rows(
                    conn,
                    pool,
                    b_n,
                    slice_spec=slice_b if slice_b is not None else slice_spec,
                    exclude_ids={s["session_id"] for s in sessions},
                    tag="b",
                    **sample_kw,
                )
            )
        return sessions

    spec = normalize_slice(slice_spec)
    filter_spec = dict(spec)
    if any_n <= 0:
        filter_spec.pop("outcome", None)

    needed = any_n if any_n > 0 else purchased_n + all_n
    candidate_limit = min(
        COHORT_CANDIDATE_CAP,
        max(needed * 20, needed + 200, 500),
    )
    path_needle = "" if filter_spec.get("page") else url_contains
    candidates = _sample_candidates(
        conn,
        pool,
        min_duration_ms=min_duration_ms,
        url_contains=path_needle,
        limit=candidate_limit,
        start_ms=start_ms,
        end_ms=end_ms,
        order=sort,
    )
    matched = [row for row in candidates if row_matches_slice(row, filter_spec)]

    def _entry(row: dict) -> dict:
        return {
            "session_id": row["session_id"],
            "filter": "purchased" if row.get("bought") else "all",
            "status": "queued",
        }

    if any_n > 0:
        return [_entry(row) for row in matched[:any_n]]

    purchased = []
    non_purchase = []
    for row in matched:
        if row.get("bought"):
            if len(purchased) < purchased_n:
                purchased.append(_entry(row))
        elif len(non_purchase) < all_n:
            non_purchase.append(_entry(row))
        if len(purchased) >= purchased_n and len(non_purchase) >= all_n:
            break
    return purchased + non_purchase


def _slice_scope_text(spec: dict, url_contains: str) -> str:
    parts = []
    if spec:
        named = slice_label(spec)
        if named and named not in ("everyone else", "all sessions"):
            parts.append(named)
    needle = (url_contains or "").strip()
    if needle and not (spec or {}).get("page"):
        parts.append(needle)
    return ", ".join(parts) if parts else "any page"


def _format_cohort_clock(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    return time.strftime("%b %-d %H:%M", time.localtime(int(ms) / 1000))


def _time_window_text(start_ms: Optional[int], end_ms: Optional[int]) -> str:
    if start_ms is None and end_ms is None:
        return ""
    if start_ms is not None and end_ms is not None:
        return f" between {_format_cohort_clock(start_ms)} and {_format_cohort_clock(end_ms)}"
    if start_ms is not None:
        return f" after {_format_cohort_clock(start_ms)}"
    return f" before {_format_cohort_clock(end_ms)}"


def cohort_selection_text(
    purchased_n: int,
    all_n: int,
    *,
    any_n: int = 0,
    a_n: int = 0,
    b_n: int = 0,
    min_duration_ms: int = COHORT_MIN_DURATION_MS,
    url_contains: str = COHORT_URL_CONTAINS,
    slice_spec: Optional[dict] = None,
    slice_b: Optional[dict] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = COHORT_ORDER_LATEST,
) -> str:
    """Human-readable sampling rule for the Insights UI."""
    spec = normalize_slice(slice_spec)
    spec_b = normalize_slice(slice_b)
    min_s = max(0, int(min_duration_ms)) / 1000
    min_label = f"{min_s:g}s" if min_s != int(min_s) else f"{int(min_s)}s"
    sort = order if order in COHORT_ORDERS else COHORT_ORDER_LATEST
    rank = "latest" if sort == COHORT_ORDER_LATEST else "earliest"
    window = _time_window_text(start_ms, end_ms)

    if a_n > 0 or b_n > 0:
        a_scope = _slice_scope_text(spec, url_contains)
        if b_n > 0:
            b_scope = _slice_scope_text(spec_b, url_contains)
            who = f"{rank} {a_n} for A ({a_scope}) + {b_n} for B ({b_scope})"
        else:
            who = f"{rank} {a_n} for A ({a_scope})"
        return (
            f"Chosen as the {who} sessions{window} with duration ≥{min_label} "
            f"(last_seen − first_seen), preferring sessions with more clicks when "
            f"timestamps tie."
        )

    if any_n > 0:
        who = f"{rank} {any_n} matching"
    else:
        who = f"{rank} {purchased_n} purchased + {all_n} non-purchase"
    scope = _slice_scope_text(spec, url_contains)
    return (
        f"Chosen as the {who} sessions on {scope}{window} with duration ≥{min_label} "
        f"(last_seen − first_seen), preferring sessions with more clicks when "
        f"timestamps tie."
    )


def _slice_label_bits(spec: Optional[dict], url_contains: str = "") -> list:
    """Compact filter tokens for cohort display names."""
    bits = []
    clean = normalize_slice(spec)
    outcome = clean.get("outcome") or []
    if outcome:
        keys = set(outcome)
        if keys == {"purchased", "checkout"}:
            bits.append("reached checkout")
        elif keys == {"checkout", "rest"}:
            bits.append("non-purchase")
        else:
            bits.append(" or ".join(OUTCOME_LABELS.get(o, o) for o in outcome))
    for field in ("device", "page", "utm_source", "utm_campaign"):
        value = clean.get(field)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            bits.append(" or ".join(str(v) for v in value))
        else:
            bits.append(str(value))
    if not clean.get("page"):
        needle = (url_contains or "").strip()
        if needle:
            bits.append(needle)
    return bits


def _auto_cohort_label(
    *,
    stamp: str,
    purchased_n: int,
    all_n: int,
    any_n: int,
    a_n: int,
    b_n: int,
    url_contains: str,
    spec: dict,
    spec_b: dict,
) -> str:
    """Date/time first, then whatever filters were set."""
    parts = [stamp]
    if a_n > 0 or b_n > 0:
        a_bits = _slice_label_bits(spec, url_contains)
        if b_n > 0:
            b_bits = _slice_label_bits(spec_b, url_contains)
            if a_bits or b_bits:
                parts.append(
                    f"A {' '.join(a_bits) if a_bits else 'any'} / "
                    f"B {' '.join(b_bits) if b_bits else 'any'}"
                )
        else:
            parts.extend(a_bits)
    elif any_n > 0 or purchased_n or all_n:
        parts.extend(_slice_label_bits(spec, url_contains))
    return " · ".join(parts)[:120]


def clean_cohort_label(label: Optional[str]) -> str:
    """Trim and cap a cohort display name. Empty string means “use auto label”."""
    return str(label or "").strip()[:120]


def build_cohort(
    conn: sqlite3.Connection,
    pool_key: str,
    purchased_n: int = COHORT_DEFAULT_PURCHASED_N,
    all_n: int = COHORT_DEFAULT_ALL_N,
    *,
    any_n: int = 0,
    a_n: int = 0,
    b_n: int = 0,
    min_duration_ms: int = COHORT_MIN_DURATION_MS,
    url_contains: str = COHORT_URL_CONTAINS,
    slice_spec: Optional[dict] = None,
    slice_b: Optional[dict] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = COHORT_ORDER_LATEST,
    label: Optional[str] = None,
) -> dict:
    now = int(time.time() * 1000)
    spec = normalize_slice(slice_spec)
    spec_b = normalize_slice(slice_b)
    sort = order if order in COHORT_ORDERS else COHORT_ORDER_LATEST
    sessions = sample_cohort_rows(
        conn,
        pool_key,
        purchased_n,
        all_n,
        any_n=any_n,
        a_n=a_n,
        b_n=b_n,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        slice_spec=spec,
        slice_b=spec_b,
        start_ms=start_ms,
        end_ms=end_ms,
        order=sort,
    )
    stamp = time.strftime("%b %-d, %-I:%M %p", time.localtime(now / 1000))
    selection = cohort_selection_text(
        purchased_n,
        all_n,
        any_n=any_n,
        a_n=a_n,
        b_n=b_n,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
        slice_spec=spec,
        slice_b=spec_b,
        start_ms=start_ms,
        end_ms=end_ms,
        order=sort,
    )
    auto_label = _auto_cohort_label(
        stamp=stamp,
        purchased_n=purchased_n,
        all_n=all_n,
        any_n=any_n,
        a_n=a_n,
        b_n=b_n,
        url_contains=url_contains,
        spec=spec,
        spec_b=spec_b,
    )
    chosen = clean_cohort_label(label) or auto_label
    return {
        "id": f"cohort-{now}",
        # Chosen name, or auto label when none was provided.
        "label": chosen,
        "selection": selection,
        "created_at": now,
        "updated_at": now,
        # Sampled only — analysis starts via POST .../analyze.
        "status": "idle",
        "purchased_n": int(purchased_n) if any_n <= 0 and a_n <= 0 else 0,
        "all_n": int(all_n) if any_n <= 0 and a_n <= 0 else 0,
        "any_n": int(any_n),
        "a_n": int(a_n),
        "b_n": int(b_n),
        "min_duration_ms": int(min_duration_ms),
        "url_contains": (url_contains or "").strip(),
        "start_ms": int(start_ms) if start_ms is not None else None,
        "end_ms": int(end_ms) if end_ms is not None else None,
        "order": sort,
        "slice": spec,
        "slice_b": spec_b if b_n > 0 else {},
        "sessions": sessions,
    }


def rename_cohort(pool: Path, cohort_id: str, label: str) -> Optional[dict]:
    """Set a cohort's display label. Membership is untouched."""
    cohort = read_cohort(pool, cohort_id)
    if not cohort:
        return None
    clean = clean_cohort_label(label)
    if not clean:
        raise ValueError("label cannot be empty")
    return write_cohort(pool, {**cohort, "label": clean})


def delete_cohort(pool: Path, cohort_id: str) -> bool:
    """Remove a cohort and its cached cuts. Session write-ups on disk stay."""
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return False
    conn, key = cohort_store(pool)
    return analyze_store.delete_cohort(conn, key, cohort_id)


async def run_extractor(session_dir: Path, out_dir: Path, chunk_names: list) -> dict:
    if not shutil.which("node"):
        raise RuntimeError("node is not installed on this server — analysis is unavailable")
    if not REPLAYER_BUNDLE.is_file():
        raise RuntimeError("replayer bundle missing — run `npm run build`")

    proc = await asyncio.create_subprocess_exec(
        "node",
        str(EXTRACTOR),
        str(session_dir),
        str(out_dir),
        str(REPLAYER_BUNDLE),
        # Positional, and ahead of the variadic chunk names.
        str(ANALYZE_FPS),
        str(ANALYZE_MAX_FRAMES),
        *chunk_names,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(BASE_DIR),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), ANALYZE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("page reconstruction timed out")

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"page reconstruction failed: {detail[-1] if detail else 'unknown error'}")

    try:
        return json.loads(stdout.decode("utf-8"))
    except Exception:
        raise RuntimeError("page reconstruction returned no digest")


def _seconds(ms) -> float:
    return round((ms or 0) / 1000, 1)


def _clock(ms) -> str:
    """MM:SS, the same vocabulary the frame labels use."""
    total = int((ms or 0) // 1000)
    return f"{total // 60:02d}:{total % 60:02d}"


# Kept in step with server.parse_attribution, which does the same job for the
# index. analyze.py cannot import server.py (server imports analyze), so the
# two are deliberate twins — change both together.
ATTRIBUTION_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _session_attribution(url: Optional[str]) -> dict:
    """utm_* / fbclid off an entry URL."""
    if not url:
        return {}
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return {}
    params = {}
    for key, values in urllib.parse.parse_qs(
        parsed.query, keep_blank_values=False
    ).items():
        if values and values[0]:
            params[key] = values[0]
    # Some ad platforms hand back "#utm_source=..." rather than a query string.
    fragment = parsed.fragment or ""
    if fragment and re.match(r"^[^/]+=", fragment):
        for key, values in urllib.parse.parse_qs(
            fragment, keep_blank_values=False
        ).items():
            if key not in params and values and values[0]:
                params[key] = values[0]
    out = {}
    for key in ATTRIBUTION_PARAMS:
        if params.get(key):
            out[key] = str(params[key])[:256]
    if params.get("fbclid"):
        out["fbclid"] = True
    return out


def session_metadata(session_dir: Path) -> dict:
    """How this visitor arrived and how the session ended.

    The digest describes what happened on screen and says nothing about where
    the visitor came from, so a write-up built from it alone cannot tie
    behaviour back to the campaign that bought the click. `visitor.json` keeps
    the entry URL with its query string intact — the only place the UTMs
    survive, since page_path() drops them and the digest's `urls` are
    post-navigation.

    Only fields actually present are returned: a key missing here means
    unknown, and the prompt says so, rather than the model reading an empty
    string as evidence of direct traffic.
    """
    meta: dict = {}

    try:
        visitor = json.loads((session_dir / "visitor.json").read_text())
    except Exception:
        visitor = None
    if isinstance(visitor, dict):
        entry = visitor.get("url")
        if entry:
            meta["entry_url"] = str(entry)[:512]
            landing = page_path(entry)
            if landing:
                meta["landing_page"] = landing
            attribution = _session_attribution(meta["entry_url"])
            if attribution:
                meta["attribution"] = attribution

    try:
        order = json.loads((session_dir / "order.json").read_text())
    except Exception:
        order = None
    order = order if isinstance(order, dict) else None
    meta["outcome"] = OUTCOME_LABELS[outcome_class(order)]
    if order:
        for key in ("order_value", "currency", "products"):
            if order.get(key) not in (None, "", [], {}):
                meta[key] = order[key]
    return meta


def prompt_digest(digest: dict) -> dict:
    """Trim the digest to what the model needs, in seconds rather than ms."""
    session = digest.get("session") or {}
    frames = digest.get("frames") or []
    return {
        "session": {
            "duration_s": _seconds(session.get("durationMs")),
            "active_s": _seconds(session.get("activeMs")),
            "idle_s": _seconds(session.get("idleMs")),
            "viewport": session.get("viewport"),
            "page_height_px": session.get("pageHeight"),
            "max_scroll_px": session.get("maxScrollPx"),
            "pages": session.get("urls") or [],
        },
        "clicks_in_order": [
            {
                "at": _clock(c.get("atMs")),
                "at_s": _seconds(c.get("atMs")),
                "label": c.get("text") or None,
                "element": c.get("tag") or None,
                "selector": c.get("selector") or None,
                "image_alt": (c.get("image") or {}).get("alt"),
                "rage_click": c.get("rage", False),
            }
            for c in (digest.get("clicks") or [])
        ],
        "frames_at": [_clock(f.get("atMs")) for f in frames],
        "warnings": digest.get("warnings") or [],
    }


def session_chunk_names(session_dir: Path) -> list:
    if not session_dir.is_dir():
        return []
    return sorted(
        c.name for c in session_dir.iterdir() if c.is_file() and SAFE_CHUNK.match(c.name)
    )


def _usable_frames(frames: list) -> list:
    return [f for f in frames if Path(f.get("file") or "").is_file()]


def _session_model_parts(digest: dict, frames: list, meta: Optional[dict] = None):
    from google.genai import types

    usable = _usable_frames(frames)
    parts = []
    if meta:
        parts.append(
            types.Part.from_text(
                text="Session metadata — how this visitor arrived and how the "
                "session ended. Any field not listed here is UNKNOWN: say so "
                "rather than guessing, and never treat a missing campaign as "
                "proof the visit was direct.\n\n" + json.dumps(meta, indent=2)
            )
        )
    parts += [
        types.Part.from_text(
            text="Measured digest for this session:\n\n"
            + json.dumps(prompt_digest(digest), indent=2)
        ),
        types.Part.from_text(
            text=f"\n{len(usable)} frames follow in chronological order, sampled "
            "through the active parts of the session. Each is labelled with its "
            "timestamp in the recording."
        ),
    ]
    # Label before the image: the model attributes a caption to what follows it
    # far more reliably than to what precedes it.
    for frame in usable:
        parts.append(types.Part.from_text(text=f"[{_clock(frame['atMs'])}]"))
        parts.append(
            types.Part.from_bytes(
                data=Path(frame["file"]).read_bytes(), mime_type="image/jpeg"
            )
        )
    return parts


def _session_model_config():
    from google.genai import types

    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        # max_output_tokens covers thinking as well as the answer, and a
        # long frame sequence makes the model think hard: at 1400 with no
        # budget, thinking ate all but ~50 tokens and the analysis came
        # back cut off mid-sentence.
        max_output_tokens=4000,
        thinking_config=types.ThinkingConfig(thinking_budget=1024),
        temperature=0.3,
    )


def build_inlined_session_request(
    session_id: str, digest: dict, frames: list, meta: Optional[dict] = None
):
    """One Batch API request for a session write-up, keyed by session_id."""
    from google.genai import types

    return types.InlinedRequest(
        contents=[
            types.Content(role="user", parts=_session_model_parts(digest, frames, meta))
        ],
        metadata={"session_id": session_id},
        config=_session_model_config(),
    )


def write_analysis_result(
    session_dir: Path,
    text: str,
    *,
    frame_count: int = 0,
    warnings: Optional[list] = None,
    model: Optional[str] = None,
) -> dict:
    result = {
        "status": "done",
        "text": text,
        "model": model or GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
        "frame_count": frame_count,
        "warnings": warnings or [],
    }
    write_analysis(session_dir, result)
    clear_job(session_dir)
    return result


async def call_gemini(digest: dict, frames: list, meta: Optional[dict] = None) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Content(role="user", parts=_session_model_parts(digest, frames, meta))
        ],
        config=_session_model_config(),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty analysis")
    return text


def _estimate_inlined_bytes(requests: list) -> int:
    total = 0
    for req in requests:
        dumped = req.model_dump(mode="json", exclude_none=True)
        total += len(json.dumps(dumped))
    return total


async def submit_gemini_batch(requests: list, *, display_name: str) -> str:
    """Create a Batch job from already-built InlinedRequest objects.

    Prefer submit_prepared_gemini_batch for cohorts: building every request up
    front keeps all frame bytes in RAM and OOM'd a 2GB box at n=100.
    """
    from google import genai
    from google.genai import types

    if not requests:
        raise ValueError("batch requires at least one request")

    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.CreateBatchJobConfig(display_name=display_name[:128])
    size = _estimate_inlined_bytes(requests)
    if size <= ANALYZE_BATCH_INLINE_MAX_BYTES:
        job = await client.aio.batches.create(
            model=GEMINI_MODEL, src=requests, config=config
        )
    else:
        # JSONL + Files API — each line is {key, request} for result matching.
        # Write on disk (not tmpfs /tmp) so the payload does not compete for RAM.
        tmp = _batch_jsonl_path()
        try:
            with tmp.open("w") as out:
                for req in requests:
                    payload = req.model_dump(mode="json", exclude_none=True)
                    key = (payload.pop("metadata", None) or {}).get("session_id") or f"r{out.tell()}"
                    out.write(
                        json.dumps({"key": key, "request": payload}, separators=(",", ":"))
                    )
                    out.write("\n")
            uploaded = await client.aio.files.upload(
                file=str(tmp),
                config=types.UploadFileConfig(mime_type="jsonl"),
            )
        finally:
            tmp.unlink(missing_ok=True)
        job = await client.aio.batches.create(
            model=GEMINI_MODEL,
            src=types.BatchJobSource(file_name=uploaded.name),
            config=config,
        )
    if not job.name:
        raise RuntimeError("Gemini batch create returned no job name")
    return job.name


def _batch_jsonl_path(work_dir: Optional[Path] = None) -> Path:
    """Disk-backed temp path for batch JSONL (avoid tmpfs /tmp on small boxes)."""
    base = work_dir if work_dir is not None else Path("/var/tmp")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"capture-batch-{int(time.time() * 1000)}.jsonl"


async def submit_prepared_gemini_batch(
    prepared: list,
    *,
    display_name: str,
    work_dir: Optional[Path] = None,
    pool: Optional[Path] = None,
) -> str:
    """Create a Batch job from (entry, digest) pairs, one session at a time.

    Streams JSONL to disk so peak RAM is roughly one session's frames, not the
    whole cohort. Always uses the Files API — at cohort scale the inline path
    is both oversized and an OOM hazard while estimating size.

    `pool` is what lets each request carry its session's campaign metadata; a
    batch submitted without it still works, minus that context.
    """
    from google import genai
    from google.genai import types

    if not prepared:
        raise ValueError("batch requires at least one request")

    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.CreateBatchJobConfig(display_name=display_name[:128])
    tmp = _batch_jsonl_path(work_dir)
    try:
        with tmp.open("w") as out:
            for entry, digest in prepared:
                sid = entry["session_id"]
                meta = session_metadata(pool / sid) if pool is not None else None
                req = build_inlined_session_request(
                    sid, digest, digest.get("frames") or [], meta
                )
                payload = req.model_dump(mode="json", exclude_none=True)
                del req
                key = (payload.pop("metadata", None) or {}).get("session_id") or sid
                out.write(
                    json.dumps({"key": key, "request": payload}, separators=(",", ":"))
                )
                out.write("\n")
                del payload
        uploaded = await client.aio.files.upload(
            file=str(tmp),
            config=types.UploadFileConfig(mime_type="jsonl"),
        )
    finally:
        tmp.unlink(missing_ok=True)

    job = await client.aio.batches.create(
        model=GEMINI_MODEL,
        src=types.BatchJobSource(file_name=uploaded.name),
        config=config,
    )
    if not job.name:
        raise RuntimeError("Gemini batch create returned no job name")
    return job.name


def _batch_state_name(job) -> str:
    state = getattr(job, "state", None)
    if state is None:
        return ""
    return getattr(state, "name", None) or str(state)


def _batch_response_text(response) -> str:
    text = (getattr(response, "text", None) or "").strip()
    if text:
        return text
    # Some SDK shapes only expose candidates/parts.
    try:
        candidates = getattr(response, "candidates", None) or []
        parts = (candidates[0].content.parts if candidates else []) or []
        chunks = [getattr(p, "text", "") for p in parts if getattr(p, "text", None)]
        return "\n".join(chunks).strip()
    except Exception:
        return ""


def parse_batch_results(job) -> dict:
    """Map session_id → {"text": str} or {"error": str} from a finished batch job."""
    out: dict = {}
    dest = getattr(job, "dest", None)
    inlined = getattr(dest, "inlined_responses", None) if dest else None
    if inlined:
        for item in inlined:
            meta = getattr(item, "metadata", None) or {}
            sid = meta.get("session_id") if isinstance(meta, dict) else None
            if not sid:
                continue
            err = getattr(item, "error", None)
            if err:
                out[sid] = {
                    "error": getattr(err, "message", None) or str(err) or "batch request failed"
                }
                continue
            text = _batch_response_text(getattr(item, "response", None))
            if text:
                out[sid] = {"text": text}
            else:
                out[sid] = {"error": "Gemini returned an empty analysis"}
        return out

    # File-output jobs: download JSONL and match on `key`.
    file_name = getattr(dest, "file_name", None) if dest else None
    if not file_name:
        return out
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    raw = client.files.download(file=file_name)
    if isinstance(raw, bytes):
        content = raw.decode("utf-8", "replace")
    else:
        content = str(raw)
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        sid = row.get("key")
        if not sid:
            continue
        if row.get("error"):
            err = row["error"]
            out[sid] = {
                "error": err.get("message") if isinstance(err, dict) else str(err)
            }
            continue
        response = row.get("response") or {}
        text = ""
        if isinstance(response, dict):
            text = (response.get("text") or "").strip()
            if not text:
                try:
                    parts = response["candidates"][0]["content"]["parts"]
                    text = "\n".join(
                        p.get("text", "") for p in parts if isinstance(p, dict)
                    ).strip()
                except Exception:
                    text = ""
        if text:
            out[sid] = {"text": text}
        else:
            out[sid] = {"error": "Gemini returned an empty analysis"}
    return out


class BatchStillRunning(RuntimeError):
    """Gave up watching a batch that is, as far as we know, still alive.

    Distinct from a batch that failed: the job name remains valid and its
    results are still coming, so the caller must keep the name on disk and let
    a resume collect them. Re-running the sessions instead would pay for the
    same work twice.
    """


async def wait_gemini_batch(
    job_name: str,
    *,
    poll_s: Optional[float] = None,
    max_wait_s: Optional[float] = None,
):
    """Poll until the batch reaches a terminal state; return the final job.

    Raises BatchStillRunning if the deadline passes or polling keeps failing —
    in both cases the job itself is untouched and can be picked up later.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    interval = ANALYZE_BATCH_POLL_S if poll_s is None else poll_s
    limit = ANALYZE_BATCH_MAX_WAIT_S if max_wait_s is None else max_wait_s
    deadline = None if limit <= 0 else time.monotonic() + limit
    terminal = {
        types.JobState.JOB_STATE_SUCCEEDED,
        types.JobState.JOB_STATE_FAILED,
        types.JobState.JOB_STATE_CANCELLED,
        types.JobState.JOB_STATE_EXPIRED,
        types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED,
    }
    terminal_names = {s.name for s in terminal}
    errors = 0
    while True:
        try:
            job = await client.aio.batches.get(name=job_name)
            errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed poll says nothing about the job, so treat it as a blip
            # until it stops looking like one.
            errors += 1
            if errors >= ANALYZE_BATCH_POLL_MAX_ERRORS:
                raise BatchStillRunning(
                    f"could not read batch {job_name} after {errors} attempts: {exc}"
                ) from exc
            print(f"batch poll {job_name} failed ({exc}); retry {errors}")
        else:
            # Compare by name first: _batch_state_name handles every shape the
            # SDK returns, while `state in terminal` raises on anything
            # unhashable and would defeat that fallback by going first.
            if _batch_state_name(job) in terminal_names:
                return job
            try:
                if getattr(job, "state", None) in terminal:
                    return job
            except TypeError:
                pass
        if deadline is not None and time.monotonic() >= deadline:
            raise BatchStillRunning(
                f"batch {job_name} still running after {limit:.0f}s"
            )
        await asyncio.sleep(max(1.0, interval))


async def cancel_gemini_batch(job_name: str) -> None:
    """Best-effort cancel of an in-flight Batch job."""
    if not job_name:
        return
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    try:
        await client.aio.batches.cancel(name=job_name)
    except Exception as exc:
        print(f"batch cancel {job_name}: {exc}")


def mark_cohort_paused(pool: Path, cohort: dict) -> dict:
    """Persist paused status and return in-flight session rows to queued."""
    for entry in cohort.get("sessions") or []:
        session_dir = pool / entry["session_id"]
        if read_analysis(session_dir):
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            continue
        if entry.get("status") in ("running", "queued") or (
            read_job(session_dir) or {}
        ).get("status") == "running":
            entry["status"] = "queued"
            entry.pop("error", None)
            clear_job(session_dir)
    cohort = {**cohort, "status": "paused"}
    cohort.pop("batch", None)
    write_cohort(pool, cohort)
    return enrich_cohort_status(pool, cohort)


async def extract_session_digest(
    session_dir: Path, chunk_names: list, *, force: bool = False
) -> dict:
    """Rebuild frames for a session. Returns the extractor digest."""
    if not force:
        cached = read_analysis(session_dir)
        if cached:
            return {"cached": True, "analysis": cached}

    _, out_dir = analysis_paths(session_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    extract_gate, _ = _gates()
    async with extract_gate:
        digest = await run_extractor(session_dir, out_dir, chunk_names)
    if digest.get("error"):
        raise RuntimeError(digest["error"])
    return digest


def _parse_json_object(text: str) -> dict:
    """Parse a JSON object, tolerating optional markdown fences."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("model response was not an object")
    return data


async def analyze_session(session_dir: Path, chunk_names: list, force: bool = False) -> dict:
    if not ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    if not force:
        cached = read_analysis(session_dir)
        if cached:
            return cached

    digest = await extract_session_digest(session_dir, chunk_names, force=True)
    meta = session_metadata(session_dir)
    _, model_gate = _gates()
    async with model_gate:
        text = await call_gemini(digest, digest.get("frames") or [], meta)
    return write_analysis_result(
        session_dir,
        text,
        frame_count=len(digest.get("frames") or []),
        warnings=digest.get("warnings") or [],
    )


def load_session_digest(session_dir: Path) -> Optional[dict]:
    """Read extractor digest.json if a prior reconstruction left one on disk."""
    path = session_dir / ANALYSIS_DIR / "digest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def resume_cohort_batch(pool: Path, cohort: dict) -> dict:
    """Finish a cohort that already has a Gemini Batch job name persisted."""
    batch = cohort.get("batch") or {}
    job_name = batch.get("name")
    if not job_name:
        return enrich_cohort_status(pool, cohort)

    prepared = []
    wanted = set(batch.get("session_ids") or [])
    for entry in cohort.get("sessions") or []:
        sid = entry.get("session_id")
        if wanted and sid not in wanted:
            continue
        session_dir = pool / sid
        if read_analysis(session_dir):
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            continue
        digest = load_session_digest(session_dir) or {"frames": [], "warnings": []}
        entry["status"] = "running"
        write_job(session_dir, "running")
        prepared.append((entry, digest))
    write_cohort(pool, cohort)

    if prepared:
        # Same rule as the first attempt: a batch we could not collect keeps
        # its name and stays resumable. Letting this raise would strand the
        # cohort as "running" with nothing watching it, since the startup
        # resume task is never awaited.
        if not await _collect_cohort_batch(pool, cohort, prepared, job_name):
            return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)
    live = read_cohort(pool, cohort["id"]) or cohort
    live.pop("batch", None)
    final = enrich_cohort_status(pool, live)
    write_cohort(pool, persistable_cohort(final, live))
    return final


async def run_cohort_job(pool: Path, cohort: dict, *, force: bool = False) -> dict:
    """Extract cohort sessions, then analyze via Batch (or sync fallback).

    Mutates and persists the cohort as work progresses. Returns the enriched
    final cohort dict. Never deletes or overwrites other cohorts.
    """
    try:
        if (cohort.get("batch") or {}).get("name") and not force:
            return await resume_cohort_batch(pool, cohort)

        cohort = {**cohort, "status": "running"}
        cohort.pop("batch", None)
        write_cohort(pool, cohort)

        pending_entries = []
        for entry in cohort.get("sessions") or []:
            session_dir = pool / entry["session_id"]
            if not force and read_analysis(session_dir):
                entry["status"] = "done"
                entry["analysis"] = ANALYSIS_FILE
                entry.pop("error", None)
                continue
            entry["status"] = "queued"
            entry.pop("error", None)
            pending_entries.append(entry)
        write_cohort(pool, cohort)

        if not pending_entries:
            return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)

        prepared = []  # (entry, digest)

        async def extract_one(entry: dict) -> None:
            session_dir = pool / entry["session_id"]
            write_job(session_dir, "running")
            # One row, not the whole document: these coroutines run
            # concurrently and each owns exactly one session.
            mark_cohort_session(pool, cohort, entry, "running")
            try:
                # Reuse a prior reconstruction unless the caller asked to redo.
                # Without this, a retry after a Batch OOM would wipe digests and
                # spend another hour in Playwright before talking to Gemini.
                if not force:
                    digest = load_session_digest(session_dir)
                    if digest and _usable_frames(digest.get("frames") or []):
                        prepared.append((entry, digest))
                        return
                chunks = session_chunk_names(session_dir)
                if not chunks:
                    raise RuntimeError("no chunks to analyze")
                if force:
                    clear_analysis(session_dir)
                digest = await extract_session_digest(session_dir, chunks, force=True)
                prepared.append((entry, digest))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc) or "analysis failed"
                write_job(session_dir, "error", error=message)
                mark_cohort_session(pool, cohort, entry, "error", error=message)

        await asyncio.gather(*(extract_one(e) for e in pending_entries), return_exceptions=True)

        if not prepared:
            return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)

        use_batch = ANALYZE_BATCH and ANALYSIS_ENABLED
        job_name = None
        if use_batch:
            # Only submission falls back to sync. Once a job exists it is
            # billable, so a later failure must keep its name and be resumed
            # rather than re-running every session at interactive prices.
            try:
                job_name = await submit_prepared_gemini_batch(
                    prepared,
                    display_name=f"capture-{cohort['id']}",
                    work_dir=cohorts_dir(pool),
                    pool=pool,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No job was created, so nothing has been paid for: falling
                # back keeps a Batch outage from stranding an extracted cohort.
                print(f"cohort batch submit failed ({exc}); falling back to sync Gemini")
                cohort.pop("batch", None)
                write_cohort(pool, cohort)
                await _cohort_sync_model(pool, cohort, prepared)

        if job_name:
            cohort["batch"] = {
                "name": job_name,
                "submitted_at": int(time.time() * 1000),
                "session_ids": [e["session_id"] for e, _ in prepared],
            }
            write_cohort(pool, cohort)
            if not await _collect_cohort_batch(pool, cohort, prepared, job_name):
                # Still in flight and no longer being watched. The name stays
                # on disk so startup resume — or a Start from the UI — picks
                # the same job back up.
                return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)
        elif not use_batch:
            await _cohort_sync_model(pool, cohort, prepared)

        live = read_cohort(pool, cohort["id"]) or cohort
        live.pop("batch", None)
        final = enrich_cohort_status(pool, live)
        write_cohort(pool, persistable_cohort(final, live))
        return final
    except asyncio.CancelledError:
        live = read_cohort(pool, cohort["id"]) or cohort
        batch_name = (live.get("batch") or {}).get("name")
        if batch_name:
            await cancel_gemini_batch(batch_name)
        mark_cohort_paused(pool, live)
        raise


async def _collect_cohort_batch(
    pool: Path, cohort: dict, prepared: list, job_name: str
) -> bool:
    """Apply a submitted batch. False when it is still in flight.

    Everything past submission is recoverable: the job exists and has been paid
    for, so a batch we simply stopped watching must leave its name on disk for
    a resume. Only a batch that genuinely failed marks its sessions errored.
    """
    try:
        await apply_cohort_batch(pool, cohort, prepared, job_name=job_name)
        return True
    except asyncio.CancelledError:
        raise
    except BatchStillRunning as exc:
        print(f"cohort batch {job_name} not collected ({exc}); leaving it to resume")
        _mark_prepared_still_running(pool, cohort, prepared)
        return False
    except Exception as exc:
        # Reading or writing the results failed, not the job. The name stays so
        # a resume can retry the collection instead of re-billing the cohort.
        message = str(exc) or "could not apply batch results"
        print(f"cohort batch {job_name} apply failed ({message}); leaving it to resume")
        _mark_prepared_still_running(pool, cohort, prepared)
        return False


def _mark_prepared_still_running(pool: Path, cohort: dict, prepared: list) -> None:
    """Return the batch's sessions to `running` so a resume picks them up.

    Row at a time rather than one `write_cohort`: apply_cohort_batch may
    already have marked some of these done before it failed, and pushing the
    whole in-memory dict back would overwrite those completions with this
    copy's stale view of them.
    """
    for entry, _ in prepared:
        if entry.get("status") == "done":
            continue
        mark_cohort_session(pool, cohort, entry, "running")


async def apply_cohort_batch(
    pool: Path,
    cohort: dict,
    prepared: list,
    *,
    job_name: Optional[str] = None,
) -> None:
    """Wait for a batch job and write per-session analysis.json results."""
    name = job_name or (cohort.get("batch") or {}).get("name")
    if not name:
        raise RuntimeError("cohort has no batch job to apply")

    by_id = {entry["session_id"]: (entry, digest) for entry, digest in prepared}
    job = await wait_gemini_batch(name)
    state = _batch_state_name(job)
    if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
        err = getattr(job, "error", None)
        message = getattr(err, "message", None) if err else None
        message = message or f"Gemini batch {state}"
        for entry, _ in prepared:
            write_job(pool / entry["session_id"], "error", error=message)
            mark_cohort_session(pool, cohort, entry, "error", error=message)
        return

    results = parse_batch_results(job)
    for sid, (entry, digest) in by_id.items():
        session_dir = pool / sid
        hit = results.get(sid)
        if not hit:
            message = "missing from batch results"
            write_job(session_dir, "error", error=message)
            mark_cohort_session(pool, cohort, entry, "error", error=message)
        elif hit.get("error"):
            write_job(session_dir, "error", error=hit["error"])
            mark_cohort_session(pool, cohort, entry, "error", error=hit["error"])
        else:
            write_analysis_result(
                session_dir,
                hit["text"],
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            mark_cohort_session(
                pool, cohort, entry, "done", analysis=ANALYSIS_FILE
            )


async def _cohort_sync_model(pool: Path, cohort: dict, prepared: list) -> None:
    """Interactive Gemini fallback used when Batch is off or unavailable."""

    async def one(entry: dict, digest: dict) -> None:
        session_dir = pool / entry["session_id"]
        write_job(session_dir, "running")
        mark_cohort_session(pool, cohort, entry, "running")
        try:
            _, model_gate = _gates()
            async with model_gate:
                text = await call_gemini(
                digest, digest.get("frames") or [], session_metadata(session_dir)
            )
            write_analysis_result(
                session_dir,
                text,
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            mark_cohort_session(pool, cohort, entry, "done", analysis=ANALYSIS_FILE)
        except Exception as exc:
            message = str(exc) or "analysis failed"
            write_job(session_dir, "error", error=message)
            mark_cohort_session(pool, cohort, entry, "error", error=message)

    await asyncio.gather(
        *(one(entry, digest) for entry, digest in prepared),
        return_exceptions=True,
    )


def iter_session_pools(data_root: Path) -> list:
    """Session directories that may own a cohorts/ sibling."""
    pools = []
    legacy = data_root / "sessions"
    if legacy.is_dir():
        pools.append(legacy)
    projects = data_root / PROJECTS_DIR
    if projects.is_dir():
        for path in projects.iterdir():
            sessions = path / "sessions"
            if sessions.is_dir():
                pools.append(sessions)
    return pools


def cohorts_needing_batch_resume(data_root: Path) -> list:
    """(pool, cohort) pairs with an in-flight Batch job name.

    An index lookup over every pool at once. The directory scan this replaced
    parsed every cohort file in every pool on the server's startup path.
    """
    pools = {pool_key(pool): pool for pool in iter_session_pools(data_root)}
    # Touch each pool so a not-yet-migrated cohort is resumable on first boot.
    for pool in pools.values():
        cohort_store(pool)
    conn = analyze_store.connect(data_root)
    out = []
    for key, cohort in analyze_store.resumable_cohorts(conn):
        pool = pools.get(key)
        if pool is not None:
            out.append((pool, cohort))
    return out


def _cli_data_root() -> Path:
    return Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")


def _cli_pool(project: Optional[str]) -> Path:
    root = _cli_data_root()
    if not project:
        return root / "sessions"
    return root / PROJECTS_DIR / project / "sessions"


async def _cli_run_cohort(
    pool: Path,
    pool_key: str,
    purchased_n: int,
    all_n: int,
    force: bool,
    *,
    any_n: int = 0,
    min_duration_ms: int,
    url_contains: str,
) -> int:
    index = _cli_data_root() / "index.db"
    if not index.is_file():
        print(f"no session index at {index} — start the server once to build it")
        return 1
    if not ANALYSIS_ENABLED:
        print("analysis is not configured — set GEMINI_API_KEY")
        return 1
    if not runtime_present():
        print("analysis runtime unavailable — need node, extract.mjs, and the replayer bundle")
        return 1

    total = any_n if any_n > 0 else purchased_n + all_n
    if total < 1:
        print("request at least one session")
        return 1
    if total > COHORT_MAX_SESSIONS:
        print(f"cohort size capped at {COHORT_MAX_SESSIONS} sessions")
        return 1

    conn = sqlite3.connect(index)
    try:
        cohort = build_cohort(
            conn,
            pool_key,
            0 if any_n > 0 else purchased_n,
            0 if any_n > 0 else all_n,
            any_n=any_n,
            min_duration_ms=min_duration_ms,
            url_contains=url_contains,
        )
    finally:
        conn.close()

    if not cohort["sessions"]:
        print("no sessions matched the sample filters")
        write_cohort(pool, cohort)
        return 1

    write_cohort(pool, cohort)
    print(
        f"{cohort['label']}: {len(cohort['sessions'])} session(s) → "
        f"{cohort_path(pool, cohort['id'])}"
        f"{' [batch]' if ANALYZE_BATCH else ' [sync]'}"
    )
    final = await run_cohort_job(pool, cohort, force=force)
    prog = final["progress"]
    print(
        f"{final['status']}: {prog['done']} done · {prog['error']} error · "
        f"{prog['queued']} queued of {prog['total']}"
    )
    return 0 if prog["error"] == 0 and prog["done"] == prog["total"] else 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Capture session analysis")
    sub = parser.add_subparsers(dest="cmd")

    cohort_p = sub.add_parser("cohort", help="Sample purchased+all sessions and analyze them")
    cohort_p.add_argument(
        "--purchased",
        type=int,
        default=COHORT_DEFAULT_PURCHASED_N,
        help=f"purchased sample size (default {COHORT_DEFAULT_PURCHASED_N})",
    )
    cohort_p.add_argument(
        "--all",
        type=int,
        default=COHORT_DEFAULT_ALL_N,
        dest="all_n",
        help=f"all/non-purchase sample size (default {COHORT_DEFAULT_ALL_N})",
    )
    cohort_p.add_argument(
        "--any",
        type=int,
        default=0,
        dest="any_n",
        help="sample N matching sessions with no purchase split (overrides --purchased/--all)",
    )
    cohort_p.add_argument(
        "--min-duration-ms",
        type=int,
        default=COHORT_MIN_DURATION_MS,
        help=f"minimum last_seen-first_seen (default {COHORT_MIN_DURATION_MS})",
    )
    cohort_p.add_argument(
        "--url-contains",
        default=COHORT_URL_CONTAINS,
        help=f"require this path fragment in url (default {COHORT_URL_CONTAINS!r}; empty = any)",
    )
    cohort_p.add_argument("--project", default="", help="project token (default: no-project pool)")
    cohort_p.add_argument("--force", action="store_true", help="re-analyze even when analysis.json exists")

    args = parser.parse_args(argv)
    if args.cmd == "cohort":
        if args.purchased < 0 or args.all_n < 0 or args.any_n < 0:
            print("sample sizes must be >= 0")
            return 1
        pool = _cli_pool(args.project or None)
        pool_key = "" if not args.project else args.project
        return asyncio.run(
            _cli_run_cohort(
                pool,
                pool_key,
                args.purchased,
                args.all_n,
                args.force,
                any_n=args.any_n,
                min_duration_ms=args.min_duration_ms,
                url_contains=args.url_contains,
            )
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
