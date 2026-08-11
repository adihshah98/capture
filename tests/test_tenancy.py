"""Tenant isolation is enforced by the database, not by every query remembering
a WHERE clause. These tests deliberately write queries with no project filter."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg

from capture import db
import pgfixture


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class TenancyCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        cls.prev_url = os.environ.get("CAPTURE_DATABASE_URL")
        os.environ["CAPTURE_DATABASE_URL"] = cls.app_url
        db.close_pool()
        with psycopg.connect(cls.admin_url, autocommit=True) as conn:
            cls.alpha = conn.execute(
                "INSERT INTO projects (name) VALUES ('alpha') RETURNING id"
            ).fetchone()[0]
            cls.beta = conn.execute(
                "INSERT INTO projects (name) VALUES ('beta') RETURNING id"
            ).fetchone()[0]
            for project, sid in ((cls.alpha, "alpha-1"), (cls.alpha, "alpha-2"), (cls.beta, "beta-1")):
                conn.execute(
                    "INSERT INTO sessions (project_id, session_id, first_seen, last_seen)"
                    " VALUES (%s, %s, 1, 2)",
                    (project, sid),
                )

    @classmethod
    def tearDownClass(cls):
        db.close_pool()
        if cls.prev_url is None:
            os.environ.pop("CAPTURE_DATABASE_URL", None)
        else:
            os.environ["CAPTURE_DATABASE_URL"] = cls.prev_url

    def all_sessions(self, conn) -> list:
        return sorted(r[0] for r in conn.execute("SELECT session_id FROM sessions"))


class TestRowLevelSecurity(TenancyCase):
    def test_unfiltered_query_sees_only_the_scoped_project(self):
        with db.connection(self.alpha) as conn:
            self.assertEqual(self.all_sessions(conn), ["alpha-1", "alpha-2"])
        with db.connection(self.beta) as conn:
            self.assertEqual(self.all_sessions(conn), ["beta-1"])

    def test_a_project_cannot_read_another_by_naming_it(self):
        with db.connection(self.alpha) as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE project_id = %s", (self.beta,)
            ).fetchall()
        self.assertEqual(rows, [])

    def test_writing_into_another_project_is_refused(self):
        # An RLS WITH CHECK failure surfaces as insufficient_privilege (42501).
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with db.connection(self.alpha) as conn:
                conn.execute(
                    "INSERT INTO sessions (project_id, session_id, first_seen, last_seen)"
                    " VALUES (%s, 'smuggled', 1, 2)",
                    (self.beta,),
                )

    def test_deleting_another_project_row_affects_nothing(self):
        with db.connection(self.alpha) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = 'beta-1'")
        with db.connection(self.beta) as conn:
            self.assertEqual(self.all_sessions(conn), ["beta-1"])

    def test_admin_scope_spans_projects(self):
        with db.connection(admin=True) as conn:
            self.assertEqual(self.all_sessions(conn), ["alpha-1", "alpha-2", "beta-1"])

    def test_unscoped_connection_is_refused_outright(self):
        with self.assertRaises(ValueError):
            with db.connection():
                pass

    def test_a_raw_connection_without_the_setting_sees_nothing(self):
        with psycopg.connect(self.app_url) as conn:
            self.assertEqual(self.all_sessions(conn), [])


class TestSchemaShape(TenancyCase):
    def test_every_tenant_table_forces_rls(self):
        expected = {
            "sessions", "session_windows", "orders",
            "cohorts", "cohort_cuts", "analyses", "analysis_jobs",
        }
        with psycopg.connect(self.admin_url) as conn:
            rows = conn.execute(
                "SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity"
            ).fetchall()
        self.assertEqual(expected - {r[0] for r in rows}, set())

    def test_a_session_cannot_exist_without_a_project(self):
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.NotNullViolation):
                conn.execute(
                    "INSERT INTO sessions (session_id, first_seen, last_seen)"
                    " VALUES ('orphan', 1, 2)"
                )

    def test_tokens_resolve_to_a_project_and_survive_rotation(self):
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO project_tokens (token, project_id) VALUES ('cap_one', %s)",
                (self.alpha,),
            )
            conn.execute(
                "INSERT INTO project_tokens (token, project_id) VALUES ('cap_two', %s)",
                (self.alpha,),
            )
            conn.execute("UPDATE project_tokens SET revoked_at = now() WHERE token = 'cap_one'")
            live = conn.execute(
                "SELECT token FROM project_tokens WHERE project_id = %s AND revoked_at IS NULL",
                (self.alpha,),
            ).fetchall()
        self.assertEqual([r[0] for r in live], ["cap_two"])
        with db.connection(self.alpha) as conn:
            self.assertEqual(self.all_sessions(conn), ["alpha-1", "alpha-2"])


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class TestReadOnlyRole(unittest.TestCase):
    """A staging mirror reading production. The role must be able to read every
    project it is scoped to and change nothing."""

    @classmethod
    def setUpClass(cls):
        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        cls.project = pgfixture.seed_default_project(cls.admin_url)
        with psycopg.connect(cls.admin_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO sessions (project_id, session_id, first_seen, last_seen)"
                " VALUES (%s, 'live-one', 1, 2)",
                (cls.project,),
            )
        cls.ro_url = pgfixture.seed_readonly_role(cls.admin_url)

    def scoped(self):
        conn = psycopg.connect(self.ro_url)
        conn.execute("SELECT set_config('app.project_id', %s, false)", (self.project,))
        return conn

    def test_it_can_read(self):
        with self.scoped() as conn:
            rows = conn.execute("SELECT session_id FROM sessions").fetchall()
        self.assertEqual(rows, [("live-one",)])

    def test_it_can_read_through_the_compat_view(self):
        with self.scoped() as conn:
            conn.execute("SET search_path = compat, public")
            rows = conn.execute("SELECT session_id FROM sessions").fetchall()
        self.assertEqual(rows, [("live-one",)])

    def test_it_cannot_insert(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.scoped() as conn:
                conn.execute(
                    "INSERT INTO sessions (project_id, session_id, first_seen, last_seen)"
                    " VALUES (%s, 'sneaky', 1, 2)",
                    (self.project,),
                )

    def test_it_cannot_delete_production_rows(self):
        """The retention sweep is the realistic path to this happening."""
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.scoped() as conn:
                conn.execute("DELETE FROM sessions WHERE session_id = 'live-one'")
        with psycopg.connect(self.admin_url) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)

    def test_it_cannot_update(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.scoped() as conn:
                conn.execute("UPDATE sessions SET bytes = 999")

    def test_it_is_still_bound_by_row_level_security(self):
        with psycopg.connect(self.ro_url) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
