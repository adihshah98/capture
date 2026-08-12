"""Cohort Batch path: submit → poll → write analysis.json without sync Gemini."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

from capture import analyze


class TestCohortBatch(pgfixture.CohortStoreMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = Path(self.tmp.name) / "sessions"
        self.pool.mkdir()
        self._saved = {
            "ANALYSIS_ENABLED": analyze.runtime.ANALYSIS_ENABLED,
            "ANALYZE_BATCH": analyze.runtime.ANALYZE_BATCH,
        }
        analyze.runtime.ANALYSIS_ENABLED = True
        analyze.runtime.ANALYZE_BATCH = True
        analyze.runtime._gate_loop = None
        # Every test in this class drives the real pipeline, so each one is an
        # enforcement pass over COHORT_TRANSITIONS: cohort_transition logs
        # rather than raises, and this turns the log into a failure.
        analyze._undeclared_transitions.clear()
        self.addCleanup(self.assert_transitions_declared)
        self.addCleanup(self.restore)

    def assert_transitions_declared(self):
        self.assertEqual(
            analyze._undeclared_transitions,
            [],
            "the pipeline made a status move COHORT_TRANSITIONS does not declare",
        )

    def restore(self):
        for key, value in self._saved.items():
            setattr(analyze.runtime, key, value)
        analyze.runtime._gate_loop = None
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

        # Mirrors wait_gemini_batch's real signature: a fake that omits a
        # parameter the production function grows will not fail loudly here.
        async def fake_wait(job_name, *, poll_s=None, max_wait_s=None):
            self.assertEqual(job_name, "batches/test-job")
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

        async def boom(*_a, **_k):
            raise AssertionError("sync Gemini must not run when batch succeeds")

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
        with (
            patch.object(analyze.session, "run_extractor", fake_extract),
            patch.object(analyze.gemini, "submit_prepared_gemini_batch", fake_submit),
            patch.object(analyze.gemini, "wait_gemini_batch", fake_wait),
            patch.object(analyze.gemini, "parse_batch_results", fake_parse),
            patch.object(analyze.gemini, "call_gemini", boom),
        ):
            final = asyncio.run(analyze.run_cohort_job(self.pool, cohort, force=True))

        self.assertEqual(final["status"], "done")
        self.assertEqual(final["progress"]["done"], 2)
        self.assertNotIn("batch", final)
        self.assertIn("What stands out", analyze.read_analysis(a)["text"])
        self.assertIn("What stands out", analyze.read_analysis(b)["text"])

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


class TestBatchIsNeverPaidForTwice(pgfixture.CohortStoreMixin, unittest.TestCase):
    """Once a batch job exists it is billable, so only submission may fall back."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pool = self.root / "sessions"
        self.pool.mkdir(parents=True)
        self._saved = {
            "ANALYSIS_ENABLED": analyze.runtime.ANALYSIS_ENABLED,
            "ANALYZE_BATCH": analyze.runtime.ANALYZE_BATCH,
            "_cohort_sync_model": analyze._cohort_sync_model,
        }
        analyze.runtime.ANALYSIS_ENABLED = True
        analyze.runtime.ANALYZE_BATCH = True
        analyze.runtime._gate_loop = None
        self.sync_calls = 0

        async def fake_extract(session_dir, out_dir, chunk_names):
            out_dir.mkdir(parents=True, exist_ok=True)
            frame = out_dir / "f000.jpg"
            frame.write_bytes(b"jpeg")
            digest = {"frames": [{"atMs": 0, "file": str(frame)}], "warnings": []}
            (out_dir / "digest.json").write_text(json.dumps(digest))
            return digest

        async def count_sync(pool, cohort, prepared):
            self.sync_calls += 1
            for entry, _ in prepared:
                entry["status"] = "done"

        self._fake_extract = fake_extract
        analyze._cohort_sync_model = count_sync
        self.addCleanup(self.restore)

    def restore(self):
        for key, value in self._saved.items():
            target = analyze.runtime if key in {"ANALYSIS_ENABLED", "ANALYZE_BATCH"} else analyze
            setattr(target, key, value)
        analyze.runtime._gate_loop = None
        self.tmp.cleanup()

    def cohort(self, n=2) -> dict:
        sessions = []
        for i in range(n):
            sid = f"{chr(97 + i) * 3}" + "x" * 29
            d = self.pool / sid
            d.mkdir()
            (d / "1700000000000-abcd1234-abcdef.json").write_text("[]")
            sessions.append({"session_id": sid, "status": "queued"})
        cohort = {
            "id": "cohort-1",
            "created_at": 1,
            "status": "idle",
            "sessions": sessions,
        }
        analyze.write_cohort(self.pool, cohort)
        return cohort

    def submits_as(self, job_name="batches/JOB1"):
        async def fake_submit(prepared, *, display_name, work_dir=None, pool=None):
            return job_name

        return patch.object(analyze.gemini, "submit_prepared_gemini_batch", fake_submit)

    def extractor_patch(self):
        return patch.object(analyze.session, "run_extractor", self._fake_extract)

    def test_a_batch_left_in_flight_is_resumed_not_rerun(self):
        # The expensive mistake: the job was created and is still running, so
        # re-sending the sessions interactively pays for the same work twice.
        async def never_finishes(name, *, poll_s=None, max_wait_s=None):
            raise analyze.BatchStillRunning("deadline")

        with (
            self.extractor_patch(),
            self.submits_as(),
            patch.object(analyze.gemini, "wait_gemini_batch", never_finishes),
        ):
            asyncio.run(analyze.run_cohort_job(self.pool, dict(self.cohort())))

        saved = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual(self.sync_calls, 0, "must not re-run a live batch")
        self.assertEqual((saved.get("batch") or {}).get("name"), "batches/JOB1")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(
            [c["id"] for _, c in analyze.cohorts_needing_batch_resume(self.root)],
            ["cohort-1"],
        )

    def test_a_failure_reading_results_also_keeps_the_job(self):
        # Same reasoning for a crash in parse/write: the job is fine, only our
        # collection of it failed.
        async def explodes(name, *, poll_s=None, max_wait_s=None):
            raise RuntimeError("SDK blew up parsing the response")

        with (
            self.extractor_patch(),
            self.submits_as(),
            patch.object(analyze.gemini, "wait_gemini_batch", explodes),
        ):
            asyncio.run(analyze.run_cohort_job(self.pool, dict(self.cohort())))

        saved = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual(self.sync_calls, 0)
        self.assertEqual((saved.get("batch") or {}).get("name"), "batches/JOB1")

    def test_a_failed_submit_still_falls_back_to_sync(self):
        # Nothing was created, so nothing has been paid for — the fallback is
        # the whole point and must survive.
        async def bad_submit(prepared, *, display_name, work_dir=None, pool=None):
            raise RuntimeError("batch API unavailable")

        with (
            self.extractor_patch(),
            patch.object(analyze.gemini, "submit_prepared_gemini_batch", bad_submit),
        ):
            asyncio.run(analyze.run_cohort_job(self.pool, dict(self.cohort())))

        saved = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual(self.sync_calls, 1)
        self.assertIsNone(saved.get("batch"))

    def test_a_genuinely_failed_batch_still_errors_its_sessions(self):
        async def failed(name, *, poll_s=None, max_wait_s=None):
            return SimpleNamespace(
                state=SimpleNamespace(name="JOB_STATE_FAILED"), error=None, dest=None
            )

        with (
            self.extractor_patch(),
            self.submits_as(),
            patch.object(analyze.gemini, "wait_gemini_batch", failed),
        ):
            asyncio.run(analyze.run_cohort_job(self.pool, dict(self.cohort(n=1))))

        saved = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual(self.sync_calls, 0)
        self.assertEqual(saved["sessions"][0]["status"], "error")
        self.assertIn("JOB_STATE_FAILED", saved["sessions"][0]["error"])

    def test_a_resume_that_cannot_collect_leaves_the_cohort_resumable(self):
        # resume_cohort_batch runs from an unawaited startup task, so raising
        # would strand the cohort as "running" with nothing watching it.
        sid = "aaa" + "x" * 29
        (self.pool / sid).mkdir()
        analyze.write_cohort(
            self.pool,
            {
                "id": "cohort-1",
                "created_at": 1,
                "status": "running",
                "batch": {"name": "batches/JOB1", "session_ids": [sid]},
                "sessions": [{"session_id": sid, "status": "running"}],
            },
        )

        async def never_finishes(name, *, poll_s=None, max_wait_s=None):
            raise analyze.BatchStillRunning("deadline")

        with patch.object(analyze.gemini, "wait_gemini_batch", never_finishes):
            asyncio.run(
                analyze.resume_cohort_batch(self.pool, analyze.read_cohort(self.pool, "cohort-1"))
            )

        saved = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual((saved.get("batch") or {}).get("name"), "batches/JOB1")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(
            [c["id"] for _, c in analyze.cohorts_needing_batch_resume(self.root)],
            ["cohort-1"],
        )


