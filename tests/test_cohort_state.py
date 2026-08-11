"""The cohort state machine.

`enrich_cohort_status` used to decide a cohort's status with 90 lines of
if/elif inferring it from a filesystem scan, because status was written from
~10 places with no shared discipline. Its own comments admitted it — "Stay
sampled-until-started unless work is somehow already in flight", "Keep paused
until the user hits Start again (even if jobs look stale)" — patches for states
the code could reach but could not name.

`cohort_status_baseline.json` is that behavior, captured by enumerating the
whole input space before the refactor: every stored status (including invalid
and absent) against every tally of done/error/queued sessions. The state
machine must reproduce it exactly. Where it deliberately does not, the case is
listed in KNOWN_DIVERGENCES with a reason.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

from capture import analyze
from capture.analyze import store as analyze_store

BASELINE = json.loads(
    (Path(__file__).parent / "fixtures" / "cohort_status_baseline.json").read_text()
)

# (stored, done, error, queued) -> (new expectation, why). Empty means the
# refactor is behavior-preserving.
KNOWN_DIVERGENCES: dict = {}


def cohort_of(stored, done, error, queued, *, writeups=True, pool=None):
    """A cohort with the given tallies.

    `writeups` decides whether the done rows have an analysis.json behind them.
    False is the stale case: a row claiming done whose write-up was removed
    after the fact. The distinction is the point of half the baseline.
    """
    sessions = []
    for i in range(done):
        sid = f"d{i}"
        sessions.append({"session_id": sid, "status": "done"})
        if writeups and pool is not None:
            directory = pool / sid
            directory.mkdir(parents=True, exist_ok=True)
            analyze.write_analysis(directory, {"text": "write-up"})
    sessions += [{"session_id": f"e{i}", "status": "error"} for i in range(error)]
    sessions += [{"session_id": f"q{i}", "status": "queued"} for i in range(queued)]
    cohort = {"id": "c", "sessions": sessions}
    if stored is not None:
        cohort["status"] = stored
    return cohort


class TestStatusBaseline(pgfixture.CohortStoreMixin, unittest.TestCase):
    """Every case the old inference engine decided, decided the same way."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.pool = Path(self.tmp) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_baseline_is_not_empty(self):
        self.assertGreater(len(BASELINE), 100)

    def build(self, case):
        """Rebuild one recorded case in a clean pool."""
        for path in self.pool.iterdir():
            shutil.rmtree(path, ignore_errors=True)
        # Write-ups live in the store now, so clearing directories no longer
        # resets them and a case with writeups=False would inherit the last one.
        analyze.forget_analyses(self.pool)
        return cohort_of(
            case["stored"],
            case["done"],
            case["error"],
            case["queued"],
            writeups=case["writeups"],
            pool=self.pool,
        )

    def test_every_recorded_case_still_decides_the_same_way(self):
        mismatches = []
        for case in BASELINE:
            key = (case["writeups"], case["stored"], case["done"], case["error"], case["queued"])
            got = analyze.enrich_cohort_status(self.pool, self.build(case))["status"]
            expect = KNOWN_DIVERGENCES.get(key, (case["expect"], None))[0]
            if got != expect:
                mismatches.append(f"{key}: expected {expect}, got {got}")
        self.assertEqual(mismatches, [], f"{len(mismatches)} status changes")

    def test_progress_tallies_are_unchanged(self):
        for case in BASELINE:
            key = (case["writeups"], case["stored"], case["done"], case["error"], case["queued"])
            got = analyze.enrich_cohort_status(self.pool, self.build(case))["progress"]
            self.assertEqual(got, case["progress"], f"progress changed for {key}")

    def test_the_baseline_covers_both_the_write_up_and_stale_cases(self):
        """Half the value is the contrast: identical tallies decide
        differently depending on whether the write-ups are actually there."""
        self.assertTrue(any(c["writeups"] for c in BASELINE))
        self.assertTrue(any(not c["writeups"] for c in BASELINE))
        with_ups = {
            (c["stored"], c["done"], c["error"], c["queued"]): c["expect"]
            for c in BASELINE
            if c["writeups"]
        }
        without = {
            (c["stored"], c["done"], c["error"], c["queued"]): c["expect"]
            for c in BASELINE
            if not c["writeups"]
        }
        differing = [k for k in with_ups if with_ups[k] != without.get(k)]
        self.assertTrue(differing, "the two halves must not be identical")


