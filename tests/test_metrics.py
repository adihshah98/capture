import unittest

from metrics import (
    METRICS_BY_KEY,
    MIN_SAMPLE_FOR_PCT,
    cohort_metrics,
    compute_metric,
)


def facts(**over):
    """A complete, measurable session; override only what a test is about."""
    base = {
        "schema": 1,
        "duration_s": 60,
        "active_s": 45,
        "idle_s": 15,
        "active_pct": 75,
        "max_depth_pct": 80,
        "dwell_ms_by_band": {"0-25": 5000, "25-50": 5000, "50-75": 20000, "75-100": 15000},
        "page_count": 1,
        "pages": ["/p/item"],
        "click_count": 2,
        "rage_click_count": 0,
        "viewport": {"width": 400, "height": 800},
        "page_height_px": 2600,
        "has_geometry": True,
    }
    base.update(over)
    return base


def rows_for(n, **row_over):
    row = {"outcome": "rest", "device": "desktop"}
    row.update(row_over)
    return [dict(row, session_id=str(i)) for i in range(n)]


class RateTests(unittest.TestCase):
    def test_rate_counts_only_sessions_meeting_the_condition(self):
        sample = [facts(max_depth_pct=d) for d in (90, 80, 40, 10)]
        out = compute_metric(METRICS_BY_KEY["scrolled_past_75"], sample, 4)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["pct"], 50)
        self.assertEqual(out["eligible"], 4)

    def test_dwell_metric_reads_the_band_totals(self):
        sample = [
            facts(dwell_ms_by_band={"0-25": 0, "25-50": 0, "50-75": 0, "75-100": 20000}),
            facts(dwell_ms_by_band={"0-25": 0, "25-50": 0, "50-75": 0, "75-100": 3000}),
        ]
        out = compute_metric(METRICS_BY_KEY["dwell_over_10s_deep"], sample, 2)
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["pct"], 50)


class CoverageTests(unittest.TestCase):
    def test_unmeasurable_sessions_leave_the_denominator(self):
        # Two sessions never got geometry; a depth metric must not read them as
        # "did not scroll" — they are simply unanswerable.
        sample = [facts(max_depth_pct=90), facts(max_depth_pct=80)] + [
            facts(has_geometry=False, max_depth_pct=None) for _ in range(2)
        ]
        out = compute_metric(METRICS_BY_KEY["scrolled_past_75"], sample, 4)
        self.assertEqual(out["eligible"], 2)
        self.assertEqual(out["pct"], 100)
        self.assertEqual(out["coverage_pct"], 50)

    def test_coverage_is_measured_against_the_whole_cohort(self):
        sample = [facts() for _ in range(3)]
        out = compute_metric(METRICS_BY_KEY["scrolled_past_50"], sample, 10)
        self.assertEqual(out["eligible"], 3)
        self.assertEqual(out["coverage_pct"], 30)

    def test_zero_eligible_reports_no_percentage(self):
        sample = [facts(has_geometry=False, max_depth_pct=None) for _ in range(3)]
        out = compute_metric(METRICS_BY_KEY["reached_bottom"], sample, 3)
        self.assertEqual(out["eligible"], 0)
        self.assertIsNone(out["pct"])
        self.assertEqual(out["coverage_pct"], 0)

    def test_small_samples_are_flagged_directional(self):
        few = [facts() for _ in range(MIN_SAMPLE_FOR_PCT - 1)]
        self.assertTrue(compute_metric(METRICS_BY_KEY["any_click"], few, 4)["directional"])
        many = [facts() for _ in range(MIN_SAMPLE_FOR_PCT + 1)]
        self.assertFalse(compute_metric(METRICS_BY_KEY["any_click"], many, 6)["directional"])


class DistributionTests(unittest.TestCase):
    def test_median_of_odd_and_even_counts(self):
        odd = [facts(active_s=s) for s in (10, 20, 90)]
        self.assertEqual(compute_metric(METRICS_BY_KEY["median_active_s"], odd, 3)["median"], 20)
        even = [facts(active_s=s) for s in (10, 20, 30, 90)]
        self.assertEqual(compute_metric(METRICS_BY_KEY["median_active_s"], even, 4)["median"], 25)

    def test_zero_length_sessions_do_not_drag_the_median(self):
        # A 0s recording is not a 0s visit; it is an absent measurement.
        sample = [facts(active_s=40, duration_s=60), facts(active_s=0, duration_s=0)]
        out = compute_metric(METRICS_BY_KEY["median_active_s"], sample, 2)
        self.assertEqual(out["median"], 40)
        self.assertEqual(out["eligible"], 1)


class CohortTests(unittest.TestCase):
    def test_slice_selects_the_same_sessions_a_cut_would(self):
        rows = rows_for(3, device="mobile") + rows_for(2, device="desktop")
        out = cohort_metrics(rows, lambda r: facts(), {"device": ["mobile"]})
        self.assertEqual(out["cohort_size"], 3)
        self.assertEqual(out["with_facts"], 3)

    def test_sessions_without_facts_are_reported_not_hidden(self):
        rows = rows_for(4)
        # Only the first two have been extracted.
        have = {"0", "1"}
        out = cohort_metrics(
            rows, lambda r: facts() if r["session_id"] in have else None, None
        )
        self.assertEqual(out["cohort_size"], 4)
        self.assertEqual(out["with_facts"], 2)
        self.assertEqual(out["missing_facts"], 2)
        # Coverage is against the full cohort, so a half-extracted cohort shows it.
        by_key = {m["key"]: m for m in out["metrics"]}
        self.assertEqual(by_key["any_click"]["coverage_pct"], 50)

    def test_selecting_specific_metrics(self):
        out = cohort_metrics(rows_for(2), lambda r: facts(), None, keys=["any_click"])
        self.assertEqual([m["key"] for m in out["metrics"]], ["any_click"])

    def test_empty_cohort_does_not_divide_by_zero(self):
        out = cohort_metrics([], lambda r: facts(), None)
        self.assertEqual(out["cohort_size"], 0)
        self.assertTrue(all(m["coverage_pct"] == 0 for m in out["metrics"]))


if __name__ == "__main__":
    unittest.main()
