"""Chunk storage: local and S3, and the dual-read that carries the migration.

The S3 backend runs against a fake client. The point is to exercise key
construction, the disk fallback and the merged listing — not boto3.
"""

import tempfile
import unittest
from pathlib import Path

from capture import storage


class FakeS3:
    """Enough of the S3 API for storage.py, and a record of what was asked."""

    def __init__(self):
        self.objects: dict = {}
        self.put_kwargs: dict = {}
        self.presigned: list = []

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body
        self.put_kwargs[Key] = kw

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)

        class Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": Body(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {
            "Contents": [{"Key": k, "Size": len(self.objects[k])} for k in keys],
            "IsTruncated": False,
        }

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append(Params["Key"])
        return f"https://s3.test/{Params['Key']}?exp={ExpiresIn}"


def chunk_name(ts: int, window: str = "aaaa1111", suffix: str = "abc123") -> str:
    return f"{ts}-{window}-{suffix}.json"


class StorageCase(unittest.IsolatedAsyncioTestCase):
    s3 = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self._saved = (storage.DATA_DIR, storage.PROJECTS_DIR, storage.BUCKET, storage._client)
        storage.DATA_DIR = root / "sessions"
        storage.PROJECTS_DIR = root / "projects"
        storage.BUCKET = "capture-test" if self.s3 else ""
        self.fake = FakeS3()
        storage._client = self.fake
        self.addCleanup(self.restore)

    def restore(self):
        storage.DATA_DIR, storage.PROJECTS_DIR, storage.BUCKET, storage._client = self._saved


class TestKeys(StorageCase):
    def test_the_default_pool_gets_a_named_prefix_not_an_empty_one(self):
        """An empty segment would collapse and put default-pool sessions at the
        bucket root, colliding with the project directories."""
        self.assertEqual(storage.key_for("", "sess", "c.json"), "sessions/_default/sess/c.json")
        self.assertEqual(storage.session_key_prefix("", "sess"), "sessions/_default/sess/")

    def test_a_project_key_uses_its_storage_prefix(self):
        self.assertEqual(
            storage.key_for("cap_abc", "sess", "c.json"), "sessions/cap_abc/sess/c.json"
        )

    def test_the_session_prefix_cannot_match_a_sibling_session(self):
        prefix = storage.session_key_prefix("cap_abc", "sess")
        self.assertFalse(storage.key_for("cap_abc", "sess2", "c.json").startswith(prefix))

    def test_analysis_keys_sit_beside_chunks_not_among_them(self):
        self.assertEqual(
            storage.analysis_key("cap_abc", "sess", "digest.json"),
            "sessions/cap_abc/sess/.analysis/digest.json",
        )
        self.assertTrue(
            storage.analysis_key("cap_abc", "sess", "digest.json").startswith(
                storage.analysis_key_prefix("cap_abc", "sess")
            )
        )
        self.assertFalse(
            storage.analysis_key("cap_abc", "sess2", "digest.json").startswith(
                storage.analysis_key_prefix("cap_abc", "sess")
            )
        )


