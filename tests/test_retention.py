"""Retention has to clear both backends.

An expired session that loses its directory and its index row but keeps its
objects is invisible: nothing points at them, and no later sweep can find them,
so they sit in the bucket until someone notices the bill.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
import psycopg
from test_storage import FakeS3, chunk_name


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class TestSweepClearsBothBackends(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil

        from capture import db, projects, server, storage

        cls.server, cls.storage, cls.db, cls.projects = server, storage, db, projects
        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-retention-"))
        cls.addClassCleanup(shutil.rmtree, cls.data_dir, ignore_errors=True)

        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        cls.project_id = pgfixture.seed_project(cls.admin_url, "Retained", "cap_r")

        cls._saved = (
            os.environ.get("CAPTURE_DATABASE_URL"),
            projects.DATA_DIR, projects.PROJECTS_DIR,
            storage.DATA_DIR, storage.PROJECTS_DIR, storage.BUCKET, storage._client,
            server.DATA_ROOT, server.RETENTION_DAYS,
        )
        cls.addClassCleanup(cls.restore)
        os.environ["CAPTURE_DATABASE_URL"] = cls.app_url
        projects.DATA_DIR = storage.DATA_DIR = cls.data_dir / "sessions"
        projects.PROJECTS_DIR = storage.PROJECTS_DIR = cls.data_dir / "projects"
        server.DATA_ROOT = cls.data_dir
        server.RETENTION_DAYS = 7
        storage.BUCKET = "capture-test"
        db.close_pool()
        projects.invalidate()

    @classmethod
    def restore(cls):
        (url, p_data, p_proj, s_data, s_proj, bucket, client,
         data_root, retention) = cls._saved
        cls.db.close_pool()
        if url is None:
            os.environ.pop("CAPTURE_DATABASE_URL", None)
        else:
            os.environ["CAPTURE_DATABASE_URL"] = url
        cls.projects.DATA_DIR, cls.projects.PROJECTS_DIR = p_data, p_proj
        cls.storage.DATA_DIR, cls.storage.PROJECTS_DIR = s_data, s_proj
        cls.storage.BUCKET, cls.storage._client = bucket, client
        cls.server.DATA_ROOT, cls.server.RETENTION_DAYS = data_root, retention
        cls.projects.invalidate()

    def setUp(self):
        self.fake = FakeS3()
        self.storage._client = self.fake
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute("DELETE FROM sessions")

    def seed(self, session_id: str, last_seen_ms: int):
        from capture.fsutil import atomic_write_bytes

        name = chunk_name(1000)
        self.fake.objects[f"sessions/cap_r/{session_id}/{name}"] = b"remote"
        atomic_write_bytes(self.storage.local_dir("cap_r", session_id) / name, b"local")
        with psycopg.connect(self.admin_url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO sessions (project_id, session_id, first_seen, last_seen,"
                " chunk_count, bytes) VALUES (%s, %s, %s, %s, 1, 6)",
                (self.project_id, session_id, last_seen_ms, last_seen_ms),
            )
        return name

    def rows(self):
        with psycopg.connect(self.admin_url) as conn:
            return [r[0] for r in conn.execute("SELECT session_id FROM sessions")]

    def test_an_expired_session_leaves_nothing_in_the_bucket(self):
        import time

        old = int((time.time() - 30 * 86400) * 1000)
        name = self.seed("stale", old)

        removed = self.server.sweep_expired_sessions()

        self.assertEqual(removed, 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.fake.objects, {}, "chunks were orphaned in the bucket")
        self.assertFalse((self.storage.local_dir("cap_r", "stale") / name).exists())

    def test_a_live_session_is_untouched(self):
        import time

        name = self.seed("fresh", int(time.time() * 1000) - 3600_000)

        self.assertEqual(self.server.sweep_expired_sessions(), 0)
        self.assertEqual(self.rows(), ["fresh"])
        self.assertEqual(len(self.fake.objects), 1)
        self.assertTrue((self.storage.local_dir("cap_r", "fresh") / name).exists())

    def test_retention_disabled_removes_nothing(self):
        import time

        self.seed("stale", int((time.time() - 30 * 86400) * 1000))
        self.server.RETENTION_DAYS = 0
        try:
            self.assertEqual(self.server.sweep_expired_sessions(), 0)
        finally:
            self.server.RETENTION_DAYS = 7
        self.assertEqual(self.rows(), ["stale"])
        self.assertEqual(len(self.fake.objects), 1)


if __name__ == "__main__":
    unittest.main()
