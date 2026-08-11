"""Gating in analyze_session: extraction serialized, model calls overlapping."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

from capture import analyze


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


class TestAnalyzeGates(pgfixture.CohortStoreMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved = {
            "ANALYSIS_ENABLED": analyze.ANALYSIS_ENABLED,
            "run_extractor": analyze.run_extractor,
            "call_gemini": analyze.call_gemini,
            "extract": analyze.ANALYZE_EXTRACT_CONCURRENCY,
            "model": analyze.ANALYZE_MODEL_CONCURRENCY,
        }
        analyze.ANALYSIS_ENABLED = True
        # Force fresh semaphores bound to this test's loop.
        analyze._gate_loop = None
        self.addCleanup(self.restore)

    def restore(self):
        analyze.ANALYSIS_ENABLED = self._saved["ANALYSIS_ENABLED"]
        analyze.run_extractor = self._saved["run_extractor"]
        analyze.call_gemini = self._saved["call_gemini"]
        analyze.ANALYZE_EXTRACT_CONCURRENCY = self._saved["extract"]
        analyze.ANALYZE_MODEL_CONCURRENCY = self._saved["model"]
        analyze._gate_loop = None
        self.tmp.cleanup()

    def run_cohort(self, n=4, extract_n=1, model_n=3):
        analyze.ANALYZE_EXTRACT_CONCURRENCY = extract_n
        analyze.ANALYZE_MODEL_CONCURRENCY = model_n
        analyze._gate_loop = None

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

        analyze.run_extractor = fake_extract
        analyze.call_gemini = fake_gemini

        dirs = []
        for i in range(n):
            d = self.root / f"session{i}"
            d.mkdir()
            dirs.append(d)

        async def main():
            await asyncio.gather(
                *(analyze.analyze_session(d, ["chunk.json"], force=True) for d in dirs)
            )

        asyncio.run(main())
        return extract_probe, model_probe, dirs

    def test_extraction_stays_serialized_while_model_calls_overlap(self):
        extract, model, _ = self.run_cohort(n=4, extract_n=1, model_n=3)
        self.assertEqual(extract.peak, 1, "extraction must not run concurrently")
        self.assertGreater(model.peak, 1, "model calls should overlap")
        self.assertLessEqual(model.peak, 3)

    def test_model_concurrency_is_bounded_by_its_setting(self):
        _, model, _ = self.run_cohort(n=6, extract_n=1, model_n=2)
        self.assertLessEqual(model.peak, 2)

    def test_each_session_still_writes_its_results(self):
        _, _, dirs = self.run_cohort(n=3)
        for d in dirs:
            saved = analyze.read_analysis(d)
            self.assertEqual(saved["status"], "done")
            self.assertIn("What stands out", saved["text"])


if __name__ == "__main__":
    unittest.main()
