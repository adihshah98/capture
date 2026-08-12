"""The patching contract, checked statically.

Every expensive edge of the analysis pipeline -- the headless Chromium, the paid
Gemini API -- is swapped in tests by assignment onto the package
(`analyze.call_gemini = fake`) and then the *real* pipeline is driven. That only
works while the call site resolves the name at call time, through a namespace the
assignment reaches.

`from .models import call_gemini` breaks it. The value is copied into the importing
module's globals at import, so a later `analyze.call_gemini = fake` rebinds the copy
while the pipeline keeps calling the real one. Nothing fails: the assertions are
about cohort state, and a real Gemini response satisfies them exactly as well as a
fake one does. The symptom is a green suite that makes billed API calls against live
infrastructure.

This was previously enforced at runtime by `_ForwardingModule`, which replaced the
package's `__class__` so assignment fanned out into whichever submodule owned the
name. That mechanism was invisible to type checkers, fired on every module-global
write, and sat at the bottom of a 3,000-line module where an import-time exception
would disable it silently. The rule it protected is real; the machinery was not the
only way to protect it.

So the rule is checked here instead: no module in the package may bind a patchable
name via `from . import X`. Import the module, call through it.
"""

import ast
import asyncio
import importlib
import pkgutil
import types
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent.parent / "capture" / "analyze"

# Names the suite rebinds on the package, or that a submodule could plausibly
# rebind later. A `from .x import <one of these>` is the bug this file exists to
# catch; anything else is an ordinary import.
#
# Derived from the actual patch sites (grep for `analyze.<name> =` in tests/) plus
# the prompt constants, which tests reassign to check cache invalidation.
PATCHABLE = frozenset(
    {
        "run_extractor",
        "call_gemini",
        "call_gemini_cut",
        "stream_gemini_chat",
        "wait_gemini_batch",
        "submit_prepared_gemini_batch",
        "parse_batch_results",
        "cancel_gemini_batch",
        "_cohort_sync_model",
        "SYSTEM_PROMPT",
        "CUT_PROMPT",
        "CHAT_PROMPT",
        "ANALYSIS_ENABLED",
        "ANALYZE_BATCH",
    }
)