class TestWaitGeminiBatch(pgfixture.CohortStoreMixin, unittest.TestCase):
    """The poll is bounded and tolerates blips, so neither hangs nor escalates."""

    def setUp(self):
        super().setUp()
        import google.genai as genai

        self._client = genai.Client
        self.addCleanup(lambda: setattr(genai, "Client", self._client))
        self.genai = genai

    def script(self, steps) -> SimpleNamespace:
        state = {"rest": list(steps), "last": None, "calls": 0}

        class Batches:
            async def get(_self, *, name):
                state["calls"] += 1
                if state["rest"]:
                    state["last"] = state["rest"].pop(0)
                item = state["last"]
                if isinstance(item, Exception):
                    raise item
                return SimpleNamespace(state=SimpleNamespace(name=item))

        batches = Batches()

        class Client:
            def __init__(self, **kwargs):
                self.aio = SimpleNamespace(batches=batches)

        self.genai.Client = Client
        return state

    def test_a_transient_poll_failure_does_not_end_the_wait(self):
        # Escalating here would drop into the fallback and re-bill the cohort.
        state = self.script([RuntimeError("503"), RuntimeError("503"), "JOB_STATE_SUCCEEDED"])
        job = asyncio.run(analyze.wait_gemini_batch("j", poll_s=0.001))
        self.assertEqual(job.state.name, "JOB_STATE_SUCCEEDED")
        self.assertEqual(state["calls"], 3)

    def test_polling_that_keeps_failing_reports_the_job_as_still_running(self):
        self.script([RuntimeError("503")] * 20)
        with self.assertRaises(analyze.BatchStillRunning):
            asyncio.run(analyze.wait_gemini_batch("j", poll_s=0.001))

    def test_a_stuck_job_gives_up_at_the_deadline(self):
        self.script(["JOB_STATE_PENDING"] * 500)
        with self.assertRaises(analyze.BatchStillRunning):
            asyncio.run(analyze.wait_gemini_batch("j", poll_s=0.001, max_wait_s=0.05))

    def test_zero_still_means_wait_forever(self):
        self.script(["JOB_STATE_PENDING", "JOB_STATE_SUCCEEDED"])
        job = asyncio.run(analyze.wait_gemini_batch("j", poll_s=0.001, max_wait_s=0))
        self.assertEqual(job.state.name, "JOB_STATE_SUCCEEDED")

    def test_terminal_states_return_the_job(self):
        for name in ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_EXPIRED"):
            with self.subTest(state=name):
                self.script([name])
                job = asyncio.run(analyze.wait_gemini_batch("j", poll_s=0.001))
                self.assertEqual(job.state.name, name)


class TestPersistableCohort(pgfixture.CohortStoreMixin, unittest.TestCase):
    """enrich_cohort_status is a read-time view; only part of it belongs on disk."""

    def setUp(self):
        super().setUp()
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
        analyze.write_analysis(session_dir, {"status": "done", "text": "write-up"})
        stored = self.store(sessions=[{"session_id": "s1", "status": "running"}])
        _, saved = self.round_trip(stored)
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["sessions"][0]["status"], "done")

    def test_write_ups_are_still_kept_out_of_the_file(self):
        session_dir = self.pool / "s1"
        session_dir.mkdir()
        analyze.write_analysis(session_dir, {"status": "done", "text": "write-up", "model": "m"})
        stored = self.store()
        enriched = analyze.attach_cohort_insights(
            self.pool, analyze.enrich_cohort_status(self.pool, stored)
        )
        analyze.write_cohort(self.pool, analyze.persistable_cohort(enriched, stored))
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