class TestLocalBackend(StorageCase):
    def test_round_trip(self):
        name = chunk_name(1000)
        storage.put_chunk("cap_a", "s1", name, b'{"events":[]}')
        self.assertEqual(storage.open_chunk("cap_a", "s1", name), b'{"events":[]}')

    def test_nothing_goes_to_s3(self):
        storage.put_chunk("cap_a", "s1", chunk_name(1000), b"x")
        self.assertEqual(self.fake.objects, {})

    def test_listing_ignores_sidecars(self):
        storage.put_chunk("cap_a", "s1", chunk_name(2000), b"22")
        storage.put_chunk("cap_a", "s1", chunk_name(1000), b"1")
        (storage.local_dir("cap_a", "s1") / "visitor.json").write_text("{}")
        self.assertEqual(
            storage.list_chunks("cap_a", "s1"),
            [storage.Chunk(chunk_name(1000), 1), storage.Chunk(chunk_name(2000), 2)],
        )

    def test_analysis_round_trip_stays_out_of_the_chunk_listing(self):
        storage.put_analysis_file("cap_a", "s1", "digest.json", b'{"frames":[]}')
        storage.put_analysis_file("cap_a", "s1", "f0000.jpg", b"jpeg")
        self.assertEqual(storage.open_analysis_file("cap_a", "s1", "digest.json"), b'{"frames":[]}')
        self.assertEqual(
            [c.name for c in storage.list_analysis_files("cap_a", "s1")],
            ["digest.json", "f0000.jpg"],
        )
        self.assertEqual(storage.list_chunks("cap_a", "s1"), [])

    def test_a_junk_name_is_not_an_analysis_file(self):
        with self.assertRaises(ValueError):
            storage.put_analysis_file("cap_a", "s1", "../chunk.json", b"x")
        self.assertIsNone(storage.open_analysis_file("cap_a", "s1", "../chunk.json"))

    def test_missing_chunk_is_none_not_an_error(self):
        self.assertIsNone(storage.open_chunk("cap_a", "s1", chunk_name(1)))

    def test_no_presigned_url_without_a_bucket(self):
        storage.put_chunk("cap_a", "s1", chunk_name(1000), b"x")
        self.assertIsNone(storage.chunk_url("cap_a", "s1", chunk_name(1000)))


class TestS3Backend(StorageCase):
    s3 = True

    def test_put_goes_to_the_bucket_not_the_disk(self):
        name = chunk_name(1000)
        storage.put_chunk("cap_a", "s1", name, b'{"events":[]}')
        self.assertEqual(list(self.fake.objects), ["sessions/cap_a/s1/" + name])
        self.assertFalse((storage.local_dir("cap_a", "s1") / name).exists())

    def test_round_trip(self):
        name = chunk_name(1000)
        storage.put_chunk("cap_a", "s1", name, b"payload")
        self.assertEqual(storage.open_chunk("cap_a", "s1", name), b"payload")

    def test_presigned_url_is_returned_for_a_key_that_exists(self):
        name = chunk_name(1000)
        storage.put_chunk("cap_a", "s1", name, b"x")
        url = storage.chunk_url("cap_a", "s1", name)
        self.assertIn(f"sessions/cap_a/s1/{name}", url)

    def test_chunks_are_stored_immutable_and_cacheable(self):
        """A redirect cannot set caching for the object it points at, so the
        header has to be on the object or every replay re-downloads."""
        name = chunk_name(1000)
        storage.put_chunk("cap_a", "s1", name, b"x")
        kw = self.fake.put_kwargs[f"sessions/cap_a/s1/{name}"]
        self.assertEqual(kw["CacheControl"], storage.CHUNK_CACHE_CONTROL)
        self.assertEqual(kw["ContentType"], "application/json")

    def test_no_presigned_url_for_a_key_that_is_not_there_yet(self):
        """The caller must fall back to serving the bytes, or a not-yet-copied
        session would 403 from S3 instead of replaying from disk."""
        self.assertIsNone(storage.chunk_url("cap_a", "s1", chunk_name(1000)))


class TestDualReadDuringMigration(StorageCase):
    s3 = True

    def setUp(self):
        super().setUp()
        # One chunk copied to S3, one still only on disk.
        self.copied, self.pending = chunk_name(1000), chunk_name(2000)
        self.fake.objects[f"sessions/cap_a/s1/{self.copied}"] = b"from-s3"
        from capture.fsutil import atomic_write_bytes

        atomic_write_bytes(storage.local_dir("cap_a", "s1") / self.copied, b"from-disk")
        atomic_write_bytes(storage.local_dir("cap_a", "s1") / self.pending, b"only-on-disk")

    def test_s3_wins_when_a_chunk_is_in_both(self):
        self.assertEqual(storage.open_chunk("cap_a", "s1", self.copied), b"from-s3")

    def test_a_chunk_not_yet_copied_still_reads_from_disk(self):
        self.assertEqual(storage.open_chunk("cap_a", "s1", self.pending), b"only-on-disk")

    def test_the_listing_merges_both_backends(self):
        names = [c.name for c in storage.list_chunks("cap_a", "s1")]
        self.assertEqual(names, [self.copied, self.pending])

    async def test_delete_clears_both(self):
        freed = storage.delete_session("cap_a", "s1")
        self.assertGreater(freed, 0)
        self.assertEqual(self.fake.objects, {})
        self.assertFalse(storage.local_dir("cap_a", "s1").exists())
        self.assertEqual(storage.list_chunks("cap_a", "s1"), [])