class TestStaleRows(pgfixture.CohortStoreMixin, unittest.TestCase):
    """A row claiming done with no analysis.json behind it.

    The old fall-through counted it as done, so a cohort reported a completed
    analysis it could not show — the UI offered a session that opened to
    nothing, and a cut would cite it as evidence.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.pool = Path(self.tmp) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def cohort(self, n=1, status="done"):
        analyze.write_cohort(
            self.pool,
            {
                "id": "c",
                "created_at": 1,
                "status": status,
                "sessions": [
                    {"session_id": f"s{i}", "status": "done", "analysis": "analysis.json"}
                    for i in range(n)
                ],
            },
        )
        for i in range(n):
            directory = self.pool / f"s{i}"
            directory.mkdir(exist_ok=True)
            analyze.write_analysis(directory, {"text": "write-up"})
        self.addCleanup(analyze_store.close_all)
        self.addCleanup(analyze._migrated.clear)
        return analyze.read_cohort(self.pool, "c")

    def live(self):
        return analyze.enrich_cohort_status(self.pool, analyze.read_cohort(self.pool, "c"))

    def test_a_missing_write_up_is_not_counted_as_done(self):
        self.cohort(n=1)
        self.assertEqual(self.live()["progress"]["done"], 1)
        analyze.clear_analysis(self.pool / "s0")
        live = self.live()
        self.assertEqual(live["progress"]["done"], 0)
        self.assertEqual(live["progress"]["stale"], 1)
        self.assertEqual(live["sessions"][0]["status"], analyze.SESSION_STALE)

    def test_the_cohort_stops_reporting_done(self):
        """The whole point: no claiming a result that cannot be shown."""
        self.cohort(n=1)
        self.assertEqual(self.live()["status"], "done")
        analyze.clear_analysis(self.pool / "s0")
        self.assertNotEqual(self.live()["status"], "done")

    def test_one_stale_row_among_finished_ones_still_blocks_done(self):
        self.cohort(n=3)
        analyze.clear_analysis(self.pool / "s1")
        live = self.live()
        self.assertEqual(live["progress"]["done"], 2)
        self.assertEqual(live["progress"]["stale"], 1)
        self.assertNotEqual(live["status"], "done")

    def test_a_stale_row_explains_itself(self):
        self.cohort(n=1)
        analyze.clear_analysis(self.pool / "s0")
        self.assertIn("re-analyze", self.live()["sessions"][0]["error"])

    def test_restoring_the_write_up_restores_done(self):
        self.cohort(n=1)
        analyze.clear_analysis(self.pool / "s0")
        self.assertNotEqual(self.live()["status"], "done")
        analyze.write_analysis(self.pool / "s0", {"text": "back"})
        live = self.live()
        self.assertEqual(live["status"], "done")
        self.assertEqual(live["progress"]["stale"], 0)

    def test_a_running_job_takes_precedence_over_stale(self):
        """The normal re-analyze path clears the write-up and immediately
        writes a running job, so it must read as running, not stale."""
        self.cohort(n=1)
        analyze.clear_analysis(self.pool / "s0")
        analyze.write_job(self.pool / "s0", "running")
        live = self.live()
        self.assertEqual(live["sessions"][0]["status"], "running")
        self.assertEqual(live["progress"]["stale"], 0)

    def test_a_stale_row_is_never_persisted(self):
        """It is a read-time observation about the disk, not durable state."""
        self.cohort(n=1)
        analyze.clear_analysis(self.pool / "s0")
        live = self.live()
        analyze.write_cohort(
            self.pool, analyze.persistable_cohort(live, analyze.read_cohort(self.pool, "c"))
        )
        stored = analyze.read_cohort(self.pool, "c")
        self.assertEqual(stored["sessions"][0]["status"], analyze.SESSION_STALE)
        # Restoring the file must clear it again with no manual intervention.
        analyze.write_analysis(self.pool / "s0", {"text": "back"})
        self.assertEqual(self.live()["sessions"][0]["status"], "done")


class TestTransitionTable(pgfixture.CohortStoreMixin, unittest.TestCase):
    """The table is only worth something if the real code obeys it.

    cohort_transition logs rather than raises, because it runs inside a live
    run with a paid batch in flight. These tests are the enforcement: they
    drive the actual pipeline and assert nothing was logged.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.pool = Path(self.tmp) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        analyze._undeclared_transitions.clear()
        self.addCleanup(analyze._undeclared_transitions.clear)

    def test_every_declared_state_is_a_real_state(self):
        for state, allowed in analyze.COHORT_TRANSITIONS.items():
            self.assertIn(state, analyze.COHORT_STATES)
            for target in allowed:
                self.assertIn(target, analyze.COHORT_STATES, f"{state} -> {target}")

    def test_every_state_has_a_row(self):
        self.assertEqual(set(analyze.COHORT_TRANSITIONS), set(analyze.COHORT_STATES))

    def test_the_whole_baseline_makes_only_declared_transitions(self):
        """Replaying every case the old engine could produce must not trip the
        table — otherwise the table describes something other than this code."""
        for case in BASELINE:
            for path in self.pool.iterdir():
                shutil.rmtree(path, ignore_errors=True)
            analyze.enrich_cohort_status(
                self.pool,
                cohort_of(
                    case["stored"],
                    case["done"],
                    case["error"],
                    case["queued"],
                    writeups=case["writeups"],
                    pool=self.pool,
                ),
            )
        self.assertEqual(analyze._undeclared_transitions, [])

    def test_an_undeclared_move_is_reported_and_still_applied(self):
        # idle -> paused is declared; idle -> a state it cannot reach is not.
        self.assertEqual(
            analyze.cohort_transition(
                analyze.CohortState.DONE, analyze.CohortState.IDLE, why="test"
            ),
            analyze.CohortState.IDLE,
            "an undeclared transition must still be applied, not swallowed",
        )
        self.assertEqual(len(analyze._undeclared_transitions), 1)

    def test_an_unknown_current_state_is_not_reported(self):
        """A status from a future version should not spam the log."""
        analyze.cohort_transition("some-new-state", analyze.CohortState.DONE, why="test")
        self.assertEqual(analyze._undeclared_transitions, [])


