"""Cohort sampling as a pure query: analyze.sample_cohort_rows over a stub index."""

import json
import sqlite3
import unittest


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
        sampled = analyze.sample_cohort_rows(conn, "", purchased_n=1, all_n=1)
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("newbuy" + "x" * 26, "purchased"), ("newall" + "x" * 26, "all")],
        )

    def test_sample_requires_ashwamag_path_and_min_duration(self):
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
        sampled = analyze.sample_cohort_rows(conn, "", purchased_n=5, all_n=5)
        self.assertEqual(
            {(s["session_id"], s["filter"]) for s in sampled},
            {("okbuy" + "x" * 27, "purchased"), ("okall" + "x" * 27, "all")},
        )

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
        sampled = analyze.sample_cohort_rows(conn, "", purchased_n=0, all_n=0, any_n=2)
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
            purchased_n=0,
            all_n=0,
            any_n=5,
            url_contains="",
            slice_spec={"device": ["mobile"], "utm_source": "google", "page": "/pages/ashwamag-01"},
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
            purchased_n=0,
            all_n=0,
            a_n=1,
            b_n=1,
            url_contains="",
            slice_spec={"outcome": ["purchased"], "page": "/pages/ashwamag-01"},
            slice_b={"outcome": ["checkout", "rest"], "page": "/pages/ashwamag-01"},
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
            purchased_n=0,
            all_n=0,
            any_n=5,
            url_contains="",
            start_ms=15_000,
            end_ms=25_000,
        )
        self.assertEqual([s["session_id"] for s in windowed], ["mid" + "x" * 29])

        earliest = analyze.sample_cohort_rows(
            conn,
            "",
            purchased_n=0,
            all_n=0,
            any_n=2,
            url_contains="",
            order=analyze.COHORT_ORDER_EARLIEST,
        )
        self.assertEqual(
            [s["session_id"] for s in earliest],
            ["old" + "x" * 29, "mid" + "x" * 29],
        )

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
