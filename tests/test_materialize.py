"""Staging chunks for analyze.py.

analyze.py reads chunks as files and writes its results back beside them. These
pin the two properties that makes safe: results survive, and nothing the shim
fetched is left behind.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_storage import FakeS3, chunk_name

from capture import storage


class Project:
    def __init__(self, pool: Path, storage_prefix: str):
        self.pool = pool
        self.storage_prefix = storage_prefix


class MaterializeCase(unittest.TestCase):
    def setUp(self):
        from capture import server

        self.server = server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self._saved = (storage.DATA_DIR, storage.PROJECTS_DIR, storage.BUCKET, storage._client)
        storage.DATA_DIR = root / "sessions"
        storage.PROJECTS_DIR = root / "projects"
        storage.BUCKET = "capture-test"
        self.fake = FakeS3()
        storage._client = self.fake
        self.addCleanup(self.restore)

        self.project = Project(storage.PROJECTS_DIR / "cap_a" / "sessions", "cap_a")
        self.session = "s1"
        self.dir = self.project.pool / self.session

    def restore(self):
        storage.DATA_DIR, storage.PROJECTS_DIR, storage.BUCKET, storage._client = self._saved

    def put_remote(self, name: str, body: bytes):
        self.fake.objects[f"sessions/cap_a/{self.session}/{name}"] = body

    def run_with_staging(self, body):
        async def main():
            async with self.server.chunks_on_disk(self.project, [self.session]):
                body()

        asyncio.run(main())


class TestStaging(MaterializeCase):
    def test_chunks_are_present_inside_the_block(self):
        name = chunk_name(1000)
        self.put_remote(name, b'{"events":[]}')
        seen = {}
        self.run_with_staging(
            lambda: seen.update(
                exists=(self.dir / name).exists(),
                body=(self.dir / name).read_bytes(),
            )
        )
        self.assertTrue(seen["exists"])
        self.assertEqual(seen["body"], b'{"events":[]}')

    def test_fetched_chunks_are_removed_afterwards(self):
        name = chunk_name(1000)
        self.put_remote(name, b"x")
        self.run_with_staging(lambda: None)
        self.assertFalse((self.dir / name).exists())

    def test_results_written_during_the_block_survive(self):
        """The reason chunks stage into the real directory rather than a temp
        one: analyze.py writes its frame cache here and it must outlive the run."""
        name = chunk_name(1000)
        self.put_remote(name, b"x")

        def write_result():
            (self.dir / ".analysis").mkdir(exist_ok=True)
            (self.dir / ".analysis" / "digest.json").write_text('{"kept": true}')

        self.run_with_staging(write_result)
        self.assertEqual(
            json.loads((self.dir / ".analysis" / "digest.json").read_text())["kept"], True
        )

    def test_a_chunk_already_on_disk_is_not_deleted(self):
        """Only what the shim fetched is cleaned up, or staging would destroy
        the local copy of a session that has not been migrated yet."""
        from capture.fsutil import atomic_write_bytes

        local = chunk_name(1000)
        atomic_write_bytes(self.dir / local, b"local-copy")
        self.put_remote(local, b"remote-copy")

        self.run_with_staging(lambda: None)
        self.assertTrue((self.dir / local).exists())
        self.assertEqual((self.dir / local).read_bytes(), b"local-copy")

    def test_staging_covers_every_session_in_a_cohort(self):
        for sid in ("s1", "s2", "s3"):
            self.fake.objects[f"sessions/cap_a/{sid}/{chunk_name(1000)}"] = b"x"

        present = {}

        async def main():
            async with self.server.chunks_on_disk(self.project, ["s1", "s2", "s3"]):
                for sid in ("s1", "s2", "s3"):
                    present[sid] = (self.project.pool / sid / chunk_name(1000)).exists()

        asyncio.run(main())
        self.assertEqual(present, {"s1": True, "s2": True, "s3": True})
        for sid in ("s1", "s2", "s3"):
            self.assertFalse((self.project.pool / sid / chunk_name(1000)).exists())

    def test_cleanup_happens_even_when_the_body_raises(self):
        name = chunk_name(1000)
        self.put_remote(name, b"x")

        async def main():
            async with self.server.chunks_on_disk(self.project, [self.session]):
                raise RuntimeError("analysis blew up")

        with self.assertRaises(RuntimeError):
            asyncio.run(main())
        self.assertFalse((self.dir / name).exists())

    def test_it_is_a_no_op_without_a_bucket(self):
        storage.BUCKET = ""
        from capture.fsutil import atomic_write_bytes

        name = chunk_name(1000)
        atomic_write_bytes(self.dir / name, b"local")
        self.run_with_staging(lambda: None)
        self.assertTrue((self.dir / name).exists())


class TestCohortSessionIds(unittest.TestCase):
    def test_it_reads_ids_and_skips_malformed_rows(self):
        from capture import server

        cohort = {"sessions": [{"session_id": "a"}, {}, None, {"session_id": "b"}]}
        self.assertEqual(server._cohort_session_ids(cohort), ["a", "b"])

    def test_an_empty_cohort_is_fine(self):
        from capture import server

        self.assertEqual(server._cohort_session_ids({}), [])


if __name__ == "__main__":
    unittest.main()
