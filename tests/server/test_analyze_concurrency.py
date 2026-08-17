"""Gating in analyze_session: extraction serialized, model calls overlapping."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

from capture.analyze import gemini, runtime, session, storage


class GateProbe:
    """Records max observed concurrency for a stubbed async stage."""

    def __init__(self, hold=0.05):
        self.hold = hold
        self.active = 0
        self.peak = 0

    async def enter(self):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.hold)
        self.active -= 1


class TestAnalyzeGates(pgfixture.CohortStoreMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {
            "ANALYSIS_ENABLED": runtime.ANALYSIS_ENABLED,
            "extract": runtime.ANALYZE_EXTRACT_CONCURRENCY,
            "model": runtime.ANALYZE_MODEL_CONCURRENCY,
        }
        runtime.ANALYSIS_ENABLED = True
        # Force fresh semaphores bound to this test's loop.
        runtime._gate_loop = None
        self.addCleanup(self.restore)

    def restore(self):
        runtime.ANALYSIS_ENABLED = self._saved["ANALYSIS_ENABLED"]
        runtime.ANALYZE_EXTRACT_CONCURRENCY = self._saved["extract"]
        runtime.ANALYZE_MODEL_CONCURRENCY = self._saved["model"]
        runtime._gate_loop = None
        self.tmp.cleanup()

    async def run_cohort(self, n=4, extract_n=1, model_n=3):
        runtime.ANALYZE_EXTRACT_CONCURRENCY = extract_n
        runtime.ANALYZE_MODEL_CONCURRENCY = model_n
        runtime._gate_loop = None

        # Mirrors production: a model call outlasts the next extraction, which
        # is the only condition under which overlapping them buys anything.
        extract_probe = GateProbe(hold=0.02)
        model_probe = GateProbe(hold=0.10)

        async def fake_extract(session_dir, out_dir, chunk_names):
            await extract_probe.enter()
            return {"frames": [], "warnings": []}

        async def fake_gemini(digest, frames, meta=None):
            await model_probe.enter()
            return "## What stands out\n- something"

        dirs = []
        for i in range(n):
            d = self.root / f"session{i}"
            d.mkdir()
            dirs.append(d)
        project_id = self._pg_project

        async def main():
            # No `await` inside the generator: gather wants the coroutine
            # objects themselves, and awaiting here would both serialise them
            # and make this an async generator, which gather cannot unpack.
            await asyncio.gather(
                *(session.analyze_session(project_id, d, ["chunk.json"], force=True) for d in dirs)
            )

        with (
            patch.object(session, "run_extractor", fake_extract),
            patch.object(gemini, "call_gemini", fake_gemini),
        ):
            await main()
        return extract_probe, model_probe, dirs

    async def test_extraction_stays_serialized_while_model_calls_overlap(self):
        extract, model, _ = await self.run_cohort(n=4, extract_n=1, model_n=3)
        self.assertEqual(extract.peak, 1, "extraction must not run concurrently")
        self.assertGreater(model.peak, 1, "model calls should overlap")
        self.assertLessEqual(model.peak, 3)

    async def test_model_concurrency_is_bounded_by_its_setting(self):
        _, model, _ = await self.run_cohort(n=6, extract_n=1, model_n=2)
        self.assertLessEqual(model.peak, 2)

    async def test_each_session_still_writes_its_results(self):
        _, _, dirs = await self.run_cohort(n=3)
        for d in dirs:
            saved = await storage.read_analysis(self._pg_project, d.name)
            self.assertEqual(saved["status"], "done")
            self.assertIn("What stands out", saved["text"])


if __name__ == "__main__":
    unittest.main()
