"""Sampling against the public Postgres schema (no compat layer)."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
import psycopg

from capture import db
from capture.analyze import cohorts, sampling
from capture.analyze.spec import CohortSpec


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class SamplingCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        cls.prev = os.environ.get("CAPTURE_DATABASE_URL")
        os.environ["CAPTURE_DATABASE_URL"] = cls.app_url
        db.close_pool()

        with psycopg.connect(cls.admin_url, autocommit=True) as conn:
            cls.alpha = conn.execute(
                "INSERT INTO projects (name) VALUES ('alpha') RETURNING id"
            ).fetchone()[0]
            cls.beta = conn.execute(
                "INSERT INTO projects (name) VALUES ('beta') RETURNING id"
            ).fetchone()[0]
            cls.seed(
                conn,
                cls.alpha,
                "a-bought",
                url="https://shop.test/p/1?utm_source=meta",
                clicks=5,
                purchased=True,
                width=390,
            )
            cls.seed(
                conn, cls.alpha, "a-browsed", url="https://shop.test/p/2", clicks=1, width=1440
            )
            cls.seed(conn, cls.beta, "b-secret", url="https://other.test/", clicks=9)

    @staticmethod
    def seed(conn, project, sid, *, url, clicks=0, purchased=False, width=None):
        from capture import classify

        device = {"width": width, "height": 800} if width else None
        attribution = {"utm_source": "meta"} if "utm_source" in url else None
        classes = classify.session_classes(url=url, device=device, attribution=attribution)
        outcome = "purchased" if purchased else "rest"
        conn.execute(
            "INSERT INTO sessions (project_id, session_id, first_seen, last_seen,"
            " chunk_count, clicks, url, device, attribution,"
            " outcome, device_class, page, utm_source, utm_medium, utm_campaign,"
            " utm_content, has_fbclid, is_meta)"
            " VALUES (%s, %s, 1000, 60000, 2, %s, %s, %s, %s,"
            " %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                project,
                sid,
                clicks,
                url,
                json.dumps(device) if device else None,
                json.dumps(attribution) if attribution else None,
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
        if purchased:
            conn.execute(
                "INSERT INTO orders (project_id, session_id, purchased_at, total, currency)"
                " VALUES (%s, %s, 55000, 42.0, 'USD')",
                (project, sid),
            )

    @classmethod
    def tearDownClass(cls):
        db.close_pool()
        if cls.prev is None:
            os.environ.pop("CAPTURE_DATABASE_URL", None)
        else:
            os.environ["CAPTURE_DATABASE_URL"] = cls.prev


class TestAnalyzeOnPublicSchema(SamplingCase):
    def test_pool_facets_reads_this_project(self):
        with db.connection(self.alpha) as conn:
            facets = sampling.pool_facets(conn, str(self.alpha))
        self.assertIn("outcome", facets)
        flat = json.dumps(facets)
        self.assertIn("meta", flat)
        self.assertNotIn("other.test", flat)

    def test_sample_cohort_rows_returns_this_project_only(self):
        with db.connection(self.alpha) as conn:
            rows = sampling.sample_cohort_rows(
                conn, str(self.alpha), CohortSpec(any_n=10, url_contains="")
            )
        ids = sorted(r["session_id"] for r in rows)
        self.assertEqual(ids, ["a-bought", "a-browsed"])

    def test_a_purchase_is_classified_from_sessions_outcome(self):
        with db.connection(self.alpha) as conn:
            rows = sampling.sample_cohort_rows(
                conn, str(self.alpha), CohortSpec(any_n=10, url_contains="")
            )
        classified = {r["session_id"]: r["filter"] for r in rows}
        self.assertEqual(classified["a-bought"], "purchased")
        self.assertNotEqual(classified["a-browsed"], "purchased")

    def test_build_cohort_composes_a_group(self):
        with db.connection(self.alpha) as conn:
            cohort = cohorts.build_cohort(
                conn, str(self.alpha), CohortSpec(any_n=2, url_contains="")
            )
        ids = sorted(s["session_id"] for s in cohort["sessions"])
        self.assertEqual(ids, ["a-bought", "a-browsed"])

    def test_clicks_column_orders_without_json_extract(self):
        with db.connection(self.alpha) as conn:
            rows = conn.execute(
                "SELECT s.session_id FROM sessions s WHERE s.project_id = %s"
                " ORDER BY coalesce(s.clicks, 0) DESC",
                (str(self.alpha),),
            ).fetchall()
        self.assertEqual([r[0] for r in rows], ["a-bought", "a-browsed"])

    def test_compat_schema_is_gone_after_migrate(self):
        with psycopg.connect(self.admin_url) as conn:
            row = conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'compat'").fetchone()
        self.assertIsNone(row)

    def test_compat_module_is_gone(self):
        with self.assertRaises(ImportError):
            from capture import compat  # noqa: F401


class TestSamplingKeepsTenantsApart(SamplingCase):
    def test_connection_cannot_leak_another_project(self):
        with db.connection(self.alpha) as conn:
            rows = conn.execute("SELECT session_id FROM sessions").fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ["a-bought", "a-browsed"])

    def test_naming_another_project_returns_nothing(self):
        with db.connection(self.alpha) as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE project_id = %s", (str(self.beta),)
            ).fetchall()
        self.assertEqual(rows, [])

    def test_analyze_cannot_sample_another_project(self):
        with db.connection(self.alpha) as conn:
            rows = sampling.sample_cohort_rows(
                conn, str(self.beta), CohortSpec(any_n=10, url_contains="")
            )
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
