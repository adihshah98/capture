"""The patching contract, checked statically.

Every expensive edge of the analysis pipeline -- the headless Chromium, the paid
Gemini API -- is swapped in tests so CI never bills and never boots browsers,
then the *real* pipeline is driven.

Gemini sync/batch I/O lives in `capture.analyze.gemini`. The extractor and
single-session path live in `capture.analyze.session`. Cut and chat live in
`capture.analyze.cuts`. Cohort lifecycle + `run_cohort_job` live in
`capture.analyze.cohorts`. Tests patch those modules with `unittest.mock.patch`
(or `patch.object`) at the definition site.

The package itself is not a facade: production goes through
`capture.analysis_port`, which imports owner modules, and tests do the same.
Nothing rebinds pipeline edges by assignment on the package namespace.
"""

import ast
import asyncio
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent.parent / "capture" / "analyze"

# Names tests patch on owner modules. A `from .gemini import call_gemini` (or
# the equivalent for any of these) copies the function into the importer's
# globals at import time, so a later `patch.object(gemini, "call_gemini", fake)`
# never reaches the call site — a green suite that bills Gemini.
#
# Import the module and call through it: `gemini.call_gemini(...)`.
# Keep this set in lockstep with the patch.object targets in tests/server.
PATCHABLE = frozenset(
    {
        "run_extractor",
        "call_gemini",
        "submit_prepared_gemini_batch",
        "wait_gemini_batch",
        "parse_batch_results",
        "session_metadata",
        "call_gemini_cut",
        "stream_gemini_chat",
        "_cohort_sync_model",
    }
)


def _package_sources():
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


class TestPatchableNamesAreNotBoundAtImport(unittest.TestCase):
    """A patchable name must be reached through its module, not copied in."""

    def test_no_relative_import_binds_a_patchable_name(self):
        self.assertTrue(PATCHABLE, "an empty PATCHABLE makes this test vacuous")
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


class TestPatchingReachesTheCallSite(unittest.TestCase):
    """The contract, demonstrated end to end rather than only asserted."""

    def test_the_gate_loop_is_rebindable_from_runtime(self):
        """`_gates` must read `_gate_loop` out of the namespace tests assign to.

        Test sites reset the gates with `runtime._gate_loop = None` so a
        new event loop gets fresh semaphores. That is a *rebind*, not a mutation,
        so it only works while `_gates` resolves the name in the same module the
        assignment lands in (`runtime.py`).
        """
        from capture.analyze import runtime

        async def gates():
            return runtime._gates()

        previous = runtime._gate_loop
        try:
            runtime._gate_loop = None
            first = asyncio.run(gates())
            runtime._gate_loop = None
            second = asyncio.run(gates())
            # Fresh objects, or the rebind did not reach the reader.
            self.assertIsNot(first[0], second[0])
            self.assertIsNot(first[1], second[1])
        finally:
            runtime._gate_loop = previous


class TestThePackageIsNotAFacade(unittest.TestCase):
    """Owner names live on owner modules. The package does not re-export them."""

    def test_the_package_does_not_forward_or_reexport_pipeline_names(self):
        from capture import analyze

        self.assertFalse(hasattr(analyze, "__getattr__"))
        leaked = [
            name
            for name in (
                "call_gemini",
                "run_extractor",
                "build_cohort",
                "ANALYSIS_ENABLED",
                "write_cohort",
                "CUT_PROMPT",
                "sample_cohort_rows",
            )
            if name in vars(analyze)
        ]
        self.assertEqual(leaked, [], f"package still re-exports: {leaked}")


if __name__ == "__main__":
    unittest.main()
