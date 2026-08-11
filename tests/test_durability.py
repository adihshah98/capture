"""Crash safety: partial files must never reach disk, and a tenant config that
cannot be parsed must stop the server rather than silently merge every project
into the shared default pool."""

import gzip
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
from servercase import ServerCase, request

from capture.fsutil import atomic_write_json, atomic_write_text


def temp_files(directory: Path) -> list:
    return [p.name for p in directory.rglob("*.tmp")]


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_writes_content_and_creates_parents(self):
        path = self.dir / "nested" / "deeper" / "x.json"
        atomic_write_json(path, {"a": 1})
        self.assertEqual(json.loads(path.read_text()), {"a": 1})

    def test_leaves_no_temp_files_behind(self):
        atomic_write_json(self.dir / "x.json", {"a": 1})
        self.assertEqual(temp_files(self.dir), [])

    def test_overwrite_replaces_content_wholesale(self):
        path = self.dir / "x.json"
        atomic_write_json(path, {"first": True})
        atomic_write_json(path, {"second": True})
        self.assertEqual(json.loads(path.read_text()), {"second": True})

    def test_failure_mid_write_leaves_the_original_intact(self):
        path = self.dir / "x.json"
        atomic_write_json(path, {"good": True})

        # write() rejects a non-str, failing after the temp file already exists.
        with self.assertRaises(TypeError):
            atomic_write_text(path, object())

        self.assertEqual(json.loads(path.read_text()), {"good": True})
        self.assertEqual(temp_files(self.dir), [])

    def test_reader_never_observes_a_truncated_file(self):
        path = self.dir / "x.json"
        atomic_write_json(path, {"n": 0})
        stop = threading.Event()
        seen = []

        def reader():
            while not stop.is_set():
                try:
                    seen.append(json.loads(path.read_text()))
                except Exception as exc:
                    seen.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for n in range(200):
                atomic_write_json(path, {"n": n, "pad": "x" * 20000})
        finally:
            stop.set()
            thread.join()

        bad = [s for s in seen if isinstance(s, Exception)]
        self.assertEqual(bad, [], f"reader saw a partial file: {bad[:3]}")


class TestUnreadableRegistryFailsClosed(ServerCase):
    """The registry moved from projects.json to Postgres, but the rule is the
    same: if it cannot be read, refuse rather than fall back to a shared pool."""

    default_project = False
    env = {"CAPTURE_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/nope?connect_timeout=1"}

    def test_healthz_reports_degraded(self):
        status, body = request(f"{self.base}/healthz")
        self.assertEqual(status, 503)
        self.assertIs(json.loads(body)["checks"]["projects_readable"], False)

    def test_ingest_refuses_rather_than_guessing_a_pool(self):
        self.assertEqual(self.ingest("d" * 32, token="cap_anything"), 503)
        pool = self.data_dir / "sessions"
        stored = list(pool.iterdir()) if pool.is_dir() else []
        self.assertEqual(stored, [], "an unreadable registry must not route recordings anywhere")

    def test_untokened_ingest_also_refuses(self):
        self.assertEqual(self.ingest("e" * 32), 503)

    def test_a_database_outage_is_retryable_not_a_500(self):
        """503 tells the recorder to try again; a 500 reads as permanent."""
        self.assertNotEqual(self.ingest("f" * 32), 500)


class TestNoDefaultProjectRefusesTokenlessIngest(ServerCase):
    default_project = False

    def test_tokenless_ingest_has_nowhere_to_land(self):
        self.assertEqual(self.ingest("g" * 32), 401)


class TestValidProjectsRoutesByToken(ServerCase):
    default_project = False
    token = "cap_realprojecttokenhere00"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pgfixture.seed_project(cls.admin_url, "real", cls.token)

    def test_known_token_is_accepted(self):
        self.assertEqual(self.ingest("h" * 32, token=self.token), 200)

    def test_unknown_token_is_rejected(self):
        self.assertEqual(self.ingest("i" * 32, token="cap_bogus"), 401)


class TestReadOnlyServer(ServerCase):
    """A staging mirror pointed at production. Reads work; nothing else does."""

    env = {"CAPTURE_READ_ONLY": "1", "RETENTION_DAYS": "0"}

    def test_ingest_is_refused_without_touching_the_database(self):
        self.assertEqual(self.ingest("r" * 32), 403)

    def test_order_is_refused(self):
        body = json.dumps({"event": "checkout_completed"}).encode()
        status = request(f"{self.base}/order?session_id={'r' * 32}", method="POST", data=body)[0]
        self.assertEqual(status, 403)

    def test_nothing_is_written_to_disk(self):
        self.ingest("s" * 32)
        pool = self.data_dir / "sessions"
        self.assertEqual(list(pool.iterdir()) if pool.is_dir() else [], [])

    def test_reads_still_work(self):
        status, body = request(f"{self.base}/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 0)

    def test_healthz_reports_read_only_without_going_degraded(self):
        status, body = request(f"{self.base}/healthz")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(body)["checks"]["read_only"], True)


class TestConcurrentReaderNeverSeesPartialChunk(ServerCase):
    """A truncating write publishes the file at open() and fills it afterwards, so
    anything listing the directory can read a half-written chunk — which is exactly
    what /api/sessions/{id}/chunks/{name} would then serve. An atomic write makes
    the name appear only at rename, fully formed."""

    def test_chunks_are_never_observable_half_written(self):
        # Large chunks keep each write in flight long enough to be caught.
        events = [{"type": 3, "data": {"source": 2, "pad": "x" * 400}} for _ in range(12000)]
        body = gzip.compress(json.dumps({"events": events}).encode())
        stop = threading.Event()
        partial = []
        ingested = []

        def hammer(n):
            while not stop.is_set():
                try:
                    ingested.append(
                        request(
                            f"{self.base}/ingest?session_id={'s' * 20}{n:02d}&compression=gzip-js",
                            method="POST",
                            data=body,
                            headers={"Content-Type": "application/octet-stream"},
                        )[0]
                    )
                except Exception:
                    return

        def watcher():
            while not stop.is_set():
                for path in self.data_dir.rglob("*.json"):
                    try:
                        json.loads(path.read_text())
                    except FileNotFoundError:
                        pass
                    except Exception as exc:
                        partial.append(f"{path.name}: {exc}")

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
        threads.append(threading.Thread(target=watcher))
        for t in threads:
            t.start()
        time.sleep(8)
        stop.set()
        for t in threads:
            t.join(timeout=15)

        self.assertGreater(
            len([c for c in ingested if c == 200]), 5, "too few chunks written to be meaningful"
        )
        self.assertEqual(partial[:5], [], "a half-written chunk was readable from the pool")


if __name__ == "__main__":
    unittest.main()