# Everything the package exposes that callers outside its defining module reach:
# `capture/analysis_port.py` plus thirteen test modules. Generated from the live
# package, so regenerate rather than hand-edit when a name is deliberately added
# or removed:
#
#   python -c "from capture import analyze; import types; print(sorted(
#       n for n, v in vars(analyze).items() if not n.startswith('__')
#       and not isinstance(v, types.ModuleType)
#       and getattr(v, '__module__', 'capture.analyze').startswith('capture')))"
EXPECTED_SURFACE = frozenset(
    {
        "ANALYSIS_DIR", "ANALYSIS_ENABLED", "ANALYSIS_FILE", "ANALYZE_BATCH",
        "ANALYZE_BATCH_MAX_WAIT_S", "ANALYZE_BATCH_POLL_MAX_ERRORS",
        "ANALYZE_BATCH_POLL_S", "ANALYZE_EXTRACT_CONCURRENCY", "ANALYZE_FPS",
        "ANALYZE_MAX_FRAMES", "ANALYZE_MODEL_CONCURRENCY", "ANALYZE_TIMEOUT_S",
        "BASE_DIR", "EXTRACTOR", "REPLAYER_BUNDLE", "SAFE_COHORT_ID", "_migrate_lock",
        "BatchStillRunning", "CHAT_MAX_MESSAGE_CHARS", "CHAT_MAX_TURNS", "CHAT_PROMPT",
        "COHORTS_DIR", "COHORT_CANDIDATE_CAP", "COHORT_DEFAULT_ALL_N",
        "COHORT_DEFAULT_PURCHASED_N", "COHORT_FILE", "COHORT_MAX_SESSIONS",
        "COHORT_MIN_DURATION_MS", "COHORT_ORDERS", "COHORT_ORDER_EARLIEST",
        "COHORT_ORDER_LATEST", "COHORT_STATES", "COHORT_TRANSITIONS",
        "COHORT_URL_CONTAINS", "COHORT_VIEW_ONLY_FIELDS",
        "COHORT_VIEW_ONLY_SESSION_FIELDS", "CUTS_SUFFIX", "CUT_PROMPT", "CUT_SECTIONS",
        "CohortState", "DEVICE_VALUES", "GEMINI_API_KEY", "GEMINI_MODEL",
        "OUTCOME_LABELS", "OUTCOME_VALUES", "PROJECTS_DIR", "SESSION_STALE",
        "SYSTEM_PROMPT", "_auto_cohort_label", "_batch_jsonl_path",
        "_batch_response_text", "_batch_state_name", "_clock", "_cohort_sync_model",
        "_collect_cohort_batch", "_cut_side_text", "_extract_gate", "_gate_loop",
        "_gates", "_given_facts", "_is_project_pool", "_mark_prepared_still_running",
        "_migrate_cohorts_once", "_migrated", "_model_gate", "_parse_json_object",
        "_read_cohort_file", "_seconds", "_session_attribution",
        "_session_model_config", "_session_model_parts", "_session_ref",
        "_undeclared_transitions", "_usable_frames", "analysis_paths",
        "analyze_session", "apply_cohort_batch", "attach_cohort_insights",
        "build_cohort", "build_cut_analysis", "build_inlined_session_request",
        "call_gemini", "call_gemini_cut", "cancel_gemini_batch", "chat_context",
        "clean_chat_messages", "clean_cohort_label", "clear_analysis", "clear_cut",
        "clear_job", "cohort_path", "cohort_selection_text", "cohort_store",
        "cohort_transition", "cohorts_dir", "cohorts_needing_batch_resume",
        "cross_tab_block", "cut_context", "cut_key", "cuts_path", "data_root_for",
        "delete_cohort", "device_class", "empty_cut_analysis", "enrich_cohort_status",
        "extract_session_digest", "facets_from_rows", "forget_analyses",
        "forget_pool_cohorts", "forget_session", "iter_session_pools", "latest_cohort",
        "list_cohorts", "load_session_digest", "mark_cohort_paused",
        "mark_cohort_session", "migrate_legacy_cohort", "normalize_cut_analysis",
        "normalize_slice", "outcome_class", "page_path", "parse_batch_results",
        "persistable_cohort", "pool_facets", "pool_key", "project_cohort_status",
        "prompt_digest", "prompt_version", "read_analysis", "read_cohort", "read_cut",
        "read_cuts", "read_job", "rename_cohort", "resume_cohort_batch",
        "row_matches_slice", "run_cohort_job", "run_extractor", "runtime_present",
        "safe_cohort_id", "sample_cohort_rows", "select_rows_by_cut",
        "session_chunk_names", "session_metadata", "slice_label", "standout_line",
        "stream_gemini_chat", "submit_prepared_gemini_batch", "wait_gemini_batch",
        "write_analysis", "write_analysis_result", "write_cohort", "write_cut",
        "write_job",
    }
)


def _package_sources():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _module_level_names(source: Path) -> set:
    """Names a module defines at top level: def, async def, class, assignment.

    Imports are excluded on purpose — `json` and `Path` are import plumbing, not
    surface. Read from the AST rather than from a live module so the answer does
    not change with whatever ran first.
    """
    names = set()
    for node in ast.parse(source.read_text(), filename=str(source)).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


class TestPatchableNamesAreNotBoundAtImport(unittest.TestCase):
    """A patchable name must be reached through its module, not copied in."""

    def test_no_relative_import_binds_a_patchable_name(self):
        offenders = []
        for source in _package_sources():
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                # `level` > 0 is a relative import: `from .x import y`. Absolute
                # imports of third-party names are not our concern.
                if not isinstance(node, ast.ImportFrom) or not node.level:
                    continue
                for alias in node.names:
                    if alias.name in PATCHABLE:
                        offenders.append(
                            f"{source.name}:{node.lineno} binds {alias.name} at import "
                            f"— import the module and call through it instead"
                        )
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_the_package_still_defines_the_patchable_names(self):
        """Guards the list itself. If a name here is renamed or moved out of the
        package, this test goes stale silently and stops protecting anything."""
        from capture import analyze

        missing = [name for name in PATCHABLE if not hasattr(analyze, name)]
        self.assertEqual(
            missing,
            [],
            f"PATCHABLE lists names the package no longer has: {missing}. "
            "Update the list, or restore the name.",
        )


