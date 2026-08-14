"""Cohort sampling as a pure query: sampling.sample_cohort_rows over a stub index."""

import json
import sqlite3
import unittest
from unittest import mock

from capture.analyze import cohort_state, cohorts, sampling, spec
from capture.analyze.spec import CohortSpec


class _PsycopgStyleSqlite:
    """sqlite3 accepts `?`; production SQL uses `%s`. Translate at execute time."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, params=()):
        return self._conn.execute(sql.replace("%s", "?"), params)

    def executemany(self, sql: str, params_seq):
        return self._conn.executemany(sql.replace("%s", "?"), params_seq)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestCohortSampling(unittest.TestCase):
    def _insert(self, conn, rows):
        # Fixtures still pass the pre-normalised 12-tuple:
        # (project_id, session_id, first_seen, last_seen, chunk_count, bytes,
        #  visitor_id, url, order_json, interactions_json, device, attribution)
        # Optionally a 13th clicks column. Order JSON becomes an orders row;
        # clicks come from the interactions JSON when not supplied explicitly.
        # Class columns are derived the same way ingest does (capture.classify).
        from capture import classify

        for row in rows:
            row = list(row)
            project_id = row[0]
            session_id = row[1]
            first_seen = row[2]
            last_seen = row[3]
            chunk_count = row[4]
            nbytes = row[5]
            visitor_id = row[6]
            url = row[7]
            order_raw = row[8]
            interactions_raw = row[9]
            device_raw = row[10]
            attribution_raw = row[11]
            if len(row) >= 13:
                clicks = int(row[12] or 0)
            else:
                clicks = 0
                if interactions_raw:
                    try:
                        data = json.loads(interactions_raw)
                    except (TypeError, ValueError):
                        data = None
                    if isinstance(data, dict):
                        clicks = int(data.get("clicks") or 0)

            device = None
            if device_raw:
                try:
                    device = json.loads(device_raw) if isinstance(device_raw, str) else device_raw
                except (TypeError, ValueError):
                    device = None
            attribution = None
            if attribution_raw:
                try:
                    attribution = (
                        json.loads(attribution_raw)
                        if isinstance(attribution_raw, str)
                        else attribution_raw
                    )
                except (TypeError, ValueError):
                    attribution = None

            checkout = purchased = None
            if order_raw:
                try:
                    order = json.loads(order_raw) if isinstance(order_raw, str) else order_raw
                except (TypeError, ValueError):
                    order = None
                if isinstance(order, dict):
                    checkout = order.get("checkout_started_at")
                    purchased = order.get("purchased_at")
            outcome = classify.outcome_class(
                {"checkout_started_at": checkout, "purchased_at": purchased}
                if checkout is not None or purchased is not None
                else None
            )
            classes = classify.session_classes(
                url=url,
                device=device if isinstance(device, dict) else None,
                attribution=attribution,
            )

            conn.execute(
                "INSERT INTO sessions (project_id, session_id, first_seen, last_seen,"
                " chunk_count, bytes, visitor_id, url, device, attribution, clicks,"
                " outcome, device_class, page, utm_source, utm_medium, utm_campaign,"
                " utm_content, has_fbclid, is_meta)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project_id,
                    session_id,
                    first_seen,
                    last_seen,
                    chunk_count,
                    nbytes,
                    visitor_id,
                    url,
                    device_raw,
                    attribution_raw,
                    clicks,
                    outcome,
                    classes["device_class"],
                    classes["page"],
                    classes["utm_source"],
                    classes["utm_medium"],
                    classes["utm_campaign"],
                    classes["utm_content"],
                    classes["has_fbclid"],
                    classes["is_meta"],
                ),
            )
            if checkout is not None or purchased is not None:
                conn.execute(
                    "INSERT INTO orders (project_id, session_id,"
                    " checkout_started_at, purchased_at)"
                    " VALUES (%s, %s, %s, %s)",
                    (project_id, session_id, checkout, purchased),
                )

    def _conn(self, rows):
        raw = sqlite3.connect(":memory:")
        raw.execute(
            """
            CREATE TABLE sessions (
                project_id TEXT, session_id TEXT, first_seen INTEGER, last_seen INTEGER,
                chunk_count INTEGER, bytes INTEGER, visitor_id TEXT, url TEXT,
                device TEXT, attribution TEXT, clicks INTEGER NOT NULL DEFAULT 0,
                outcome TEXT NOT NULL DEFAULT 'rest',
                device_class TEXT,
                page TEXT,
                utm_source TEXT,
                utm_medium TEXT,
                utm_campaign TEXT,
                utm_content TEXT,
                has_fbclid INTEGER NOT NULL DEFAULT 0,
                is_meta INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        raw.execute(
            """
            CREATE TABLE orders (
                project_id TEXT, session_id TEXT,
                checkout_started_at INTEGER, purchased_at INTEGER
            )
            """
        )
        conn = _PsycopgStyleSqlite(raw)
        if rows:
            self._insert(conn, rows)
        return conn

    def test_sample_prefers_newest_and_splits_filters(self):
        conn = self._conn(
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
        sampled = sampling.sample_cohort_rows(
            conn,
            "",
            CohortSpec(
                a_n=1,
                b_n=1,
                url_contains="",
                slice_spec={"outcome": ["purchased"]},
                slice_b={"outcome": ["checkout", "rest"]},
            ),
        )
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("newbuy" + "x" * 26, "a"), ("newall" + "x" * 26, "b")],
        )

    def test_clicks_break_last_seen_ties(self):
        conn = self._conn(
            [
                (
                    "",
                    "quiet" + "x" * 27,
                    0,
                    50_000,
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
                    "busy" + "x" * 28,
                    0,
                    50_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    json.dumps({"clicks": 9}),
                    None,
                    None,
                ),
            ]
        )
        sampled = sampling.sample_cohort_rows(conn, "", CohortSpec(a_n=2, url_contains=""))
        self.assertEqual(
            [s["session_id"] for s in sampled],
            ["busy" + "x" * 28, "quiet" + "x" * 27],
        )

    def test_pool_facets_caps_the_scan(self):
        """An unbounded fetchall grew with every session in the project."""
        from capture.analyze import spec as cohort_spec

        conn = self._conn([])
        rows = [
            (
                "",
                f"row{i:02d}" + "x" * 25,
                0,
                100_000 - i,
                1,
                1,
                None,
                "https://x.test/pages/ashwamag-01",
                None,
                None,
                json.dumps({"width": 390 if i % 2 == 0 else 1280}),
                None,
            )
            for i in range(8)
        ]
        self._insert(conn, rows)
        with mock.patch.object(cohort_spec, "CANDIDATE_CAP", 3):
            facets = sampling.pool_facets(conn, "", min_duration_ms=0)
        self.assertEqual(facets["n"], 3)
        # Newest three: widths alternate starting mobile at i=0.
        devices = {item["value"]: item["count"] for item in facets["device"]}
        self.assertEqual(devices.get("mobile"), 2)
        self.assertEqual(devices.get("desktop"), 1)

    def test_classified_rows_are_scoped_to_the_requested_ids(self):
        """Cuts used to SELECT every session, then re-classify from raw JSON."""
        keep = "keep" + "x" * 28
        skip = "skip" + "x" * 28
        other = "othr" + "x" * 28
        conn = self._conn(
            [
                (
                    "",
                    keep,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/p?utm_source=google",
                    json.dumps({"purchased_at": 1}),
                    None,
                    json.dumps({"width": 375}),
                    json.dumps({"utm_source": "google"}),
                ),
                (
                    "",
                    skip,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/other",
                    None,
                    None,
                    json.dumps({"width": 1280}),
                    None,
                ),
                (
                    "p2",
                    other,
                    0,
                    20_000,
                    1,
                    1,
                    None,
                    "https://x.test/p",
                    json.dumps({"purchased_at": 1}),
                    None,
                    json.dumps({"width": 375}),
                    None,
                ),
            ]
        )
        facts = sampling.classified_rows(conn, "", [keep, "gone" + "x" * 28])
        self.assertEqual(set(facts), {keep})
        row = facts[keep]
        self.assertEqual(row["outcome"], "purchased")
        self.assertEqual(row["device"], "mobile")
        self.assertEqual(row["page"], "/p")
        self.assertEqual(row["attribution"].get("utm_source"), "google")

    def test_classified_rows_skip_sql_when_the_id_list_is_empty(self):
        conn = mock.Mock()
        self.assertEqual(sampling.classified_rows(conn, "proj", []), {})
        conn.execute.assert_not_called()

    def test_sample_honours_the_url_filter_and_min_duration(self):
        conn = self._conn(
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
        sampled = sampling.sample_cohort_rows(
            conn,
            "",
            CohortSpec(
                a_n=5,
                b_n=5,
                url_contains="/pages/ashwamag-01",
                slice_spec={"outcome": ["purchased"]},
                slice_b={"outcome": ["checkout", "rest"]},
            ),
        )
        self.assertEqual(
            {(s["session_id"], s["filter"]) for s in sampled},
            {("okbuy" + "x" * 27, "a"), ("okall" + "x" * 27, "b")},
        )

    def test_an_empty_url_filter_samples_every_page(self):
        """The default. A pool with no ashwamag URLs still yields a cohort --
        the regression that the old hardcoded default caused for every tenant
        that was not that one customer."""
        conn = self._conn(
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
        sampled = sampling.sample_cohort_rows(
            conn,
            "",
            CohortSpec(a_n=1, url_contains="", slice_spec={"outcome": ["purchased"]}),
        )
        self.assertEqual([s["session_id"] for s in sampled], ["onbrand" + "x" * 25])

    def test_sample_any_mixes_outcomes_up_to_limit(self):
        conn = self._conn(
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
        sampled = sampling.sample_cohort_rows(conn, "", CohortSpec(a_n=2, url_contains=""))
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("buy1" + "x" * 28, "a"), ("rest1" + "x" * 27, "a")],
        )

    def test_sample_honors_cut_style_slice_filters(self):
        conn = self._conn(
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
        sampled = sampling.sample_cohort_rows(
            conn,
            "",
            CohortSpec(
                a_n=5,
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
        conn = self._conn(
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
        sampled = sampling.sample_cohort_rows(
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

    def test_group_b_does_not_burn_its_window_on_a(self):
        """A's members must not occupy B's LIMIT — exclude_ids is pushed into SQL."""
        conn = self._conn([])
        rows = []
        # Newest three: purchased (A). Older three: non-purchase (B).
        for i in range(3):
            rows.append(
                (
                    "",
                    f"buy{i}" + "x" * 28,
                    0,
                    90_000 - i,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 1}),
                    None,
                    None,
                    None,
                )
            )
        for i in range(3):
            rows.append(
                (
                    "",
                    f"rest{i}" + "x" * 27,
                    0,
                    50_000 - i,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    None,
                    None,
                )
            )
        self._insert(conn, rows)
        result = sampling.sample_cohort(
            conn,
            "",
            CohortSpec(
                a_n=3,
                b_n=3,
                url_contains="",
                slice_spec={"outcome": ["purchased"]},
                slice_b={"outcome": ["checkout", "rest"]},
            ),
        )
        by_arm = {
            "a": [s["session_id"] for s in result.sessions if s["filter"] == "a"],
            "b": [s["session_id"] for s in result.sessions if s["filter"] == "b"],
        }
        self.assertEqual(len(by_arm["a"]), 3)
        self.assertEqual(len(by_arm["b"]), 3)
        self.assertEqual(result.sample.matched, 6)
        self.assertFalse(result.sample.capped)
        # Each arm is its own LIMIT 3 — B does not re-read A's purchased rows.
        self.assertEqual(result.sample.scanned, 6)

    def test_sample_time_window_and_earliest_order(self):
        conn = self._conn(
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
        windowed = sampling.sample_cohort_rows(
            conn,
            "",
            CohortSpec(a_n=5, url_contains="", start_ms=15_000, end_ms=25_000),
        )
        self.assertEqual([s["session_id"] for s in windowed], ["mid" + "x" * 29])

        earliest = sampling.sample_cohort_rows(
            conn,
            "",
            CohortSpec(a_n=2, url_contains="", order=spec.ORDER_EARLIEST),
        )
        self.assertEqual(
            [s["session_id"] for s in earliest],
            ["old" + "x" * 29, "mid" + "x" * 29],
        )

    def test_sample_reports_a_genuine_pool_shortfall(self):
        """Fewer matches than asked for, but the index was fully scanned."""
        conn = self._conn(
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
        result = sampling.sample_cohort(
            conn,
            "",
            CohortSpec(a_n=5, url_contains="", slice_spec={"device": ["mobile"]}),
        )
        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(
            result.sample.as_dict(),
            {"requested": 5, "matched": 1, "scanned": 1, "capped": False},
        )

    def test_sample_sql_finds_a_rare_slice_without_scanning_misses(self):
        """Slice predicates are columns: LIMIT 1 never walks past non-matches."""
        conn = self._conn([])
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
        self._insert(conn, rows)
        result = sampling.sample_cohort(
            conn,
            "",
            CohortSpec(
                a_n=1,
                url_contains="",
                slice_spec={"device": ["mobile"], "page": "/pages/ashwamag-01"},
            ),
        )
        self.assertEqual([s["session_id"] for s in result.sessions], ["hit" + "x" * 29])
        self.assertEqual(
            result.sample.as_dict(),
            {"requested": 1, "matched": 1, "scanned": 1, "capped": False},
        )

    def test_sample_empty_slice_is_a_pool_shortfall_not_a_scan_cap(self):
        """No matching rows → matched=0; SQL does not invent a scan-cap shortfall."""
        conn = self._conn([])
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
        self._insert(conn, rows)
        result = sampling.sample_cohort(
            conn,
            "",
            CohortSpec(
                a_n=2,
                url_contains="",
                slice_spec={"device": ["mobile"]},
            ),
        )
        self.assertEqual(result.sessions, [])
        self.assertEqual(
            result.sample.as_dict(),
            {"requested": 2, "matched": 0, "scanned": 0, "capped": False},
        )

    def test_build_cohort_persists_sample_stats(self):
        conn = self._conn(
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
        cohort = cohorts.build_cohort(conn, "", CohortSpec(a_n=3, url_contains=""), label="probe")
        self.assertEqual(
            cohort["sample"],
            {"requested": 3, "matched": 1, "scanned": 1, "capped": False},
        )
        self.assertEqual(len(cohort["sessions"]), 1)
        self.assertEqual(cohort["a_n"], 3)
        self.assertNotIn("any_n", cohort)
        self.assertEqual(cohort["sessions"][0]["filter"], "a")

    def test_standout_line_prefers_what_stands_out_section(self):
        text = (
            "## What they did\nWent to cart.\n\n"
            "## What stands out\n- Shipping fee surprise at checkout.\n- Also size chart.\n"
        )
        self.assertEqual(cohort_state.standout_line(text), "Shipping fee surprise at checkout.")
        self.assertEqual(cohort_state.standout_line("## Hello\nPlain opener."), "Plain opener.")


if __name__ == "__main__":
    unittest.main()
