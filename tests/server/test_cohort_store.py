"""Cohort state in Postgres: the guarantees the JSON documents could not make.

Cohort membership, per-session status and the cut cache used to be JSON files
rewritten in full by concurrent writers. These pin what the move bought —
concurrent writers that do not drop each other, and reads that never see a torn
cohort.
"""

import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

from capture.analyze import cohort_state, cohorts, storage
from capture.analyze import store as analyze_store


def cohort(cohort_id="cohort-1", n=3, **extra):
    return {
        "id": cohort_id,
        "created_at": 1700000000000,
        "status": "idle",
        "sessions": [{"session_id": f"s{i}", "status": "queued"} for i in range(n)],
        **extra,
    }


class StoreCase(pgfixture.CohortStoreMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pool = self.root / "sessions"
        self.pool.mkdir()
        self.project_id = self._pg_project
        from capture import projects

        self.acme_id = projects.by_handle("acme").id
        self.addCleanup(self.tmp.cleanup)


class TestRoundTrip(StoreCase):
    async def test_a_cohort_survives_a_write_read_cycle_intact(self):
        written = await storage.write_cohort(
            self.project_id,
            cohort(a_n=5, b_n=5, label="Sample", slice={"outcome": "purchased"}),
        )
        got = await storage.read_cohort(self.project_id, "cohort-1")
        self.assertEqual(got["label"], "Sample")
        self.assertEqual(got["a_n"], 5)
        # Sampling fields ride in the fields blob; they must survive the trip.
        self.assertEqual(got["slice"], {"outcome": "purchased"})
        self.assertEqual([s["session_id"] for s in got["sessions"]], ["s0", "s1", "s2"])
        self.assertEqual(written["updated_at"], got["updated_at"])

    async def test_membership_order_is_preserved(self):
        ids = [f"s{i}" for i in range(30)]
        await storage.write_cohort(
            self.project_id,
            {**cohort(n=0), "sessions": [{"session_id": i} for i in ids]},
        )
        got = await storage.read_cohort(self.project_id, "cohort-1")
        self.assertEqual([s["session_id"] for s in got["sessions"]], ids)

    async def test_per_session_extras_are_not_dropped(self):
        await storage.write_cohort(
            self.project_id,
            {
                **cohort(n=0),
                "sessions": [{"session_id": "s0", "filter": "a", "qa_status": "ok"}],
            },
        )
        row = (await storage.read_cohort(self.project_id, "cohort-1"))["sessions"][0]
        self.assertEqual(row["filter"], "a")
        self.assertEqual(row["qa_status"], "ok")

    async def test_absent_batch_is_absent_rather_than_null(self):
        """Callers test `cohort.get("batch")` to mean "a job is in flight"."""
        await storage.write_cohort(self.project_id, cohort())
        self.assertNotIn("batch", await storage.read_cohort(self.project_id, "cohort-1"))

    async def test_batch_round_trips_and_clears(self):
        await storage.write_cohort(
            self.project_id, cohort(batch={"name": "batches/J1", "session_ids": ["s0"]})
        )
        self.assertEqual(
            (await storage.read_cohort(self.project_id, "cohort-1"))["batch"]["name"], "batches/J1"
        )
        stored = await storage.read_cohort(self.project_id, "cohort-1")
        stored.pop("batch")
        await storage.write_cohort(self.project_id, stored)
        self.assertNotIn("batch", await storage.read_cohort(self.project_id, "cohort-1"))

    async def test_progress_never_reaches_storage(self):
        """`progress` is a tally of `sessions`; a second copy on disk is the
        one any other writer updates out from under."""
        await storage.write_cohort(self.project_id, cohort(progress={"total": 3, "done": 3}))
        self.assertNotIn("progress", await storage.read_cohort(self.project_id, "cohort-1"))

    async def test_unknown_cohort_reads_as_none(self):
        self.assertIsNone(await storage.read_cohort(self.project_id, "cohort-nope"))

    async def test_a_bad_cohort_id_is_refused_not_looked_up(self):
        self.assertIsNone(await storage.read_cohort(self.project_id, "../../etc/passwd"))
        self.assertEqual(await storage.read_cuts(self.project_id, "../../etc/passwd"), {})

    async def test_listing_is_newest_first(self):
        await storage.write_cohort(self.project_id, cohort("cohort-old", created_at=1))
        await storage.write_cohort(self.project_id, cohort("cohort-new", created_at=2))
        self.assertEqual(
            [c["id"] for c in await storage.list_cohorts(self.project_id)],
            ["cohort-new", "cohort-old"],
        )

    async def test_delete_removes_membership_and_cuts(self):
        await storage.write_cohort(self.project_id, cohort())
        await storage.write_cut(self.project_id, "cohort-1", "k", {"n": 1})
        self.assertTrue(await cohorts.delete_cohort(self.project_id, "cohort-1"))
        self.assertIsNone(await storage.read_cohort(self.project_id, "cohort-1"))
        self.assertEqual(await storage.read_cuts(self.project_id, "cohort-1"), {})
        # Second delete reports nothing was there, so the API can 404.
        self.assertFalse(await cohorts.delete_cohort(self.project_id, "cohort-1"))

    async def test_created_at_is_not_clobbered_by_a_later_write(self):
        await storage.write_cohort(self.project_id, cohort(created_at=1700000000000))
        stored = await storage.read_cohort(self.project_id, "cohort-1")
        stored.pop("created_at")
        await storage.write_cohort(self.project_id, {**stored, "status": "running"})
        self.assertEqual(
            (await storage.read_cohort(self.project_id, "cohort-1"))["created_at"], 1700000000000
        )


class TestRowLevelUpdates(StoreCase):
    """Status, batch and label are columns. Updating them must not replace membership."""

    async def seed(self, **extra):
        await storage.write_cohort(
            self.project_id,
            cohort(
                sessions=[
                    {"session_id": "s0", "status": "queued", "filter": "a"},
                    {"session_id": "s1", "status": "queued", "filter": "b"},
                ],
                **extra,
            ),
        )
        return await storage.read_cohort(self.project_id, "cohort-1")

    async def test_set_cohort_status_does_not_rewrite_membership(self):
        stored = await self.seed()
        await storage.mark_cohort_session(
            self.project_id, stored, stored["sessions"][0], "done", analysis="analysis.json"
        )
        await storage.set_cohort_status(self.project_id, stored, "running")
        got = await storage.read_cohort(self.project_id, "cohort-1")
        self.assertEqual(got["status"], "running")
        self.assertEqual(got["sessions"][0]["status"], "done")
        self.assertEqual(got["sessions"][0]["filter"], "a")
        self.assertEqual(got["sessions"][1]["status"], "queued")

    async def test_set_cohort_status_keeps_the_callers_dict_in_step(self):
        stored = await self.seed()
        await storage.set_cohort_status(
            self.project_id, stored, "running", batch={"name": "batches/J1"}
        )
        self.assertEqual(stored["status"], "running")
        self.assertEqual(stored["batch"]["name"], "batches/J1")
        await storage.set_cohort_status(self.project_id, stored, "paused", clear_batch=True)
        self.assertEqual(stored["status"], "paused")
        self.assertNotIn("batch", stored)
        self.assertNotIn("batch", await storage.read_cohort(self.project_id, "cohort-1"))

    async def test_status_only_leaves_the_batch_handle_in_place(self):
        stored = await self.seed(batch={"name": "batches/J1"})
        await storage.set_cohort_status(self.project_id, stored, "running")
        self.assertEqual(
            (await storage.read_cohort(self.project_id, "cohort-1"))["batch"]["name"], "batches/J1"
        )

    async def test_rename_does_not_clobber_in_flight_marks(self):
        stored = await self.seed()
        await storage.mark_cohort_session(self.project_id, stored, stored["sessions"][1], "running")
        self.assertEqual(
            (await cohorts.rename_cohort(self.project_id, "cohort-1", "Spring"))["label"], "Spring"
        )
        got = await storage.read_cohort(self.project_id, "cohort-1")
        self.assertEqual(got["label"], "Spring")
        self.assertEqual(got["sessions"][1]["status"], "running")
        self.assertEqual(got["sessions"][0]["filter"], "a")


class TestBulkReads(StoreCase):
    async def test_analyses_and_jobs_are_read_in_one_query_each(self):
        for i in range(4):
            await storage.write_analysis(self.project_id, f"s{i}", {"text": f"up-{i}"})
        await storage.write_job(self.project_id, "s1", "running")
        await storage.write_job(self.project_id, "s2", "error", error="boom")
        analyses = await storage.read_analyses(self.project_id, ["s0", "s1", "s2", "s3", "missing"])
        jobs = await storage.read_jobs(self.project_id, ["s0", "s1", "s2", "missing"])
        self.assertEqual(
            {sid: a["text"] for sid, a in analyses.items()},
            {"s0": "up-0", "s1": "up-1", "s2": "up-2", "s3": "up-3"},
        )
        self.assertNotIn("missing", analyses)
        self.assertNotIn("s0", jobs)
        self.assertEqual(jobs["s1"]["status"], "running")
        self.assertEqual(jobs["s2"]["error"], "boom")

    async def test_empty_id_list_is_a_no_op(self):
        self.assertEqual(await storage.read_analyses(self.project_id, []), {})
        self.assertEqual(await storage.read_jobs(self.project_id, []), {})

    async def test_enrich_does_not_round_trip_per_session(self):
        await storage.write_cohort(self.project_id, cohort(n=5))
        for i in range(5):
            await storage.write_analysis(self.project_id, f"s{i}", {"text": "write-up"})
        with (
            patch.object(
                storage, "read_analysis", side_effect=AssertionError("per-session analysis")
            ),
            patch.object(storage, "read_job", side_effect=AssertionError("per-session job")),
        ):
            live = await cohort_state.enrich_cohort_status(
                self.project_id, await storage.read_cohort(self.project_id, "cohort-1")
            )
            insights = await cohort_state.attach_cohort_insights(self.project_id, live)
        self.assertEqual(live["progress"]["done"], 5)
        self.assertEqual(insights["sessions"][0]["text"], "write-up")


class TestConcurrency(StoreCase):
    """The reason for the move. Each of these lost data as JSON."""

    async def run_concurrent(self, make_coro, n):
        """Run `n` overlapping writers and return whatever went wrong.

        These used to be OS threads because the store was synchronous. The
        driver is async now, so real overlap comes from putting `n` coroutines
        in flight at once against the pool — which is also how the production
        pipeline reaches this code (`asyncio.gather` over a cohort's sessions),
        so the test now exercises the same shape it is guarding.
        """
        results = await asyncio.gather(*(make_coro(i) for i in range(n)), return_exceptions=True)
        return [repr(r) for r in results if isinstance(r, BaseException)]

    async def test_concurrent_session_marks_all_land(self):
        """apply_cohort_batch and extract_one mark sessions from inside loops.
        Under JSON each mark rewrote the whole document, so marks raced."""
        await storage.write_cohort(self.project_id, cohort(n=50))
        store = analyze_store.connect()
        errors = await self.run_concurrent(
            lambda i: analyze_store.update_session_status(
                store, self.project_id, "cohort-1", f"s{i}", status="done", analysis="analysis.json"
            ),
            50,
        )
        self.assertEqual(errors, [])
        rows = (await storage.read_cohort(self.project_id, "cohort-1"))["sessions"]
        self.assertEqual([r["status"] for r in rows], ["done"] * 50)
        self.assertEqual([r["session_id"] for r in rows], [f"s{i}" for i in range(50)])

    async def test_concurrent_cuts_do_not_drop_each_other(self):
        """The textbook lost update: read_cuts → mutate → write the whole map.
        _cut_tasks is keyed per cut, so two cuts of one cohort really do finish
        together."""
        await storage.write_cohort(self.project_id, cohort())
        errors = await self.run_concurrent(
            lambda i: storage.write_cut(self.project_id, "cohort-1", f"key-{i}", {"n": i}), 40
        )
        self.assertEqual(errors, [])
        cuts = await storage.read_cuts(self.project_id, "cohort-1")
        self.assertEqual(len(cuts), 40)
        self.assertEqual({k: v["n"] for k, v in cuts.items()}, {f"key-{i}": i for i in range(40)})

    async def test_a_read_never_sees_a_torn_cohort(self):
        """A truncated JSON write used to make a cohort vanish from the UI
        along with its in-flight batch job name. Every read is now all 50
        members or nothing in between — never None, never a partial membership."""
        await storage.write_cohort(self.project_id, cohort(n=50))
        stop = asyncio.Event()
        seen, errors = [], []

        async def writer():
            while not stop.is_set():
                try:
                    await storage.write_cohort(self.project_id, cohort(n=50, status="running"))
                except Exception as exc:
                    errors.append(repr(exc))

        async def reader():
            while not stop.is_set():
                try:
                    got = await storage.read_cohort(self.project_id, "cohort-1")
                    seen.append(None if got is None else len(got["sessions"]))
                except Exception as exc:
                    errors.append(repr(exc))

        # Two coroutines rather than two threads: the writer's DELETE+INSERT of
        # membership and the reader's SELECT now interleave on the event loop,
        # which is exactly how they meet in the server. The guarantee under test
        # is the transaction's, and it holds either way.
        tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
        await asyncio.sleep(1.0)
        stop.set()
        await asyncio.gather(*tasks)

        self.assertEqual(errors, [])
        self.assertTrue(seen, "reader never ran")
        self.assertEqual(set(seen), {50})


class TestSessionDeletion(StoreCase):
    """A membership row references a recording. Delete the recording and the
    row cannot be substantiated — and enrich_cohort_status trusts the stored
    status when it finds nothing on disk, so it would keep counting as done."""

    async def seed(self):
        for sid in ("s0", "s1"):
            (self.pool / sid).mkdir()
            await storage.write_analysis(self.project_id, sid, {"text": "write-up"})
        await storage.write_cohort(
            self.project_id,
            {
                "id": "c-A",
                "created_at": 1,
                "status": "done",
                "sessions": [
                    {"session_id": "s0", "status": "done", "analysis": "analysis.json"},
                    {"session_id": "s1", "status": "done", "analysis": "analysis.json"},
                ],
            },
        )
        await storage.write_cohort(
            self.project_id,
            {
                "id": "c-B",
                "created_at": 2,
                "status": "done",
                "sessions": [{"session_id": "s0", "status": "done", "analysis": "analysis.json"}],
            },
        )

    async def test_a_deleted_session_leaves_every_cohort_it_was_in(self):
        await self.seed()
        shutil.rmtree(self.pool / "s0")
        self.assertEqual(await storage.forget_session(self.project_id, "s0"), 2)
        self.assertEqual(
            [
                s["session_id"]
                for s in (await storage.read_cohort(self.project_id, "c-A"))["sessions"]
            ],
            ["s1"],
        )
        self.assertEqual((await storage.read_cohort(self.project_id, "c-B"))["sessions"], [])

    async def test_the_cohort_stops_counting_it_as_analyzed(self):
        """The failure this prevents: a cohort reporting a completed analysis
        for a session whose write-up no longer exists."""
        await self.seed()
        shutil.rmtree(self.pool / "s0")
        await storage.forget_session(self.project_id, "s0")
        live = await cohort_state.enrich_cohort_status(
            self.project_id, await storage.read_cohort(self.project_id, "c-A")
        )
        self.assertEqual(live["progress"]["total"], 1)
        self.assertEqual(live["progress"]["done"], 1)

    async def test_surviving_members_are_untouched(self):
        await self.seed()
        await storage.forget_session(self.project_id, "s0")
        row = (await storage.read_cohort(self.project_id, "c-A"))["sessions"][0]
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["status"], "done")

    async def test_forgetting_is_idempotent_and_scoped(self):
        await self.seed()
        self.assertEqual(await storage.forget_session(self.project_id, "s0"), 2)
        self.assertEqual(await storage.forget_session(self.project_id, "s0"), 0)
        self.assertEqual(await storage.forget_session(self.project_id, "never-existed"), 0)

    async def test_the_owning_cohorts_are_touched_so_pollers_notice(self):
        await self.seed()
        before = (await storage.read_cohort(self.project_id, "c-A"))["updated_at"]
        time.sleep(0.005)
        await storage.forget_session(self.project_id, "s0")
        self.assertGreater(
            (await storage.read_cohort(self.project_id, "c-A"))["updated_at"], before
        )

    async def test_cached_cuts_survive_a_membership_change(self):
        """A cut records what the model said when it ran; rewriting it to match
        a changed membership would be a second fabrication."""
        await self.seed()
        await storage.write_cut(self.project_id, "c-A", "k", {"headline": "as analyzed"})
        await storage.forget_session(self.project_id, "s0")
        self.assertEqual(
            await storage.read_cut(self.project_id, "c-A", "k"), {"headline": "as analyzed"}
        )

    async def test_forgetting_a_pool_drops_its_cohorts_and_cuts(self):
        await self.seed()
        await storage.write_cut(self.project_id, "c-A", "k", {"n": 1})
        await storage.forget_pool_cohorts(self.project_id)
        self.assertEqual(await storage.list_cohorts(self.project_id), [])
        self.assertEqual(await storage.read_cuts(self.project_id, "c-A"), {})

    async def test_another_pool_is_not_affected(self):
        await self.seed()
        await storage.write_cohort(
            self.acme_id,
            {"id": "c-A", "created_at": 1, "sessions": [{"session_id": "s0"}]},
        )
        await storage.forget_session(self.project_id, "s0")
        self.assertEqual(
            [s["session_id"] for s in (await storage.read_cohort(self.acme_id, "c-A"))["sessions"]],
            ["s0"],
        )


