"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.

Cohort lifecycle (sample, Batch/sync job, resume) lives in `cohorts.py`. A
cohort is immutable once sampled, so a "cut" across it can be analysed once and
cached against it (`refresh=1` busts that cache).

Cohort state lives in Postgres (`0003_cohorts.sql`); the `cohorts/<id>.json`
layout this package used to write is read only by the migration in store.py.
Sampling defaults are in spec.py, not here.
"""

# Submodules kept reachable as `analyze.<name>` for callers that navigate to
# them that way. `as` rather than a bare import because these are re-exports,
# not uses: this file calls through the names below, not through the modules.
from . import cohort_state as cohort_state
from . import cohorts, cuts, gemini, runtime, session, spec
from . import slices as slices
from . import stats as stats
from . import storage as storage

# A cohort's status, projected from its session rows rather than read back. The
# state machine and the view/persist split live in cohort_state.py; re-exported
# because the port and the suite reach them off the package. `_undeclared_
# transitions` is a test hook the suite clears in place, so it must stay the same
# list object here — a copy would silently stop recording.
from .cohort_state import (
    COHORT_STATES,
    COHORT_TRANSITIONS,
    COHORT_VIEW_ONLY_FIELDS,
    COHORT_VIEW_ONLY_SESSION_FIELDS,
    SESSION_STALE,
    CohortState,
    attach_cohort_insights,
    cohort_transition,
    enrich_cohort_status,
    persistable_cohort,
    project_cohort_status,
    standout_line,
)
from .cohort_state import (
    _undeclared_transitions as _undeclared_transitions,
)

# The model's cut reply, coerced into the documented shape — including the guard
# that drops a share whose denominator is not this group's size.
from .cutfmt import CUT_SECTIONS, empty_cut_analysis, normalize_cut_analysis

# How a cohort describes itself: the selection sentence under it in the UI, and
# the auto-generated display name. Pure string generation over a normalised slice.
from .labels import _auto_cohort_label, clean_cohort_label, cohort_selection_text

# Cohort membership and the facet picker: the only SQL in the package.
from .sampling import facets_from_rows, pool_facets, sample_cohort_rows

# The slice language: pure predicates over dicts, shared by the sampler, the cut
# cache key and the UI labels.
from .slices import (
    DEVICE_VALUES,
    OUTCOME_LABELS,
    OUTCOME_VALUES,
    device_class,
    normalize_slice,
    outcome_class,
    page_path,
    row_matches_slice,
    slice_label,
)

# Measured splits: the model invents shares when asked to count, so the numbers
# in the cut prompt are computed in Python and handed over as exact. The gates
# and the cell builder are stats.py's own business, and tests reach them there.
from .stats import cross_tab_block

# Result and cohort persistence, addressed by pool path and session directory.
# Re-exported rather than re-implemented: `capture/analysis_port.py` and the
# suite reach these off the package, and ANALYSIS_FILE in particular is read by
# the port at import time. None of them is a patchable name, so binding by value
# here is safe — see the patching contract at the foot of this file.
from .storage import (
    ANALYSIS_DIR,
    ANALYSIS_FILE,
    COHORT_FILE,
    COHORTS_DIR,
    CUTS_SUFFIX,
    PROJECTS_DIR,
    SAFE_COHORT_ID,
    analysis_paths,
    clear_analysis,
    clear_cut,
    clear_job,
    cohort_path,
    cohort_store,
    cohorts_dir,
    cuts_path,
    data_root_for,
    forget_analyses,
    forget_pool_cohorts,
    forget_session,
    latest_cohort,
    list_cohorts,
    mark_cohort_session,
    migrate_legacy_cohort,
    pool_key,
    read_analysis,
    read_cohort,
    read_cut,
    read_cuts,
    read_job,
    safe_cohort_id,
    write_analysis,
    write_cohort,
    write_cut,
    write_job,
)
from .storage import (
    _is_project_pool as _is_project_pool,
)
from .storage import (
    _migrate_cohorts_once as _migrate_cohorts_once,
)
from .storage import (
    _migrate_lock as _migrate_lock,
)
from .storage import (
    _migrated as _migrated,
)
from .storage import (
    _read_cohort_file as _read_cohort_file,
)
from .storage import (
    _session_ref as _session_ref,
)

# What the package exports. Declared because most of the names above are
# re-exports this file never calls itself -- `capture/analysis_port.py` and the
# suite reach them off the package -- and without a declaration a linter reads
# every one of them as an unused import and offers to delete the package's map
# of itself.
#
# Not an access control: `analyze.<anything>` still resolves, which is what the
# patching contract at the foot of this file depends on. It states intent, and
# `tests/server/test_patch_contract.py` is what actually holds the surface still.
__all__ = [
    # Configuration and runtime.
    "ANALYSIS_ENABLED",
    "ANALYZE_BATCH",
    "ANALYZE_BATCH_MAX_WAIT_S",
    "ANALYZE_BATCH_POLL_MAX_ERRORS",
    "ANALYZE_BATCH_POLL_S",
    "ANALYZE_EXTRACT_CONCURRENCY",
    "ANALYZE_FPS",
    "ANALYZE_MAX_FRAMES",
    "ANALYZE_MODEL_CONCURRENCY",
    "ANALYZE_TIMEOUT_S",
    "BASE_DIR",
    "EXTRACTOR",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "REPLAYER_BUNDLE",
    "runtime_present",
    # Sampling vocabulary, aliased from spec.py under the older names.
    "COHORT_CANDIDATE_CAP",
    "COHORT_DEFAULT_ALL_N",
    "COHORT_DEFAULT_PURCHASED_N",
    "COHORT_MAX_SESSIONS",
    "COHORT_MIN_DURATION_MS",
    "COHORT_ORDERS",
    "COHORT_ORDER_EARLIEST",
    "COHORT_ORDER_LATEST",
    "COHORT_URL_CONTAINS",
    # Prompts.
    "CHAT_PROMPT",
    "CUT_PROMPT",
    "SYSTEM_PROMPT",
    "prompt_version",
    # Persistence (storage.py).
    "ANALYSIS_DIR",
    "ANALYSIS_FILE",
    "COHORTS_DIR",
    "COHORT_FILE",
    "CUTS_SUFFIX",
    "PROJECTS_DIR",
    "SAFE_COHORT_ID",
    "analysis_paths",
    "clear_analysis",
    "clear_cut",
    "clear_job",
    "cohort_path",
    "cohort_store",
    "cohorts_dir",
    "cuts_path",
    "data_root_for",
    "forget_analyses",
    "forget_pool_cohorts",
    "forget_session",
    "latest_cohort",
    "list_cohorts",
    "mark_cohort_session",
    "migrate_legacy_cohort",
    "pool_key",
    "read_analysis",
    "read_cohort",
    "read_cut",
    "read_cuts",
    "read_job",
    "safe_cohort_id",
    "write_analysis",
    "write_cohort",
    "write_cut",
    "write_job",
    # Cohort status (cohort_state.py).
    "COHORT_STATES",
    "COHORT_TRANSITIONS",
    "COHORT_VIEW_ONLY_FIELDS",
    "COHORT_VIEW_ONLY_SESSION_FIELDS",
    "CohortState",
    "SESSION_STALE",
    "attach_cohort_insights",
    "cohort_transition",
    "enrich_cohort_status",
    "persistable_cohort",
    "project_cohort_status",
    "standout_line",
    # The slice language (slices.py) and the measured splits (stats.py).
    "DEVICE_VALUES",
    "OUTCOME_LABELS",
    "OUTCOME_VALUES",
    "cross_tab_block",
    "device_class",
    "normalize_slice",
    "outcome_class",
    "page_path",
    "row_matches_slice",
    "slice_label",
    # Cuts and chat.
    "CHAT_MAX_MESSAGE_CHARS",
    "CHAT_MAX_TURNS",
    "CUT_SECTIONS",
    "build_cut_analysis",
    "call_gemini_cut",
    "chat_context",
    "clean_chat_messages",
    "cut_context",
    "cut_key",
    "empty_cut_analysis",
    "normalize_cut_analysis",
    "select_rows_by_cut",
    "stream_gemini_chat",
    # Cohort lifecycle (sampling.py, labels.py).
    "build_cohort",
    "clean_cohort_label",
    "cohort_selection_text",
    "delete_cohort",
    "facets_from_rows",
    "pool_facets",
    "rename_cohort",
    "sample_cohort_rows",
    # Extraction and the session write-up.
    "analyze_session",
    "build_inlined_session_request",
    "call_gemini",
    "extract_session_digest",
    "load_session_digest",
    "prompt_digest",
    "run_extractor",
    "session_chunk_names",
    "session_metadata",
    "write_analysis_result",
    # Batch.
    "BatchStillRunning",
    "apply_cohort_batch",
    "cancel_gemini_batch",
    "parse_batch_results",
    "submit_prepared_gemini_batch",
    "wait_gemini_batch",
    # Orchestration.
    "cohorts_needing_batch_resume",
    "iter_session_pools",
    "mark_cohort_paused",
    "resume_cohort_batch",
    "run_cohort_job",
]

# Config, path anchors, and the shared extract/model semaphores live in
# runtime.py. Re-exported below so `analyze.X` and analysis_port keep working;
# call sites in this file go through the `runtime` module object so test patches
# against `capture.analyze.runtime` reach the reader.
_gates = runtime._gates
runtime_present = runtime.runtime_present

# Mutable / rebound runtime names are forwarded via __getattr__ rather than
# copied at import: a from-import of ANALYSIS_ENABLED would freeze the bool, and
# analyze._gate_loop = None would set an unrelated attribute while _gates kept
# reading runtime._gate_loop. Tests that rebind these assign on analyze.runtime.
_RUNTIME_EXPORTS = frozenset(
    {
        "ANALYSIS_ENABLED",
        "ANALYZE_BATCH",
        "ANALYZE_BATCH_MAX_WAIT_S",
        "ANALYZE_BATCH_POLL_MAX_ERRORS",
        "ANALYZE_BATCH_POLL_S",
        "ANALYZE_EXTRACT_CONCURRENCY",
        "ANALYZE_FPS",
        "ANALYZE_MAX_FRAMES",
        "ANALYZE_MODEL_CONCURRENCY",
        "ANALYZE_TIMEOUT_S",
        "BASE_DIR",
        "EXTRACTOR",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "REPLAYER_BUNDLE",
        "_extract_gate",
        "_gate_loop",
        "_model_gate",
    }
)

# Gemini I/O and its request-assembly helpers live in gemini.py. Forwarded the
# same way as runtime so `analyze.call_gemini` stays the public name while
# orchestration and tests resolve through `gemini.<name>` / mock.patch.
_GEMINI_EXPORTS = frozenset(
    {
        "BatchStillRunning",
        "SYSTEM_PROMPT",
        "_batch_jsonl_path",
        "_batch_response_text",
        "_batch_state_name",
        "_clock",
        "_seconds",
        "_session_attribution",
        "_session_model_config",
        "_session_model_parts",
        "_usable_frames",
        "build_inlined_session_request",
        "call_gemini",
        "cancel_gemini_batch",
        "parse_batch_results",
        "prompt_digest",
        "session_metadata",
        "submit_prepared_gemini_batch",
        "wait_gemini_batch",
    }
)

# Extractor + single-session analyze path live in session.py. Same forwarding
# rule as gemini: package re-exports for callers; orchestration patches the
# owner module (`session.run_extractor`).
_SESSION_EXPORTS = frozenset(
    {
        "analyze_session",
        "extract_session_digest",
        "load_session_digest",
        "run_extractor",
        "session_chunk_names",
        "write_analysis_result",
    }
)

# Cut + chat orchestration lives in cuts.py. Same forwarding rule: package
# re-exports for callers; tests patch `cuts.call_gemini_cut` /
# `cuts.stream_gemini_chat` / `cuts.CUT_PROMPT` at the definition site.
_CUTS_EXPORTS = frozenset(
    {
        "CHAT_MAX_MESSAGE_CHARS",
        "CHAT_MAX_TURNS",
        "CHAT_PROMPT",
        "CUT_PROMPT",
        "_cut_side_text",
        "_given_facts",
        "_parse_json_object",
        "build_cut_analysis",
        "call_gemini_cut",
        "chat_context",
        "clean_chat_messages",
        "cut_context",
        "cut_key",
        "prompt_version",
        "select_rows_by_cut",
        "stream_gemini_chat",
    }
)

# Cohort lifecycle + run_cohort_job live in cohorts.py. Same forwarding rule:
# package re-exports for callers; tests patch `cohorts._cohort_sync_model` /
# other edges at the definition site.
_COHORTS_EXPORTS = frozenset(
    {
        "_cohort_sync_model",
        "_collect_cohort_batch",
        "_mark_prepared_still_running",
        "apply_cohort_batch",
        "build_cohort",
        "cohorts_needing_batch_resume",
        "delete_cohort",
        "iter_session_pools",
        "mark_cohort_paused",
        "rename_cohort",
        "resume_cohort_batch",
        "run_cohort_job",
    }
)


def __getattr__(name: str):
    if name in _RUNTIME_EXPORTS:
        return getattr(runtime, name)
    if name in _GEMINI_EXPORTS:
        return getattr(gemini, name)
    if name in _SESSION_EXPORTS:
        return getattr(session, name)
    if name in _CUTS_EXPORTS:
        return getattr(cuts, name)
    if name in _COHORTS_EXPORTS:
        return getattr(cohorts, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# The sampling vocabulary lives in spec.py. Aliased under the older names so the
# sampler and the port keep reading the way they always have.
COHORT_DEFAULT_PURCHASED_N = spec.DEFAULT_PURCHASED_N
COHORT_DEFAULT_ALL_N = spec.DEFAULT_ALL_N
COHORT_MAX_SESSIONS = spec.MAX_SESSIONS
COHORT_MIN_DURATION_MS = spec.MIN_DURATION_MS
COHORT_URL_CONTAINS = spec.URL_CONTAINS
COHORT_CANDIDATE_CAP = spec.CANDIDATE_CAP
COHORT_ORDER_LATEST = spec.ORDER_LATEST
COHORT_ORDER_EARLIEST = spec.ORDER_EARLIEST
COHORT_ORDERS = spec.ORDERS


# --- The patching contract -------------------------------------------------
#
# Gemini sync/batch I/O lives in `gemini.py`; extractor + session analyze in
# `session.py`; cut + chat in `cuts.py`; cohort lifecycle + `run_cohort_job` in
# `cohorts.py`. Call sites go through those module objects
# (`gemini.call_gemini(...)`, `session.extract_session_digest`,
# `cohorts._cohort_sync_model`, etc.), and tests patch at the definition site
# with `unittest.mock.patch("capture.analyze.gemini…")` /
# `patch("capture.analyze.session…")` / `patch("capture.analyze.cuts…")` /
# `patch("capture.analyze.cohorts…")`. Package re-exports of those names are
# for `analysis_port` and other readers, not for assignment patching.
#
# Config and `_gates` live in `runtime.py`. Rebound test hooks
# (`ANALYSIS_ENABLED`, `ANALYZE_BATCH`, `_gate_loop`, the concurrency caps)
# must be assigned on `analyze.runtime`, not on the package — a package
# assignment would not reach `_gates` or the orchestration call sites above.
# `_migrated` and `_undeclared_transitions` are only **mutated in place**, so a
# re-export binds the same object and `.clear()` still reaches the definition.
#
# See docs/analyze-design-review.md §4.1 and the modularize plan.
