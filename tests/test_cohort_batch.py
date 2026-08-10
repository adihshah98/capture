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


class TestPersistableCohort(unittest.TestCase):
    """enrich_cohort_status is a read-time view; only part of it belongs on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = Path(self.tmp.name) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def store(self, **fields) -> dict:
        cohort = {
            "id": "cohort-1",
            "created_at": 1,
            "status": "idle",
            "sessions": [{"session_id": "s1", "status": "queued"}],
            **fields,
        }
        analyze.write_cohort(self.pool, cohort)
        return analyze.read_cohort(self.pool, "cohort-1")

    def round_trip(self, stored: dict) -> tuple:
        """(response, persisted) for one enrich-then-write cycle."""
        live = analyze.enrich_cohort_status(self.pool, stored)
        analyze.write_cohort(self.pool, analyze.persistable_cohort(live, stored))
        return live, analyze.read_cohort(self.pool, "cohort-1")

    def test_progress_is_returned_but_never_stored(self):
        live, saved = self.round_trip(self.store())
        self.assertEqual(live["progress"]["total"], 1)
        self.assertNotIn("progress", saved)

    def test_a_guessed_selection_is_not_frozen_into_the_file(self):
        # No sampling fields at all: the backfill can only guess, and the guess
        # ("latest 0 purchased + 0 non-purchase") would be permanent once
        # written, because enrich only backfills a missing selection.
        stored = self.store(purchased_n=0, all_n=0, any_n=0, a_n=0, b_n=0)
        live, saved = self.round_trip(stored)
        self.assertIn("0 purchased + 0 non-purchase", live["selection"])
        self.assertIsNone(saved.get("selection"))

    def test_a_real_selection_survives(self):
        text = "Chosen as the latest 50 purchased + 50 non-purchase sessions on /x"
        _, saved = self.round_trip(self.store(selection=text))
        self.assertEqual(saved["selection"], text)

    def test_refreshed_status_is_still_persisted(self):
        # The whole reason callers write after enriching: a crashed runner
        # leaves rows claiming "running" that only a re-read corrects.
        session_dir = self.pool / "s1"
        session_dir.mkdir()
        (session_dir / analyze.ANALYSIS_FILE).write_text(
            json.dumps({"status": "done", "text": "write-up"})
        )
        stored = self.store(sessions=[{"session_id": "s1", "status": "running"}])
        _, saved = self.round_trip(stored)
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["sessions"][0]["status"], "done")

    def test_write_ups_are_still_kept_out_of_the_file(self):
        session_dir = self.pool / "s1"
        session_dir.mkdir()
        (session_dir / analyze.ANALYSIS_FILE).write_text(
            json.dumps({"status": "done", "text": "write-up", "model": "m"})
        )
        stored = self.store()
        enriched = analyze.attach_cohort_insights(
            self.pool, analyze.enrich_cohort_status(self.pool, stored)
        )
        analyze.write_cohort(
            self.pool, analyze.persistable_cohort(enriched, stored)
        )
        saved = analyze.read_cohort(self.pool, "cohort-1")
        self.assertNotIn("text", saved["sessions"][0])
        self.assertNotIn("progress", saved)

    def test_without_a_stored_cohort_it_only_drops_the_derived_view(self):
        live = analyze.enrich_cohort_status(self.pool, self.store())
        out = analyze.persistable_cohort(live)
        self.assertNotIn("progress", out)
        self.assertIn("selection", out)


if __name__ == "__main__":
    unittest.main()