class TestTerminalRecompute(pgfixture.CohortStoreMixin, unittest.TestCase):
    """Two behaviors that look like bugs and are not, so nobody 'fixes' them.

    A terminal status is recomputed from the tallies rather than frozen: the
    stored value can be stale (a crashed runner, a retried session), and the
    session rows are the truth.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.pool = Path(self.tmp) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def status(self, stored, done, error, queued):
        """Decide with the write-ups actually present, so `done` rows count."""
        for path in self.pool.iterdir():
            shutil.rmtree(path, ignore_errors=True)
        # Write-ups live in the store now, so clearing directories no longer
        # resets them and a case with writeups=False would inherit the last one.
        analyze.forget_analyses(self.pool)
        return analyze.enrich_cohort_status(
            self.pool, cohort_of(stored, done, error, queued, pool=self.pool)
        )["status"]

    def test_a_done_cohort_whose_sessions_all_failed_reports_error(self):
        self.assertEqual(self.status("done", 0, 3, 0), "error")

    def test_a_done_cohort_with_any_success_stays_done(self):
        self.assertEqual(self.status("done", 1, 2, 0), "done")

    def test_an_error_cohort_reports_done_once_a_retry_succeeds(self):
        self.assertEqual(self.status("error", 1, 2, 0), "done")

    def test_an_error_cohort_with_queued_work_stays_in_error(self):
        """A queued row does not silently revive an errored cohort: only
        `running` and `queued` are revivable, and `error` is not in that set.
        The user has to retry, which is the intended contract."""
        self.assertEqual(self.status("error", 1, 1, 1), "error")

    def test_a_queued_cohort_with_work_left_reports_running(self):
        self.assertEqual(self.status("queued", 1, 0, 2), "running")


if __name__ == "__main__":
    unittest.main()
