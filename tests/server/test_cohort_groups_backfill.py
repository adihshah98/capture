"""One-shot rewrite of stored purchased/all/any sampling into A/B groups."""

import json
import sys
import unittest
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

from capture import db


class TestGroupsFieldsFromLegacy(unittest.TestCase):
    def test_purchase_split_becomes_ab_with_outcome_slices(self):
        out = db.groups_fields_from_legacy(
            {
                "purchased_n": 2,
                "all_n": 3,
                "slice": {"device": ["mobile"]},
                "selection": "Chosen as the latest 2 purchased + 3 non-purchase",
            }
        )
        self.assertEqual(out["a_n"], 2)
        self.assertEqual(out["b_n"], 3)
        self.assertEqual(out["slice"]["outcome"], ["purchased"])
        self.assertEqual(out["slice_b"]["outcome"], ["checkout", "rest"])
        self.assertEqual(out["slice"]["device"], ["mobile"])
        self.assertNotIn("purchased_n", out)
        self.assertEqual(
            out["selection"], "Chosen as the latest 2 purchased + 3 non-purchase"
        )

    def test_any_becomes_group_a(self):
        out = db.groups_fields_from_legacy({"any_n": 7, "slice": {"page": "/p"}})
        self.assertEqual(out["a_n"], 7)
        self.assertEqual(out.get("b_n"), None)
        self.assertEqual(out["slice"], {"page": "/p"})
        self.assertNotIn("any_n", out)

    def test_already_grouped_strips_the_old_keys(self):
        out = db.groups_fields_from_legacy(
            {"a_n": 10, "b_n": 4, "purchased_n": 0, "all_n": 0, "any_n": 0}
        )
        self.assertEqual(out, {"a_n": 10, "b_n": 4})


class TestCohortGroupsBackfill(pgfixture.CohortStoreMixin, unittest.TestCase):
    def test_rewrites_stored_fields_and_session_tags(self):
        with psycopg.connect(self._pg_admin) as conn:
            conn.execute(
                """
                INSERT INTO cohorts (project_id, cohort_id, status, fields)
                VALUES (%s, 'cohort-legacy', 'idle', %s::jsonb)
                """,
                (
                    self._pg_project,
                    json.dumps(
                        {
                            "purchased_n": 2,
                            "all_n": 3,
                            "selection": "keep this sentence",
                            "slice": {"device": ["mobile"]},
                        }
                    ),
                ),
            )
            conn.execute(
                """
                INSERT INTO cohort_sessions
                    (project_id, cohort_id, session_id, position, fields)
                VALUES
                    (%s, 'cohort-legacy', 'buy', 0, %s::jsonb),
                    (%s, 'cohort-legacy', 'rest', 1, %s::jsonb)
                """,
                (
                    self._pg_project,
                    json.dumps({"filter": "purchased"}),
                    self._pg_project,
                    json.dumps({"filter": "all"}),
                ),
            )
            db.rewrite_legacy_cohort_fields(conn)
            conn.commit()
            fields = conn.execute(
                "SELECT fields FROM cohorts WHERE cohort_id = 'cohort-legacy'"
            ).fetchone()[0]
            tags = [
                row[0]
                for row in conn.execute(
                    "SELECT fields->>'filter' FROM cohort_sessions"
                    " WHERE cohort_id = 'cohort-legacy' ORDER BY position"
                ).fetchall()
            ]

        self.assertEqual(fields["a_n"], 2)
        self.assertEqual(fields["b_n"], 3)
        self.assertEqual(fields["selection"], "keep this sentence")
        self.assertNotIn("purchased_n", fields)
        self.assertEqual(tags, ["a", "b"])

    def test_rewrite_is_idempotent(self):
        with psycopg.connect(self._pg_admin) as conn:
            conn.execute(
                """
                INSERT INTO cohorts (project_id, cohort_id, status, fields)
                VALUES (%s, 'cohort-once', 'idle', %s::jsonb)
                """,
                (self._pg_project, json.dumps({"any_n": 4})),
            )
            db.rewrite_legacy_cohort_fields(conn)
            db.rewrite_legacy_cohort_fields(conn)
            conn.commit()
            fields = conn.execute(
                "SELECT fields FROM cohorts WHERE cohort_id = 'cohort-once'"
            ).fetchone()[0]
        self.assertEqual(fields, {"a_n": 4})


if __name__ == "__main__":
    unittest.main()
