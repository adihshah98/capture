"""The backlog copier: idempotent, resumable, and it never deletes a local file."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


import pgfixture
from test_storage import FakeS3, chunk_name


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class TestCopier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import shutil

        from capture import db, projects, s3copy, storage

        cls.s3copy, cls.storage, cls.db, cls.projects = s3copy, storage, db, projects
        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-copy-"))
        cls.addClassCleanup(shutil.rmtree, cls.data_dir, ignore_errors=True)

        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        pgfixture.seed_project(cls.admin_url, "Copied", "cap_c")

        cls._saved = (
            os.environ.get("CAPTURE_DATABASE_URL"),
            projects.DATA_DIR,
            projects.PROJECTS_DIR,
            storage.DATA_DIR,
            storage.PROJECTS_DIR,
            storage.BUCKET,
            storage._client,
        )
        cls.addClassCleanup(cls.restore)
        os.environ["CAPTURE_DATABASE_URL"] = cls.app_url
        projects.DATA_DIR = storage.DATA_DIR = cls.data_dir / "sessions"
        projects.PROJECTS_DIR = storage.PROJECTS_DIR = cls.data_dir / "projects"
        storage.BUCKET = "capture-test"
        db.close_pool()
        projects.invalidate()

    @classmethod
    def restore(cls):
        url, p_data, p_proj, s_data, s_proj, bucket, client = cls._saved
        cls.db.close_pool()
        if url is None:
            os.environ.pop("CAPTURE_DATABASE_URL", None)
        else:
            os.environ["CAPTURE_DATABASE_URL"] = url
        cls.projects.DATA_DIR, cls.projects.PROJECTS_DIR = p_data, p_proj
        cls.storage.DATA_DIR, cls.storage.PROJECTS_DIR = s_data, s_proj
        cls.storage.BUCKET, cls.storage._client = bucket, client
        cls.projects.invalidate()

    def setUp(self):
        import shutil

        self.fake = FakeS3()
        self.storage._client = self.fake
        shutil.rmtree(self.storage.PROJECTS_DIR, ignore_errors=True)

    def seed_local(self, session_id: str, count: int = 3):
        from capture.fsutil import atomic_write_bytes

        names = []
        for i in range(count):
            name = chunk_name(1000 + i)
            atomic_write_bytes(
                self.storage.local_dir("cap_c", session_id) / name, b"chunk" + bytes([i])
            )
            names.append(name)
        # A sidecar the copier must ignore.
        (self.storage.local_dir("cap_c", session_id) / "visitor.json").write_text("{}")
        return names

    def test_it_uploads_every_chunk_and_no_sidecars(self):
        self.seed_local("s1")
        progress = self.s3copy.copy_all(concurrency=2, verbose=False)
        self.assertEqual(progress.uploaded, 3)
        self.assertEqual(
            sorted(self.fake.objects),
            sorted(f"sessions/cap_c/s1/{n}" for n in self.seed_names()),
        )

    def seed_names(self):
        return [chunk_name(1000 + i) for i in range(3)]

    def test_rerunning_uploads_nothing(self):
        self.seed_local("s1")
        self.s3copy.copy_all(concurrency=2, verbose=False)
        again = self.s3copy.copy_all(concurrency=2, verbose=False)
        self.assertEqual(again.uploaded, 0)
        self.assertEqual(again.skipped, 3)

    def test_an_interrupted_run_resumes(self):
        names = self.seed_local("s1")
        self.fake.objects[f"sessions/cap_c/s1/{names[0]}"] = b"chunk\x00"

        progress = self.s3copy.copy_all(concurrency=2, verbose=False)
        self.assertEqual((progress.uploaded, progress.skipped), (2, 1))
        self.assertEqual(len(self.fake.objects), 3)

    def test_local_files_are_left_alone(self):
        """Keeping them is what makes the switch reversible, and reads still
        fall back to them for anything not yet copied."""
        names = self.seed_local("s1")
        self.s3copy.copy_all(concurrency=2, verbose=False)
        for name in names:
            self.assertTrue((self.storage.local_dir("cap_c", "s1") / name).exists())

    def test_content_survives_the_copy(self):
        names = self.seed_local("s1")
        self.s3copy.copy_all(concurrency=2, verbose=False)
        for i, name in enumerate(names):
            self.assertEqual(self.fake.objects[f"sessions/cap_c/s1/{name}"], b"chunk" + bytes([i]))

    def test_many_sessions_across_workers(self):
        for n in range(12):
            self.seed_local(f"s{n:02d}", count=2)
        progress = self.s3copy.copy_all(concurrency=6, verbose=False)
        self.assertEqual(progress.uploaded, 24)
        self.assertEqual(progress.sessions, 12)
        self.assertEqual(len(self.fake.objects), 24)

    def test_dry_run_writes_nothing(self):
        self.seed_local("s1")
        progress = self.s3copy.copy_all(concurrency=2, dry_run=True, verbose=False)
        self.assertEqual(progress.uploaded, 3)
        self.assertEqual(self.fake.objects, {})

    def test_limit_caps_the_work(self):
        for n in range(5):
            self.seed_local(f"s{n}", count=1)
        progress = self.s3copy.copy_all(concurrency=2, limit=2, verbose=False)
        self.assertEqual(progress.sessions, 2)
        self.assertEqual(progress.uploaded, 2)

    def test_a_local_digest_is_copied_next_to_the_chunks(self):
        names = self.seed_local("s1", count=1)
        analysis = self.storage.local_dir("cap_c", "s1") / self.storage.ANALYSIS_DIR
        analysis.mkdir()
        (analysis / "digest.json").write_bytes(b'{"frames":[]}')
        (analysis / "f0000.jpg").write_bytes(b"jpeg")
        progress = self.s3copy.copy_all(concurrency=2, verbose=False)
        self.assertEqual(progress.uploaded, 3)
        self.assertEqual(
            self.fake.objects[f"sessions/cap_c/s1/{names[0]}"],
            b"chunk\x00",
        )
        self.assertEqual(
            self.fake.objects["sessions/cap_c/s1/.analysis/digest.json"],
            b'{"frames":[]}',
        )
        self.assertEqual(self.fake.objects["sessions/cap_c/s1/.analysis/f0000.jpg"], b"jpeg")


if __name__ == "__main__":
    unittest.main()
