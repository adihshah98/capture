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

import analyze

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "cohort_status_baseline.json").read_text())

# (stored, done, error, queued) -> (new expectation, why). Empty means the
# refactor is behavior-preserving.
KNOWN_DIVERGENCES: dict = {}


def cohort_of(stored, done, error, queued):
    sessions = [{"session_id": f"d{i}", "status": "done"} for i in range(done)]
    sessions += [{"session_id": f"e{i}", "status": "error"} for i in range(error)]
    sessions += [{"session_id": f"q{i}", "status": "queued"} for i in range(queued)]
    cohort = {"id": "c", "sessions": sessions}
    if stored is not None:
        cohort["status"] = stored
    return cohort


class TestStatusBaseline(unittest.TestCase):
    """Every case the old inference engine decided, decided the same way."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pool = Path(self.tmp) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_baseline_is_not_empty(self):
        self.assertGreater(len(BASELINE), 100)

    def test_every_recorded_case_still_decides_the_same_way(self):
        mismatches = []
        for case in BASELINE:
            key = (case["stored"], case["done"], case["error"], case["queued"])
            got = analyze.enrich_cohort_status(
                self.pool, cohort_of(*key)
            )["status"]
            expect = KNOWN_DIVERGENCES.get(key, (case["expect"], None))[0]
            if got != expect:
                mismatches.append(f"{key}: expected {expect}, got {got}")
        self.assertEqual(mismatches, [], f"{len(mismatches)} status changes")

    def test_progress_tallies_are_unchanged(self):
        for case in BASELINE:
            key = (case["stored"], case["done"], case["error"], case["queued"])
            got = analyze.enrich_cohort_status(self.pool, cohort_of(*key))["progress"]
            self.assertEqual(got, case["progress"], f"progress changed for {key}")


class TestTransitionTable(unittest.TestCase):
    """The table is only worth something if the real code obeys it.

    cohort_transition logs rather than raises, because it runs inside a live
    run with a paid batch in flight. These tests are the enforcement: they
    drive the actual pipeline and assert nothing was logged.
    """

    def setUp(self):
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
            analyze.enrich_cohort_status(
                self.pool,
                cohort_of(case["stored"], case["done"], case["error"], case["queued"]),
            )
        self.assertEqual(analyze._undeclared_transitions, [])

    def test_an_undeclared_move_is_reported_and_still_applied(self):
        # idle -> paused is declared; idle -> a state it cannot reach is not.
        self.assertEqual(
            analyze.cohort_transition(analyze.CohortState.DONE, analyze.CohortState.IDLE, why="test"),
            analyze.CohortState.IDLE,
            "an undeclared transition must still be applied, not swallowed",
        )
        self.assertEqual(len(analyze._undeclared_transitions), 1)

    def test_an_unknown_current_state_is_not_reported(self):
        """A status from a future version should not spam the log."""
        analyze.cohort_transition("some-new-state", analyze.CohortState.DONE, why="test")
        self.assertEqual(analyze._undeclared_transitions, [])


class TestTerminalRecompute(unittest.TestCase):
    """Two behaviors that look like bugs and are not, so nobody 'fixes' them.

    A terminal status is recomputed from the tallies rather than frozen: the
    stored value can be stale (a crashed runner, a retried session), and the
    session rows are the truth.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pool = Path(self.tmp) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def status(self, stored, done, error, queued):
        return analyze.enrich_cohort_status(
            self.pool, cohort_of(stored, done, error, queued)
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
