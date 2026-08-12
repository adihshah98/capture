"""The compat schema lets analyze.py's unmodified SQLite-era queries run against
Postgres. These call the real analyze.py functions — no reimplementation."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
import psycopg

from capture import analyze, compat, db
from capture.analyze.spec import CohortSpec


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class CompatCase(unittest.TestCase):
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
        conn.execute(
            "INSERT INTO sessions (project_id, session_id, first_seen, last_seen,"
            " chunk_count, clicks, url, device, attribution)"
            " VALUES (%s, %s, 1000, 60000, 2, %s, %s, %s, %s)",
            (
                project,
                sid,
                clicks,
                url,
                json.dumps({"width": width, "height": 800}) if width else None,
                json.dumps({"utm_source": "meta"}) if "utm_source" in url else None,
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


class TestAnalyzeRunsUnmodified(CompatCase):
    def test_pool_facets_reads_through_the_view(self):
        with compat.cohort_connection(self.alpha) as conn:
            facets = analyze.pool_facets(conn, str(self.alpha))
        self.assertIn("outcome", facets)
        flat = json.dumps(facets)
        self.assertIn("meta", flat)
        self.assertNotIn("other.test", flat)

    def test_sample_cohort_rows_returns_this_project_only(self):
        with compat.cohort_connection(self.alpha) as conn:
            rows = analyze.sample_cohort_rows(
                conn, str(self.alpha), CohortSpec(any_n=10, url_contains="")
            )
        ids = sorted(r["session_id"] for r in rows)
        self.assertEqual(ids, ["a-bought", "a-browsed"])

    def test_a_purchase_survives_the_json_round_trip(self):
        """The view casts jsonb back to text because _parse_index_json calls
        json.loads; a dict would silently degrade to None, and every session
        would look like it never bought anything."""
        with compat.cohort_connection(self.alpha) as conn:
            rows = analyze.sample_cohort_rows(
                conn, str(self.alpha), CohortSpec(any_n=10, url_contains="")
            )
        classified = {r["session_id"]: r["filter"] for r in rows}
        self.assertEqual(classified["a-bought"], "purchased")
        self.assertNotEqual(classified["a-browsed"], "purchased")

    def test_the_view_hands_back_json_as_text_not_dicts(self):
        with compat.cohort_connection(self.alpha) as conn:
            order, interactions = conn.execute(
                'SELECT "order", interactions FROM sessions WHERE session_id = ?',
                ("a-bought",),
            ).fetchone()
        self.assertIsInstance(order, str)
        self.assertIsInstance(interactions, str)
        self.assertEqual(json.loads(order)["purchased_at"], 55000)
        self.assertEqual(json.loads(interactions)["clicks"], 5)

    def test_build_cohort_composes_a_group(self):
        with compat.cohort_connection(self.alpha) as conn:
            cohort = analyze.build_cohort(
                conn, str(self.alpha), CohortSpec(any_n=2, url_contains="")
            )
        ids = sorted(s["session_id"] for s in cohort["sessions"])
        self.assertEqual(ids, ["a-bought", "a-browsed"])

    def test_clicks_column_orders_without_json_extract(self):
        with compat.cohort_connection(self.alpha) as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE pool = ?"
                " ORDER BY coalesce(clicks, 0) DESC",
                (str(self.alpha),),
            ).fetchall()
        self.assertEqual([r[0] for r in rows], ["a-bought", "a-browsed"])

    def test_json_extract_shim_is_gone(self):
        with psycopg.connect(self.admin_url) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'compat' AND p.proname = 'json_extract'"
            ).fetchone()
        self.assertIsNone(row)


class TestCompatViewKeepsTenantsApart(CompatCase):
    def test_the_view_cannot_leak_another_project(self):
        with compat.cohort_connection(self.alpha) as conn:
            rows = conn.execute("SELECT session_id FROM sessions").fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ["a-bought", "a-browsed"])

    def test_naming_another_project_through_the_view_returns_nothing(self):
        with compat.cohort_connection(self.alpha) as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE pool = ?", (str(self.beta),)
            ).fetchall()
        self.assertEqual(rows, [])

    def test_analyze_cannot_sample_another_project(self):
        with compat.cohort_connection(self.alpha) as conn:
            rows = analyze.sample_cohort_rows(
                conn, str(self.beta), CohortSpec(any_n=10, url_contains="")
            )
        self.assertEqual(rows, [])


class TestPlaceholderTranslation(unittest.TestCase):
    def test_question_marks_become_psycopg_placeholders(self):
        self.assertEqual(
            compat.translate("WHERE a = ? AND b = ?", (1, 2)), "WHERE a = %s AND b = %s"
        )

    def test_literal_percent_is_escaped_before_translation(self):
        self.assertEqual(
            compat.translate("WHERE url LIKE '%x%' AND a = ?", (1,)),
            "WHERE url LIKE '%%x%%' AND a = %s",
        )

    def test_paramless_sql_is_left_alone(self):
        self.assertEqual(compat.translate("SELECT 100 % 7", ()), "SELECT 100 % 7")


if __name__ == "__main__":
    unittest.main()