class TestAnalysisOnS3(StorageCase):
    s3 = True

    def test_put_goes_to_the_bucket_not_the_disk(self):
        storage.put_analysis_file("cap_a", "s1", "digest.json", b'{"frames":[]}')
        storage.put_analysis_file("cap_a", "s1", "f0000.jpg", b"jpeg")
        self.assertEqual(
            sorted(self.fake.objects),
            [
                "sessions/cap_a/s1/.analysis/digest.json",
                "sessions/cap_a/s1/.analysis/f0000.jpg",
            ],
        )
        self.assertFalse(
            (storage.local_dir("cap_a", "s1") / storage.ANALYSIS_DIR / "digest.json").exists()
        )

    def test_round_trip(self):
        storage.put_analysis_file("cap_a", "s1", "digest.json", b'{"ok":true}')
        self.assertEqual(storage.open_analysis_file("cap_a", "s1", "digest.json"), b'{"ok":true}')

    def test_listing_chunks_does_not_see_the_digest(self):
        name = chunk_name(1000)
        storage.put_chunk("cap_a", "s1", name, b"chunk")
        storage.put_analysis_file("cap_a", "s1", "digest.json", b"{}")
        self.assertEqual([c.name for c in storage.list_chunks("cap_a", "s1")], [name])
        self.assertEqual(
            [c.name for c in storage.list_analysis_files("cap_a", "s1")], ["digest.json"]
        )

    async def test_delete_session_removes_the_digest_too(self):
        """Retention used to rebuild keys from chunk names, which misses
        `.analysis/` and would orphan every persisted reconstruction."""
        storage.put_chunk("cap_a", "s1", chunk_name(1000), b"chunk")
        storage.put_analysis_file("cap_a", "s1", "digest.json", b"{}")
        storage.put_analysis_file("cap_a", "s1", "f0000.jpg", b"jpeg")
        storage.delete_session("cap_a", "s1")
        self.assertEqual(self.fake.objects, {})

    def test_delete_analysis_leaves_the_recording(self):
        name = chunk_name(1000)
        storage.put_chunk("cap_a", "s1", name, b"chunk")
        storage.put_analysis_file("cap_a", "s1", "digest.json", b"{}")
        storage.delete_analysis("cap_a", "s1")
        self.assertEqual(list(self.fake.objects), [f"sessions/cap_a/s1/{name}"])
        self.assertEqual(storage.list_analysis_files("cap_a", "s1"), [])

    def test_content_type_follows_the_file(self):
        storage.put_analysis_file("cap_a", "s1", "digest.json", b"{}")
        storage.put_analysis_file("cap_a", "s1", "f0000.jpg", b"jpeg")
        self.assertEqual(
            self.fake.put_kwargs["sessions/cap_a/s1/.analysis/digest.json"]["ContentType"],
            "application/json",
        )
        self.assertEqual(
            self.fake.put_kwargs["sessions/cap_a/s1/.analysis/f0000.jpg"]["ContentType"],
            "image/jpeg",
        )


# Chunk-name parsing moved to capture/paths.py with the rest of the naming
# rules; tests/test_paths.py covers both helpers, and every case that was here.


if __name__ == "__main__":
    unittest.main()