class TestConcurrentMarks(StoreCase):
    """run_cohort_job runs N coroutines that each own one session but share
    the cohort dict. Marking through write_cohort meant every mark rewrote all
    N rows from the writer's copy, so two marks landing together lost one."""

    async def cohort(self, n=4):
        await storage.write_cohort(
            self.project_id,
            {
                "id": "c",
                "created_at": 1,
                "status": "running",
                "sessions": [{"session_id": f"s{i}", "status": "queued"} for i in range(n)],
            },
        )
        return await storage.read_cohort(self.project_id, "c")

    async def test_marks_from_stale_copies_do_not_overwrite_each_other(self):
        await self.cohort()
        # Two holders of the same cohort, as the pipeline's coroutines have.
        a = await storage.read_cohort(self.project_id, "c")
        b = await storage.read_cohort(self.project_id, "c")
        await storage.mark_cohort_session(
            self.project_id, a, a["sessions"][0], "done", analysis="analysis.json"
        )
        await storage.mark_cohort_session(
            self.project_id, b, b["sessions"][1], "done", analysis="analysis.json"
        )

        got = [s["status"] for s in (await storage.read_cohort(self.project_id, "c"))["sessions"]]
        self.assertEqual(got, ["done", "done", "queued", "queued"])

    async def test_a_mark_keeps_the_callers_dict_in_step(self):
        cohort = await self.cohort()
        entry = cohort["sessions"][0]
        await storage.mark_cohort_session(self.project_id, cohort, entry, "error", error="boom")
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["error"], "boom")
        # And clearing it on the way to done drops the error in both places.
        await storage.mark_cohort_session(
            self.project_id, cohort, entry, "done", analysis="analysis.json"
        )
        self.assertNotIn("error", entry)
        stored = (await storage.read_cohort(self.project_id, "c"))["sessions"][0]
        self.assertEqual(stored["status"], "done")
        self.assertNotIn("error", stored)

    async def test_a_mark_does_not_disturb_the_sampling_tag(self):
        await storage.write_cohort(
            self.project_id,
            {
                "id": "c",
                "created_at": 1,
                "sessions": [{"session_id": "s0", "status": "queued", "filter": "a"}],
            },
        )
        cohort = await storage.read_cohort(self.project_id, "c")
        await storage.mark_cohort_session(
            self.project_id, cohort, cohort["sessions"][0], "done", analysis="analysis.json"
        )
        self.assertEqual(
            (await storage.read_cohort(self.project_id, "c"))["sessions"][0]["filter"], "a"
        )

    async def test_a_status_refresh_does_not_clobber_in_flight_marks(self):
        """GET used to persist enrich via write_cohort, which DELETE+INSERTed
        membership from a stale snapshot and dropped marks that landed in between."""
        snapshot = await self.cohort()
        live = {
            **snapshot,
            "status": "running",
            "sessions": [
                {**row, "status": "running" if i == 1 else row["status"]}
                for i, row in enumerate(snapshot["sessions"])
            ],
        }
        # The pipeline holds a different copy, the way GET's poll and extract do.
        pipeline = await storage.read_cohort(self.project_id, "c")
        await storage.mark_cohort_session(
            self.project_id, pipeline, pipeline["sessions"][0], "done", analysis="analysis.json"
        )
        self.assertTrue(await storage.persist_cohort_refresh(self.project_id, snapshot, live))

        got = await storage.read_cohort(self.project_id, "c")
        self.assertEqual(got["status"], "running")
        self.assertEqual(
            [s["status"] for s in got["sessions"]],
            ["done", "running", "queued", "queued"],
        )

    async def test_a_status_refresh_still_persists_corrected_rows(self):
        await storage.write_cohort(
            self.project_id,
            {
                "id": "c",
                "created_at": 1,
                "status": "running",
                "sessions": [
                    {"session_id": "s0", "status": "running", "filter": "a"},
                    {"session_id": "s1", "status": "queued"},
                ],
            },
        )
        stored = await storage.read_cohort(self.project_id, "c")
        live = {
            **stored,
            "status": "done",
            "sessions": [
                {**stored["sessions"][0], "status": "done", "analysis": "analysis.json"},
                stored["sessions"][1],
            ],
        }
        self.assertTrue(await storage.persist_cohort_refresh(self.project_id, stored, live))
        got = await storage.read_cohort(self.project_id, "c")
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["sessions"][0]["status"], "done")
        self.assertEqual(got["sessions"][0]["analysis"], "analysis.json")
        self.assertEqual(got["sessions"][0]["filter"], "a")
        self.assertEqual(got["sessions"][1]["status"], "queued")

    async def test_a_matching_view_does_not_touch_the_store(self):
        stored = await self.cohort()
        before = (await storage.read_cohort(self.project_id, "c"))["updated_at"]
        self.assertFalse(await storage.persist_cohort_refresh(self.project_id, stored, stored))
        self.assertEqual((await storage.read_cohort(self.project_id, "c"))["updated_at"], before)

    async def test_marking_a_session_that_left_the_cohort_is_reported(self):
        await self.cohort()
        store = analyze_store.connect()
        self.assertTrue(
            await analyze_store.update_session_status(
                store, self.project_id, "c", "s0", status="done"
            )
        )
        await storage.forget_session(self.project_id, "s0")
        self.assertFalse(
            await analyze_store.update_session_status(
                store, self.project_id, "c", "s0", status="done"
            ),
            "a mark aimed at a non-member must report that it matched nothing",
        )

    async def test_concurrent_marks_all_land(self):
        """Each writer reads its own copy of the cohort and marks one row.

        The failure this guards is last-writer-wins: under JSON every mark
        rewrote all 30 rows from the writer's stale snapshot, so marks landing
        together silently dropped each other. Thirty overlapping coroutines is
        the shape `run_cohort_job` actually produces.
        """
        await self.cohort(n=30)

        async def mark(i):
            copy = await storage.read_cohort(self.project_id, "c")
            await storage.mark_cohort_session(
                self.project_id, copy, copy["sessions"][i], "done", analysis="analysis.json"
            )

        results = await asyncio.gather(*(mark(i) for i in range(30)), return_exceptions=True)
        errors = [repr(r) for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [])
        got = [s["status"] for s in (await storage.read_cohort(self.project_id, "c"))["sessions"]]
        self.assertEqual(got, ["done"] * 30)


