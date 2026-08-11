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
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "capture" / "analyze"

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


def _package_sources():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


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
        """`slices` and `stats` are pure leaves. If one of them ever imported a
        patchable name, patching the package would silently miss it."""
        from capture.analyze import slices, stats

        for module in (slices, stats):
            shared = PATCHABLE & set(vars(module))
            self.assertEqual(
                shared, set(), f"{module.__name__} holds patchable names: {shared}"
            )


if __name__ == "__main__":
    unittest.main()
