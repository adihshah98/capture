"""Cohort state in SQLite: the guarantees the JSON documents could not make.

Cohort membership, per-session status and the cut cache used to be JSON files
rewritten in full by concurrent writers. These pin what the move bought —
concurrent writers that do not drop each other, reads that never see a torn
cohort — and the one-time import of the files left over from that layout.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze
import analyze_store


def cohort(cohort_id="cohort-1", n=3, **extra):
    return {
        "id": cohort_id,
        "created_at": 1700000000000,
        "status": "idle",
        "sessions": [{"session_id": f"s{i}", "status": "queued"} for i in range(n)],
        **extra,
    }


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pool = self.root / "sessions"
        self.pool.mkdir()
        self.addCleanup(self.tmp.cleanup)
        # Connections and the migration latch are cached per data root, and
        # every test gets a fresh one.
        self.addCleanup(analyze_store.close_all)
        self.addCleanup(analyze._migrated.clear)


class TestRoundTrip(StoreCase):
    def test_a_cohort_survives_a_write_read_cycle_intact(self):
        written = analyze.write_cohort(
            self.pool, cohort(purchased_n=5, all_n=5, label="Sample", slice={"outcome": "purchased"})
        )
        got = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual(got["label"], "Sample")
        self.assertEqual(got["purchased_n"], 5)
        # Sampling fields ride in the fields blob; they must survive the trip.
        self.assertEqual(got["slice"], {"outcome": "purchased"})
        self.assertEqual([s["session_id"] for s in got["sessions"]], ["s0", "s1", "s2"])
        self.assertEqual(written["updated_at"], got["updated_at"])

    def test_membership_order_is_preserved(self):
        ids = [f"s{i}" for i in range(30)]
        analyze.write_cohort(
            self.pool,
            {**cohort(n=0), "sessions": [{"session_id": i} for i in ids]},
        )
        got = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual([s["session_id"] for s in got["sessions"]], ids)

    def test_per_session_extras_are_not_dropped(self):
        analyze.write_cohort(
            self.pool,
            {
                **cohort(n=0),
                "sessions": [{"session_id": "s0", "filter": "purchased", "qa_status": "ok"}],
            },
        )
        row = analyze.read_cohort(self.pool, "cohort-1")["sessions"][0]
        self.assertEqual(row["filter"], "purchased")
        self.assertEqual(row["qa_status"], "ok")

    def test_absent_batch_is_absent_rather_than_null(self):
        """Callers test `cohort.get("batch")` to mean "a job is in flight"."""
        analyze.write_cohort(self.pool, cohort())
        self.assertNotIn("batch", analyze.read_cohort(self.pool, "cohort-1"))

    def test_batch_round_trips_and_clears(self):
        analyze.write_cohort(self.pool, cohort(batch={"name": "batches/J1", "session_ids": ["s0"]}))
        self.assertEqual(analyze.read_cohort(self.pool, "cohort-1")["batch"]["name"], "batches/J1")
        stored = analyze.read_cohort(self.pool, "cohort-1")
        stored.pop("batch")
        analyze.write_cohort(self.pool, stored)
        self.assertNotIn("batch", analyze.read_cohort(self.pool, "cohort-1"))

    def test_progress_never_reaches_storage(self):
        """`progress` is a tally of `sessions`; a second copy on disk is the
        one any other writer updates out from under."""
        analyze.write_cohort(self.pool, cohort(progress={"total": 3, "done": 3}))
        self.assertNotIn("progress", analyze.read_cohort(self.pool, "cohort-1"))

    def test_unknown_cohort_reads_as_none(self):
        self.assertIsNone(analyze.read_cohort(self.pool, "cohort-nope"))

    def test_a_bad_cohort_id_is_refused_not_looked_up(self):
        self.assertIsNone(analyze.read_cohort(self.pool, "../../etc/passwd"))
        self.assertEqual(analyze.read_cuts(self.pool, "../../etc/passwd"), {})

    def test_listing_is_newest_first(self):
        analyze.write_cohort(self.pool, cohort("cohort-old", created_at=1))
        analyze.write_cohort(self.pool, cohort("cohort-new", created_at=2))
        self.assertEqual(
            [c["id"] for c in analyze.list_cohorts(self.pool)],
            ["cohort-new", "cohort-old"],
        )

    def test_delete_removes_membership_and_cuts(self):
        analyze.write_cohort(self.pool, cohort())
        analyze.write_cut(self.pool, "cohort-1", "k", {"n": 1})
        self.assertTrue(analyze.delete_cohort(self.pool, "cohort-1"))
        self.assertIsNone(analyze.read_cohort(self.pool, "cohort-1"))
        self.assertEqual(analyze.read_cuts(self.pool, "cohort-1"), {})
        # Second delete reports nothing was there, so the API can 404.
        self.assertFalse(analyze.delete_cohort(self.pool, "cohort-1"))

    def test_created_at_is_not_clobbered_by_a_later_write(self):
        analyze.write_cohort(self.pool, cohort(created_at=1700000000000))
        stored = analyze.read_cohort(self.pool, "cohort-1")
        stored.pop("created_at")
        analyze.write_cohort(self.pool, {**stored, "status": "running"})
        self.assertEqual(
            analyze.read_cohort(self.pool, "cohort-1")["created_at"], 1700000000000
        )


class TestConcurrency(StoreCase):
    """The reason for the move. Each of these lost data as JSON."""

    def run_threads(self, fn, n):
        errors = []

        def wrapped(i):
            try:
                fn(i)
            except Exception as exc:  # noqa: BLE001 - the assertion is "none"
                errors.append(repr(exc))

        threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_concurrent_session_marks_all_land(self):
        """apply_cohort_batch and extract_one mark sessions from inside loops.
        Under JSON each mark rewrote the whole document, so marks raced."""
        analyze.write_cohort(self.pool, cohort(n=50))
        conn, key = analyze.cohort_store(self.pool)
        errors = self.run_threads(
            lambda i: analyze_store.update_session_status(
                conn, key, "cohort-1", f"s{i}", status="done", analysis="analysis.json"
            ),
            50,
        )
        self.assertEqual(errors, [])
        rows = analyze.read_cohort(self.pool, "cohort-1")["sessions"]
        self.assertEqual([r["status"] for r in rows], ["done"] * 50)
        self.assertEqual([r["session_id"] for r in rows], [f"s{i}" for i in range(50)])

    def test_concurrent_cuts_do_not_drop_each_other(self):
        """The textbook lost update: read_cuts → mutate → write the whole map.
        _cut_tasks is keyed per cut, so two cuts of one cohort really do finish
        together."""
        analyze.write_cohort(self.pool, cohort())
        errors = self.run_threads(
            lambda i: analyze.write_cut(self.pool, "cohort-1", f"key-{i}", {"n": i}), 40
        )
        self.assertEqual(errors, [])
        cuts = analyze.read_cuts(self.pool, "cohort-1")
        self.assertEqual(len(cuts), 40)
        self.assertEqual({k: v["n"] for k, v in cuts.items()}, {f"key-{i}": i for i in range(40)})

    def test_a_read_never_sees_a_torn_cohort(self):
        """A truncated JSON write made _read_cohort_file return None, so the
        cohort vanished from the UI along with its in-flight batch job name."""
        analyze.write_cohort(self.pool, cohort(n=50))
        stop = threading.Event()
        seen, errors = [], []

        def writer():
            while not stop.is_set():
                try:
                    analyze.write_cohort(self.pool, cohort(n=50, status="running"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))

        def reader():
            while not stop.is_set():
                try:
                    got = analyze.read_cohort(self.pool, "cohort-1")
                    seen.append(None if got is None else len(got["sessions"]))
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertTrue(seen, "reader never ran")
        # Every read is all 50 members or nothing in between — never None,
        # never a partial membership.
        self.assertEqual(set(seen), {50})


class TestMigration(StoreCase):
    def write_legacy(self, data, cuts=None):
        directory = self.pool.parent / "cohorts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{data['id']}.json").write_text(json.dumps(data))
        if cuts is not None:
            (directory / f"{data['id']}.cuts.json").write_text(json.dumps(cuts))
        return directory

    def test_json_cohorts_and_cuts_are_imported_then_removed(self):
        directory = self.write_legacy(
            cohort(purchased_n=5, label="Old", progress={"total": 3, "done": 3}),
            cuts={"cut-key": {"headline": "cached"}},
        )
        got = analyze.read_cohort(self.pool, "cohort-1")
        self.assertEqual(got["label"], "Old")
        self.assertEqual(got["purchased_n"], 5)
        self.assertEqual(len(got["sessions"]), 3)
        # The persisted view-only tally does not come along.
        self.assertNotIn("progress", got)
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", "cut-key"), {"headline": "cached"})
        # Nothing is left that a later reader could mistake for live state.
        self.assertFalse(directory.exists())

    def test_import_does_not_roll_back_newer_state(self):
        """A file that outlived its import must not overwrite the table."""
        self.write_legacy(cohort(status="idle", label="stale"))
        analyze.read_cohort(self.pool, "cohort-1")
        analyze.write_cohort(
            self.pool, {**analyze.read_cohort(self.pool, "cohort-1"), "status": "done"}
        )
        # Put the stale file back and force another import pass.
        self.write_legacy(cohort(status="idle", label="stale"))
        analyze._migrated.clear()
        self.assertEqual(analyze.read_cohort(self.pool, "cohort-1")["status"], "done")

    def test_an_unreadable_cohort_file_is_kept_not_deleted(self):
        directory = self.pool.parent / "cohorts"
        directory.mkdir(parents=True, exist_ok=True)
        broken = directory / "cohort-broken.json"
        broken.write_text("{ truncated")
        self.assertEqual(analyze.list_cohorts(self.pool), [])
        # Deleting it would destroy the only copy of state we failed to import.
        self.assertTrue(broken.is_file())

    def test_the_pre_multi_cohort_file_still_migrates(self):
        legacy = self.pool.parent / "cohort.json"
        legacy.write_text(json.dumps(cohort("cohort-1700000000000", label="Legacy")))
        got = analyze.read_cohort(self.pool, "cohort-1700000000000")
        self.assertIsNotNone(got)
        self.assertEqual(got["label"], "Legacy")
        self.assertFalse(legacy.exists())


class TestPoolLayout(unittest.TestCase):
    """data_root_for and pool_key must agree on where a pool sits, or a cohort
    is written under one key and read under another."""

    def test_default_and_project_pools(self):
        self.assertEqual(analyze.data_root_for(Path("/data/sessions")), Path("/data"))
        self.assertEqual(analyze.pool_key(Path("/data/sessions")), "")
        self.assertEqual(
            analyze.data_root_for(Path("/data/projects/acme/sessions")), Path("/data")
        )
        self.assertEqual(analyze.pool_key(Path("/data/projects/acme/sessions")), "acme")

    def test_a_data_root_named_projects_is_not_mistaken_for_one(self):
        pool = Path("/projects/sessions")
        self.assertEqual(analyze.data_root_for(pool), Path("/projects"))
        self.assertEqual(analyze.pool_key(pool), "")


class TestResume(StoreCase):
    def test_resumable_lookup_finds_running_cohorts_with_a_job_name(self):
        analyze.write_cohort(
            self.pool, cohort("cohort-run", status="running", batch={"name": "batches/J1"})
        )
        analyze.write_cohort(self.pool, cohort("cohort-idle", status="idle"))
        analyze.write_cohort(self.pool, cohort("cohort-done", status="done"))
        # Running but never submitted: nothing to resume.
        analyze.write_cohort(self.pool, cohort("cohort-nojob", status="running"))

        found = analyze.cohorts_needing_batch_resume(self.root)
        self.assertEqual([c["id"] for _, c in found], ["cohort-run"])
        self.assertEqual(found[0][0], self.pool)

    def test_resume_covers_project_pools(self):
        project = self.root / "projects" / "acme" / "sessions"
        project.mkdir(parents=True)
        analyze.write_cohort(
            project, cohort("cohort-acme", status="running", batch={"name": "batches/J2"})
        )
        found = analyze.cohorts_needing_batch_resume(self.root)
        self.assertEqual([(p, c["id"]) for p, c in found], [(project, "cohort-acme")])

    def test_pools_are_keyed_apart(self):
        """Two pools can hold the same cohort id without colliding."""
        project = self.root / "projects" / "acme" / "sessions"
        project.mkdir(parents=True)
        analyze.write_cohort(self.pool, cohort("cohort-1", n=2, label="default"))
        analyze.write_cohort(project, cohort("cohort-1", n=7, label="acme"))
        self.assertEqual(analyze.read_cohort(self.pool, "cohort-1")["label"], "default")
        self.assertEqual(len(analyze.read_cohort(self.pool, "cohort-1")["sessions"]), 2)
        self.assertEqual(analyze.read_cohort(project, "cohort-1")["label"], "acme")
        self.assertEqual(len(analyze.read_cohort(project, "cohort-1")["sessions"]), 7)


if __name__ == "__main__":
    unittest.main()
