"""What status a cohort is in, derived from its session rows rather than trusted.

A cohort's stored status is a cache, not a record. A crashed runner leaves rows
saying "running"; a retried session turns an errored cohort back into a finished
one. So the terminal states are recomputed on every read from what the sessions
actually show, and `project_cohort_status` is the projection that does it.

Split from `__init__.py` on the same rule as `storage.py`: nothing here reads a
name the tests patch, and nothing calls back into orchestration. It reads results
through the storage facade and describes cohorts through `labels.py`, both of
which sit below it. See `tests/server/test_patch_contract.py`.

`persistable_cohort` is the counterpart to `enrich_cohort_status` and they must
be read together: enrich lays a view over the stored cohort, persistable strips
that view back off before anything writes it down.
"""

import re

from . import spec, storage
from .labels import cohort_selection_text
from .storage import ANALYSIS_FILE


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
    CohortState.IDLE: {
        CohortState.QUEUED,
        CohortState.RUNNING,
        CohortState.PAUSED,
        CohortState.DONE,
        CohortState.ERROR,
        CohortState.IDLE,
    },
    CohortState.QUEUED: {
        CohortState.RUNNING,
        CohortState.PAUSED,
        CohortState.DONE,
        CohortState.ERROR,
        CohortState.QUEUED,
    },
    CohortState.RUNNING: {
        CohortState.DONE,
        CohortState.ERROR,
        CohortState.PAUSED,
        CohortState.RUNNING,
    },
    CohortState.PAUSED: {
        CohortState.RUNNING,
        CohortState.QUEUED,
        CohortState.DONE,
        CohortState.PAUSED,
    },
    # Terminal, but recomputed: see above. QUEUED is reachable from both
    # because a write-up can go missing after the fact (rule 5), which returns
    # the cohort to needing work.
    CohortState.DONE: {
        CohortState.DONE,
        CohortState.ERROR,
        CohortState.RUNNING,
        CohortState.QUEUED,
    },
    CohortState.ERROR: {
        CohortState.ERROR,
        CohortState.DONE,
        CohortState.RUNNING,
        CohortState.QUEUED,
    },
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
        tally["done"],
        tally["running"],
        tally["queued"],
        tally["error"],
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
        return cohort_transition(
            current, CohortState.QUEUED, why="write-ups missing, needs re-analysis"
        )

    return current


def enrich_cohort_status(project_id: str, cohort: dict) -> dict:
    """Refresh per-session status from analyses / analysis_jobs in Postgres."""
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
        sid = row["session_id"]
        cached = storage.read_analysis(project_id, sid)
        job = storage.read_job(project_id, sid)
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
            # Claims done, but the branches above found no write-up. The
            # result was removed after the fact (clear_analysis on a
            # re-analyze, a deleted row, a crash between the two). Counting it
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
        selection = cohort_selection_text(spec.CohortSpec.from_cohort(cohort))

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


def persistable_cohort(enriched: dict, stored: dict | None = None) -> dict:
    """Strip an enriched cohort back to what belongs on disk.

    The status refresh itself is a durable fact — a crashed runner leaves rows
    saying "running" that only a re-read of analysis.json corrects — so callers
    do need to write it back. GET does that through `persist_cohort_refresh`
    rather than `write_cohort`, so a poll cannot DELETE+INSERT membership over
    in-flight marks. Pipeline finish still goes through this function, then
    `write_cohort`. What they must not write is the view laid over it.

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
            {k: v for k, v in row.items() if k not in COHORT_VIEW_ONLY_SESSION_FIELDS}
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


def attach_cohort_insights(project_id: str, cohort: dict) -> dict:
    """Attach analysis write-ups — response-only, never written to the cohort file."""
    sessions = []
    for entry in cohort.get("sessions") or []:
        row = dict(entry)
        if row.get("status") == "done":
            cached = storage.read_analysis(project_id, row["session_id"])
            if cached and isinstance(cached.get("text"), str) and cached["text"].strip():
                row["text"] = cached["text"]
                row["standout"] = standout_line(cached["text"])
                if cached.get("model"):
                    row["model"] = cached["model"]
                if cached.get("frame_count") is not None:
                    row["frame_count"] = cached["frame_count"]
        sessions.append(row)
    return {**cohort, "sessions": sessions}