class TestResume(StoreCase):
    async def test_resumable_lookup_finds_running_cohorts_with_a_job_name(self):
        await storage.write_cohort(
            self.project_id, cohort("cohort-run", status="running", batch={"name": "batches/J1"})
        )
        await storage.write_cohort(self.project_id, cohort("cohort-idle", status="idle"))
        await storage.write_cohort(self.project_id, cohort("cohort-done", status="done"))
        # Running but never submitted: nothing to resume.
        await storage.write_cohort(self.project_id, cohort("cohort-nojob", status="running"))

        found = await cohorts.cohorts_needing_batch_resume()
        self.assertEqual([c["id"] for _, c in found], ["cohort-run"])
        self.assertEqual(found[0][0], self.project_id)

    async def test_resume_covers_project_pools(self):
        await storage.write_cohort(
            self.acme_id, cohort("cohort-acme", status="running", batch={"name": "batches/J2"})
        )
        found = await cohorts.cohorts_needing_batch_resume()
        self.assertEqual([(pid, c["id"]) for pid, c in found], [(self.acme_id, "cohort-acme")])

    async def test_pools_are_keyed_apart(self):
        """Two projects can hold the same cohort id without colliding."""
        await storage.write_cohort(self.project_id, cohort("cohort-1", n=2, label="default"))
        await storage.write_cohort(self.acme_id, cohort("cohort-1", n=7, label="acme"))
        self.assertEqual(
            (await storage.read_cohort(self.project_id, "cohort-1"))["label"], "default"
        )
        self.assertEqual(
            len((await storage.read_cohort(self.project_id, "cohort-1"))["sessions"]), 2
        )
        self.assertEqual((await storage.read_cohort(self.acme_id, "cohort-1"))["label"], "acme")
        self.assertEqual(len((await storage.read_cohort(self.acme_id, "cohort-1"))["sessions"]), 7)


if __name__ == "__main__":
    unittest.main()
