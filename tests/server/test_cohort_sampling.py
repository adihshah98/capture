"""Cohort sampling as a pure query: analyze.sample_cohort_rows over a stub index."""

import json
import sqlite3
import unittest
from unittest import mock

from capture.analyze.spec import CohortSpec


class TestCohortSampling(unittest.TestCase):
    def _conn(self, rows):
        from capture import analyze

        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE sessions (
                pool TEXT, session_id TEXT, first_seen INTEGER, last_seen INTEGER,
                chunk_count INTEGER, bytes INTEGER, visitor_id TEXT, url TEXT,
                "order" TEXT, interactions TEXT, device TEXT, attribution TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return analyze, conn

    def test_sample_prefers_newest_and_splits_filters(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "oldbuy" + "x" * 26,
                    0,
                    10_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 1}),
                    None,
                    None,
                    None,
                ),
                (
                    "",
                    "newbuy" + "x" * 26,
                    0,
                    30_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 2}),
                    json.dumps({"clicks": 3}),
                    None,
                    None,
                ),
                (
                    "",
                    "oldall" + "x" * 26,
                    0,
                    5_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    "",
                    "newall" + "x" * 26,
                    0,
                    40_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    json.dumps({"clicks": 2}),
                    None,
                    None,
                ),
                (
                    "",
                    "nochunk" + "x" * 25,
                    0,
                    50_000,
                    0,
                    0,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 3}),
                    None,
                    None,
                    None,
                ),
            ]
        )
        sampled = analyze.sample_cohort_rows(
            conn, "", CohortSpec(purchased_n=1, all_n=1, url_contains="")
        )
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("newbuy" + "x" * 26, "purchased"), ("newall" + "x" * 26, "all")],
        )

    def test_sample_honours_the_url_filter_and_min_duration(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "short" + "x" * 27,
                    0,
                    4_999,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 1}),
                    None,
                    None,
                    None,
                ),
                (
                    "",
                    "other" + "x" * 27,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/other",
                    json.dumps({"purchased_at": 1}),
                    None,
                    None,
                    None,
                ),
                (
                    "",
                    "okbuy" + "x" * 27,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01?x=1",
                    json.dumps({"purchased_at": 1}),
                    None,
                    None,
                    None,
                ),
                (
                    "",
                    "okall" + "x" * 27,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    None,
                    None,
                ),
            ]
        )
        # url_contains is passed explicitly: it is a per-cohort choice, not a
        # global default. It used to default to one customer's landing page,
        # which silently scoped every cohort sampled without an override.
        sampled = analyze.sample_cohort_rows(
            conn, "", CohortSpec(purchased_n=5, all_n=5, url_contains="/pages/ashwamag-01")
        )
        self.assertEqual(
            {(s["session_id"], s["filter"]) for s in sampled},
            {("okbuy" + "x" * 27, "purchased"), ("okall" + "x" * 27, "all")},
        )

    def test_an_empty_url_filter_samples_every_page(self):
        """The default. A pool with no ashwamag URLs still yields a cohort --
        the regression that the old hardcoded default caused for every tenant
        that was not that one customer."""
        analyze, conn = self._conn(
            [
                (
                    "",
                    "onbrand" + "x" * 25,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://other.test/pages/serum",
                    json.dumps({"purchased_at": 1}),
                    None,
                    None,
                    None,
                ),
            ]
        )
        sampled = analyze.sample_cohort_rows(
            conn, "", CohortSpec(purchased_n=1, all_n=0, url_contains="")
        )
        self.assertEqual([s["session_id"] for s in sampled], ["onbrand" + "x" * 25])

    def test_sample_any_mixes_outcomes_up_to_limit(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "buy1" + "x" * 28,
                    0,
                    30_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 1}),
                    json.dumps({"clicks": 2}),
                    None,
                    None,
                ),
                (
                    "",
                    "rest1" + "x" * 27,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    json.dumps({"clicks": 1}),
                    None,
                    None,
                ),
                (
                    "",
                    "buy2" + "x" * 28,
                    0,
                    10_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 2}),
                    None,
                    None,
                    None,
                ),
            ]
        )
        sampled = analyze.sample_cohort_rows(conn, "", CohortSpec(any_n=2, url_contains=""))
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("buy1" + "x" * 28, "purchased"), ("rest1" + "x" * 27, "all")],
        )

    def test_sample_honors_cut_style_slice_filters(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "mob" + "x" * 29,
                    0,
                    40_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01?utm_source=google",
                    json.dumps({"purchased_at": 1}),
                    None,
                    json.dumps({"width": 390}),
                    json.dumps({"utm_source": "google"}),
                ),
                (
                    "",
                    "desk" + "x" * 28,
                    0,
                    50_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01?utm_source=google",
                    json.dumps({"purchased_at": 2}),
                    None,
                    json.dumps({"width": 1280}),
                    json.dumps({"utm_source": "google"}),
                ),
                (
                    "",
                    "fb" + "x" * 30,
                    0,
                    60_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01?utm_source=facebook",
                    None,
                    None,
                    json.dumps({"width": 390}),
                    json.dumps({"utm_source": "facebook"}),
                ),
            ]
        )
        sampled = analyze.sample_cohort_rows(
            conn,
            "",
            CohortSpec(
                any_n=5,
                url_contains="",
                slice_spec={
                    "device": ["mobile"],
                    "utm_source": "google",
                    "page": "/pages/ashwamag-01",
                },
            ),
        )
        self.assertEqual(
            [s["session_id"] for s in sampled],
            ["mob" + "x" * 29],
        )

    def test_sample_groups_a_and_b_with_independent_slices(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "buy" + "x" * 29,
                    0,
                    50_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 1}),
                    None,
                    json.dumps({"width": 390}),
                    None,
                ),
                (
                    "",
                    "rest" + "x" * 28,
                    0,
                    40_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    json.dumps({"width": 390}),
                    None,
                ),
                (
                    "",
                    "other" + "x" * 27,
                    0,
                    30_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/other",
                    None,
                    None,
                    json.dumps({"width": 390}),
                    None,
                ),
            ]
        )
        sampled = analyze.sample_cohort_rows(
            conn,
            "",
            CohortSpec(
                a_n=1,
                b_n=1,
                url_contains="",
                slice_spec={"outcome": ["purchased"], "page": "/pages/ashwamag-01"},
                slice_b={"outcome": ["checkout", "rest"], "page": "/pages/ashwamag-01"},
            ),
        )
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("buy" + "x" * 29, "a"), ("rest" + "x" * 28, "b")],
        )

    def test_sample_time_window_and_earliest_order(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "old" + "x" * 29,
                    0,
                    10_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    "",
                    "mid" + "x" * 29,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    "",
                    "new" + "x" * 29,
                    0,
                    30_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    None,
                    None,
                ),
            ]
        )
        windowed = analyze.sample_cohort_rows(
            conn,
            "",
            CohortSpec(any_n=5, url_contains="", start_ms=15_000, end_ms=25_000),
        )
        self.assertEqual([s["session_id"] for s in windowed], ["mid" + "x" * 29])

        earliest = analyze.sample_cohort_rows(
            conn,
            "",
            CohortSpec(any_n=2, url_contains="", order=analyze.COHORT_ORDER_EARLIEST),
        )
        self.assertEqual(
            [s["session_id"] for s in earliest],
            ["old" + "x" * 29, "mid" + "x" * 29],
        )

    def test_sample_reports_a_genuine_pool_shortfall(self):
        """Fewer matches than asked for, but the index was fully scanned."""
        analyze, conn = self._conn(
            [
                (
                    "",
                    "only" + "x" * 28,
                    0,
                    10_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    json.dumps({"width": 390}),
                    None,
                ),
            ]
        )
        result = analyze.sample_cohort(
            conn,
            "",
            CohortSpec(any_n=5, url_contains="", slice_spec={"device": ["mobile"]}),
        )
        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(
            result.sample.as_dict(),
            {"requested": 5, "matched": 1, "scanned": 1, "capped": False},
        )

    def test_sample_pages_past_a_thin_first_window(self):
        """A rare slice past the first page used to return empty with no signal."""
        from capture.analyze import spec as cohort_spec

        analyze, conn = self._conn([])
        # Newest rows miss the slice; the match sits just past a tiny page.
        rows = []
        for i in range(12):
            rows.append(
                (
                    "",
                    f"miss{i:02d}" + "x" * 25,
                    0,
                    100_000 - i,
                    1,
                    1,
                    None,
                    "https://x.test/pages/other",
                    None,
                    None,
                    json.dumps({"width": 1280}),
                    None,
                )
            )
        rows.append(
            (
                "",
                "hit" + "x" * 29,
                0,
                100_000 - 12,
                1,
                1,
                None,
                "https://x.test/pages/ashwamag-01",
                None,
                None,
                json.dumps({"width": 390}),
                None,
            )
        )
        conn.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        with mock.patch.object(cohort_spec, "CANDIDATE_CAP", 20):
            with mock.patch("capture.analyze.sampling._page_size", return_value=5):
                result = analyze.sample_cohort(
                    conn,
                    "",
                    CohortSpec(
                        any_n=1,
                        url_contains="",
                        slice_spec={"device": ["mobile"], "page": "/pages/ashwamag-01"},
                    ),
                )
        self.assertEqual([s["session_id"] for s in result.sessions], ["hit" + "x" * 29])
        self.assertEqual(result.sample.requested, 1)
        self.assertEqual(result.sample.matched, 1)
        self.assertGreater(result.sample.scanned, 5)
        self.assertFalse(result.sample.capped)

    def test_sample_marks_capped_when_the_hard_scan_limit_bites(self):
        from capture.analyze import spec as cohort_spec

        analyze, conn = self._conn([])
        rows = [
            (
                "",
                f"desk{i:02d}" + "x" * 24,
                0,
                50_000 - i,
                1,
                1,
                None,
                "https://x.test/pages/ashwamag-01",
                None,
                None,
                json.dumps({"width": 1280}),
                None,
            )
            for i in range(8)
        ]
        conn.executemany(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        with mock.patch.object(cohort_spec, "CANDIDATE_CAP", 3):
            with mock.patch("capture.analyze.sampling._page_size", return_value=3):
                result = analyze.sample_cohort(
                    conn,
                    "",
                    CohortSpec(
                        any_n=2,
                        url_contains="",
                        slice_spec={"device": ["mobile"]},
                    ),
                )
        self.assertEqual(result.sessions, [])
        self.assertEqual(
            result.sample.as_dict(),
            {"requested": 2, "matched": 0, "scanned": 3, "capped": True},
        )

    def test_build_cohort_persists_sample_stats(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "one" + "x" * 29,
                    0,
                    10_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    None,
                    None,
                ),
            ]
        )
        cohort = analyze.build_cohort(conn, "", CohortSpec(any_n=3, url_contains=""), label="probe")
        self.assertEqual(
            cohort["sample"],
            {"requested": 3, "matched": 1, "scanned": 1, "capped": False},
        )
        self.assertEqual(len(cohort["sessions"]), 1)

    def test_standout_line_prefers_what_stands_out_section(self):
        from capture import analyze

        text = (
            "## What they did\nWent to cart.\n\n"
            "## What stands out\n- Shipping fee surprise at checkout.\n- Also size chart.\n"
        )
        self.assertEqual(analyze.standout_line(text), "Shipping fee surprise at checkout.")
        self.assertEqual(analyze.standout_line("## Hello\nPlain opener."), "Plain opener.")


if __name__ == "__main__":
    unittest.main()
