"""Cohort Batch path: submit → poll → write analysis.json without sync Gemini."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze


class TestCohortBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = Path(self.tmp.name) / "sessions"
        self.pool.mkdir()
        self._saved = {
            "ANALYSIS_ENABLED": analyze.ANALYSIS_ENABLED,
            "ANALYZE_BATCH": analyze.ANALYZE_BATCH,
            "run_extractor": analyze.run_extractor,
            "submit_prepared_gemini_batch": analyze.submit_prepared_gemini_batch,
            "wait_gemini_batch": analyze.wait_gemini_batch,
            "parse_batch_results": analyze.parse_batch_results,
            "call_gemini": analyze.call_gemini,
        }
        analyze.ANALYSIS_ENABLED = True
        analyze.ANALYZE_BATCH = True
        analyze._gate_loop = None
        self.addCleanup(self.restore)

    def restore(self):
        for key, value in self._saved.items():
            setattr(analyze, key, value)
        analyze._gate_loop = None
        self.tmp.cleanup()

    def _session(self, sid):
        d = self.pool / sid
        d.mkdir()
        (d / "1700000000000-abcd1234-abcdef.json").write_text("[]")
        return d

    def test_run_cohort_job_uses_batch_and_keeps_other_cohorts(self):
        older = analyze.write_cohort(
            self.pool,
            {
                "id": "cohort-old",
                "label": "keep me",
                "status": "done",
                "sessions": [{"session_id": "other", "filter": "all", "status": "done"}],
            },
        )
        a = self._session("aaa" + "x" * 29)
        b = self._session("bbb" + "x" * 29)

        async def fake_extract(session_dir, out_dir, chunk_names):
            out_dir.mkdir(parents=True, exist_ok=True)
            frame = out_dir / "f000.jpg"
            frame.write_bytes(b"jpeg")
            digest = {
                "frames": [{"atMs": 0, "file": str(frame)}],
                "warnings": [],
                "session": {"durationMs": 6000},
            }
            (out_dir / "digest.json").write_text(json.dumps(digest))
            return digest

        async def fake_submit(prepared, *, display_name, work_dir=None, pool=None):
            self.assertEqual(len(prepared), 2)
            self.assertTrue(display_name.startswith("capture-"))
            # Without the pool the batch path cannot resolve a session dir, so
            # every write-up in the batch would lose its campaign metadata.
            self.assertIsNotNone(pool)
            return "batches/test-job"

        async def fake_wait(name, *, poll_s=None):
            self.assertEqual(name, "batches/test-job")
            return SimpleNamespace(
                state=SimpleNamespace(name="JOB_STATE_SUCCEEDED"),
                dest=SimpleNamespace(inlined_responses=[], file_name=None),
                error=None,
            )

        def fake_parse(job):
            return {
                a.name: {"text": "## What stands out\n- a"},
                b.name: {"text": "## What stands out\n- b"},
            }

        analyze.run_extractor = fake_extract
        analyze.submit_prepared_gemini_batch = fake_submit
        analyze.wait_gemini_batch = fake_wait
        analyze.parse_batch_results = fake_parse

        async def boom(*_a, **_k):
            raise AssertionError("sync Gemini must not run when batch succeeds")

        analyze.call_gemini = boom

        cohort = {
            "id": "cohort-new",
            "label": "new",
            "status": "queued",
            "sessions": [
                {"session_id": a.name, "filter": "purchased", "status": "queued"},
                {"session_id": b.name, "filter": "all", "status": "queued"},
            ],
        }
        analyze.write_cohort(self.pool, cohort)
        final = asyncio.run(analyze.run_cohort_job(self.pool, cohort, force=True))

        self.assertEqual(final["status"], "done")
        self.assertEqual(final["progress"]["done"], 2)
        self.assertNotIn("batch", final)
        self.assertIn("What stands out", (a / "analysis.json").read_text())
        self.assertIn("What stands out", (b / "analysis.json").read_text())

        still = analyze.read_cohort(self.pool, older["id"])
        self.assertEqual(still["label"], "keep me")
        self.assertEqual(len(still["sessions"]), 1)

    def test_parse_batch_results_reads_inlined_metadata(self):
        response = SimpleNamespace(text="  hello from batch  ")
        job = SimpleNamespace(
            dest=SimpleNamespace(
                inlined_responses=[
                    SimpleNamespace(
                        metadata={"session_id": "s1"},
                        response=response,
                        error=None,
                    ),
                    SimpleNamespace(
                        metadata={"session_id": "s2"},
                        response=None,
                        error=SimpleNamespace(message="nope"),
                    ),
                ],
                file_name=None,
            )
        )
        out = analyze.parse_batch_results(job)
        self.assertEqual(out["s1"]["text"], "hello from batch")
        self.assertEqual(out["s2"]["error"], "nope")


if __name__ == "__main__":
    unittest.main()