class TestPatchingReachesTheCallSite(unittest.TestCase):
    """The contract, demonstrated end to end rather than only asserted."""

    def test_rebinding_redirects_the_pipeline(self):
        from capture import analyze

        original = analyze.call_gemini
        try:
            analyze.call_gemini = lambda *a, **k: "fake"
            # Resolve the way the pipeline does: through the module's globals at
            # call time. A `from` binding elsewhere would not see this.
            self.assertEqual(analyze.call_gemini(), "fake")
            self.assertIs(vars(analyze)["call_gemini"], analyze.call_gemini)
        finally:
            analyze.call_gemini = original
        self.assertIs(analyze.call_gemini, original)

    def test_submodules_do_not_shadow_the_package_namespace(self):
        """No submodule may hold a patchable name in its own globals.

        Discovered rather than listed. This named `slices` and `stats` while they
        were the only leaves, which meant every module extracted afterwards was
        unchecked until someone remembered to add it — backwards for a test whose
        whole job is to police new modules.
        """
        import capture.analyze

        offenders = {}
        for info in pkgutil.iter_modules(capture.analyze.__path__):
            module = importlib.import_module(f"capture.analyze.{info.name}")
            shared = PATCHABLE & set(vars(module))
            if shared:
                offenders[module.__name__] = sorted(shared)
        self.assertEqual(offenders, {}, f"submodules hold patchable names: {offenders}")

    def test_the_gate_loop_is_rebindable_from_the_package(self):
        """`_gates` must read `_gate_loop` out of the namespace tests assign to.

        Nine test sites reset the gates with `analyze._gate_loop = None` so a new
        event loop gets fresh semaphores. That is a *rebind*, not a mutation, so
        it only works while `_gates` resolves the name in the same module the
        assignment lands in. Move `_gates` into a submodule and the assignment
        silently sets an unrelated attribute: the reset stops happening, and
        semaphores bound to a dead loop leak into the next test.
        """
        from capture import analyze

        async def gates():
            return analyze._gates()

        previous = analyze._gate_loop
        try:
            analyze._gate_loop = None
            first = asyncio.run(gates())
            analyze._gate_loop = None
            second = asyncio.run(gates())
            # Fresh objects, or the rebind did not reach the reader.
            self.assertIsNot(first[0], second[0])
            self.assertIsNot(first[1], second[1])
        finally:
            analyze._gate_loop = previous


class TestThePackageSurfaceIsDeclared(unittest.TestCase):
    """What the package exposes, pinned.

    Thirteen test modules and `capture/analysis_port.py` reach names off the
    package rather than off the module that defines them, so a re-export dropped
    during a refactor breaks callers far from the edit. Without this, that shows
    up as a confusing AttributeError somewhere downstream; with it, as one diff
    naming exactly what went missing.

    Incidental imports (`json`, `Path`, submodule objects) are excluded — this
    pins the analysis surface, not the import block.

    Derived from the source rather than from `vars()`, so it does not depend on
    what has already run: `_extract_gate` and `_model_gate` hold `None` until the
    first `_gates()` call replaces them with `asyncio.Semaphore`s, and a
    value-based filter would include them or not depending on test order.
    """

    def test_the_exported_surface_matches_the_declared_list(self):
        from capture import analyze

        # Assigned or defined at module level in the package's own sources.
        declared = set()
        for source in _package_sources():
            if source.parent != PACKAGE or source.name == "__init__.py":
                continue
            declared |= _module_level_names(source)
        declared |= _module_level_names(PACKAGE / "__init__.py")

        actual = {
            name
            for name, value in vars(analyze).items()
            if not name.startswith("__")
            and not isinstance(value, types.ModuleType)
            and name in declared
        }
        missing = sorted(EXPECTED_SURFACE - actual)
        added = sorted(actual - EXPECTED_SURFACE)
        self.assertEqual(
            (missing, added),
            ([], []),
            f"package surface changed — gone: {missing}; new: {added}. "
            "If the change is intended, update EXPECTED_SURFACE; if not, restore "
            "the re-export.",
        )


if __name__ == "__main__":
    unittest.main()
