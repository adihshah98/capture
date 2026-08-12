"""The patching contract, checked statically.

Every expensive edge of the analysis pipeline -- the headless Chromium, the paid
Gemini API -- is swapped in tests so CI never bills and never boots browsers,
then the *real* pipeline is driven.

Gemini sync/batch I/O lives in `capture.analyze.gemini`. The extractor and
single-session path live in `capture.analyze.session`. Tests patch those modules
with `unittest.mock.patch` (or `patch.object`). Remaining edges still rebound on
the package by assignment (`analyze.call_gemini_cut = fake`) must resolve at call
time through a namespace the assignment reaches.

`from .x import call_gemini_cut` (etc.) breaks package-assignment patches: the
value is copied into the importing module's globals at import, so a later
`analyze.call_gemini_cut = fake` rebinds the copy while the pipeline keeps
calling the real one. Nothing fails: the assertions are about cohort state, and
a real Gemini response satisfies them exactly as well as a fake one does. The
symptom is a green suite that makes billed API calls against live
infrastructure.

So the rule for names still in PATCHABLE is checked here: no module in the
package may bind one via `from . import X`. Import the module, call through it.
Names migrated to definition-site mock.patch are dropped from PATCHABLE.
"""

import ast
import asyncio
import importlib
import pkgutil
import types
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parent.parent.parent / "capture" / "analyze"

# Names the suite still rebinds on the package (or that a submodule could
# plausibly rebind later via assignment). A `from .x import <one of these>` is
# the bug this file exists to catch for that style.
#
# Gemini I/O and session extraction moved to owner modules; tests patch
# `capture.analyze.gemini` / `capture.analyze.session` rather than the package.
# ANALYSIS_ENABLED / ANALYZE_BATCH live on runtime.py for the same reason.
PATCHABLE = frozenset(
    {
        "call_gemini_cut",
        "stream_gemini_chat",
        "_cohort_sync_model",
        "CUT_PROMPT",
        "CHAT_PROMPT",
    }
)


# The private hooks the suite reaches that `__all__` deliberately does not list.
# Underscore-prefixed and not public, but thirteen test modules depend on them:
# `_migrated` and `_undeclared_transitions` are cleared between cases,
# `_gate_loop` is rebound on analyze.runtime to reset the semaphores, and the
# rest are called directly. They are named here so dropping one is a failure
# rather than a surprise somewhere downstream.
EXPECTED_PRIVATE_SURFACE = frozenset(
    {
        "_auto_cohort_label",
        "_batch_jsonl_path",
        "_batch_response_text",
        "_batch_state_name",
        "_clock",
        "_cohort_sync_model",
        "_collect_cohort_batch",
        "_cut_side_text",
        "_extract_gate",
        "_gate_loop",
        "_gates",
        "_given_facts",
        "_is_project_pool",
        "_mark_prepared_still_running",
        "_migrate_cohorts_once",
        "_migrate_lock",
        "_migrated",
        "_model_gate",
        "_parse_json_object",
        "_read_cohort_file",
        "_seconds",
        "_session_attribution",
        "_session_model_config",
        "_session_model_parts",
        "_session_ref",
        "_undeclared_transitions",
        "_usable_frames",
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

    def test_gemini_patches_reach_orchestration_via_the_owner_module(self):
        """Definition-site patch: orchestrators call gemini.call_gemini through
        the module object, so mock.patch on that path redirects the pipeline."""
        from capture import analyze

        async def fake(*a, **k):
            return "fake"

        with patch.object(analyze.gemini, "call_gemini", fake):
            self.assertIs(analyze.gemini.call_gemini, fake)
            # Package re-export tracks the owner module.
            self.assertIs(analyze.call_gemini, fake)

    def test_session_extractor_patches_reach_digest_via_the_owner_module(self):
        """Definition-site patch: extract_session_digest looks up run_extractor
        in session.py, so mock.patch there redirects reconstruction."""
        from capture import analyze

        async def fake(*a, **k):
            return {"frames": [], "warnings": []}

        with patch.object(analyze.session, "run_extractor", fake):
            self.assertIs(analyze.session.run_extractor, fake)
            self.assertIs(analyze.run_extractor, fake)

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

    def test_the_gate_loop_is_rebindable_from_runtime(self):
        """`_gates` must read `_gate_loop` out of the namespace tests assign to.

        Test sites reset the gates with `analyze.runtime._gate_loop = None` so a
        new event loop gets fresh semaphores. That is a *rebind*, not a mutation,
        so it only works while `_gates` resolves the name in the same module the
        assignment lands in (`runtime.py`).
        """
        from capture import analyze

        async def gates():
            return analyze.runtime._gates()

        previous = analyze.runtime._gate_loop
        try:
            analyze.runtime._gate_loop = None
            first = asyncio.run(gates())
            analyze.runtime._gate_loop = None
            second = asyncio.run(gates())
            # Fresh objects, or the rebind did not reach the reader.
            self.assertIsNot(first[0], second[0])
            self.assertIsNot(first[1], second[1])
        finally:
            analyze.runtime._gate_loop = previous


class TestThePackageSurfaceIsDeclared(unittest.TestCase):
    """What the package exposes, pinned.

    Thirteen test modules and `capture/analysis_port.py` reach names off the
    package rather than off the module that defines them, so a re-export dropped
    during a refactor breaks callers far from the edit. Without this, that shows
    up as a confusing AttributeError somewhere downstream; with it, as one diff
    naming exactly what went missing.

    The public half is checked against the package's own `__all__` rather than a
    copy of it -- a second hand-maintained list would just drift from the first.
    What `__all__` cannot cover is the private hooks the suite reaches, so those
    are listed above.
    """

    def test_every_public_name_resolves(self):
        """`__all__` promises these; a stale entry is a broken `import *`."""
        from capture import analyze

        missing = sorted(name for name in analyze.__all__ if not hasattr(analyze, name))
        self.assertEqual(missing, [], f"__all__ names the package does not have: {missing}")

    def test_all_is_free_of_duplicates(self):
        from capture import analyze

        duplicated = sorted({n for n in analyze.__all__ if analyze.__all__.count(n) > 1})
        self.assertEqual(duplicated, [], f"__all__ lists these twice: {duplicated}")

    def test_the_private_hooks_the_suite_reaches_are_present(self):
        from capture import analyze

        missing = sorted(name for name in EXPECTED_PRIVATE_SURFACE if not hasattr(analyze, name))
        self.assertEqual(
            missing,
            [],
            f"private names the suite depends on are gone: {missing}. Restore the "
            "re-export, or drop them from EXPECTED_PRIVATE_SURFACE and from the "
            "tests that use them.",
        )

    def test_no_public_definition_is_left_out_of_all(self):
        """The direction that actually rots: a name added to a submodule, wired
        into `__init__` and never declared. Read from the AST rather than
        `vars()` so it does not depend on what has already run -- `_extract_gate`
        holds None until the first `_gates()` call swaps in a Semaphore.
        """
        from capture import analyze

        declared = set()
        for source in _package_sources():
            if source.parent == PACKAGE:
                declared |= _module_level_names(source)

        public = {
            name
            for name, value in vars(analyze).items()
            if not name.startswith("_")
            and not isinstance(value, types.ModuleType)
            and name in declared
        }
        undeclared = sorted(public - set(analyze.__all__))
        self.assertEqual(
            undeclared,
            [],
            f"reachable off the package but not in __all__: {undeclared}. Add them, "
            "or make them private.",
        )


if __name__ == "__main__":
    unittest.main()
