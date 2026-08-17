"""Ingest must not serialise behind its own I/O.

A chunk PUT to S3 measures ~64ms from the production box. Run inline in the
async handler that blocks the event loop, capping one worker near 15 req/s
against ~70 when the write was local. This pins the property that the wait
happens off the loop, because nothing else would notice if it regressed.
"""

import asyncio
import gzip
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

WRITE_DELAY_S = 0.06
CONCURRENCY = 8


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class TestIngestDoesNotBlockTheEventLoop(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import shutil
        import tempfile

        # Patch the already-imported modules rather than re-importing them.
        # Popping sys.modules leaves other test files holding a stale module
        # object while the server holds a fresh one, and the two disagree.
        from capture import config, db, paths, projects, server, storage

        cls.server, cls.storage, cls.db, cls.projects = server, storage, db, projects
        cls.paths, cls.config = paths, config
        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-conc-"))
        cls.addClassCleanup(shutil.rmtree, cls.data_dir, ignore_errors=True)

        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        pgfixture.seed_default_project(cls.admin_url)

        cls._saved = (
            os.environ.get("CAPTURE_DATABASE_URL"),
            projects.DATA_DIR,
            projects.PROJECTS_DIR,
            storage.DATA_DIR,
            storage.PROJECTS_DIR,
            paths.DATA_ROOT,
        )
        cls.addClassCleanup(cls.restore_environment)
        os.environ["CAPTURE_DATABASE_URL"] = cls.app_url
        projects.DATA_DIR = storage.DATA_DIR = cls.data_dir / "sessions"
        projects.PROJECTS_DIR = storage.PROJECTS_DIR = cls.data_dir / "projects"
        # ingest free-space-checks DATA_ROOT, which is baked at import time.
        paths.DATA_ROOT = cls.data_dir
        db.close_pool()
        projects.invalidate()

    @classmethod
    def restore_environment(cls):
        url, p_data, p_proj, s_data, s_proj, data_root = cls._saved
        cls.db.close_pool()
        if url is None:
            os.environ.pop("CAPTURE_DATABASE_URL", None)
        else:
            os.environ["CAPTURE_DATABASE_URL"] = url
        cls.projects.DATA_DIR, cls.projects.PROJECTS_DIR = p_data, p_proj
        cls.storage.DATA_DIR, cls.storage.PROJECTS_DIR = s_data, s_proj
        cls.paths.DATA_ROOT = data_root
        cls.projects.invalidate()

    def setUp(self):
        self.real_put = self.storage.put_chunk

        def slow_put(*args, **kwargs):
            time.sleep(WRITE_DELAY_S)
            return self.real_put(*args, **kwargs)

        self.storage.put_chunk = slow_put
        self.addCleanup(setattr, self.storage, "put_chunk", self.real_put)

    def body(self):
        return gzip.compress(
            json.dumps(
                {
                    "events": [{"type": 3, "data": {"source": 2}, "timestamp": 1}],
                    "window_id": "aaaa1111",
                }
            ).encode()
        )

    async def test_concurrent_ingests_overlap_instead_of_queueing(self):
        """Every write must be in flight at once.

        This used to time the batch and require it to beat 60% of the serialised
        cost, which asks the machine to be fast as well as the handler to be
        correct: on a loaded box the run came in at 0.30-0.35s against a 0.288s
        ceiling while still overlapping perfectly. A barrier asks the question
        directly instead. If put_chunk ran on the event loop the first call would
        hold it and the other seven would never arrive, so the barrier would
        break; its timeout only bounds how long a genuine failure takes to
        report, and never decides a passing run.
        """
        import concurrent.futures

        import httpx

        payload = self.body()
        barrier = threading.Barrier(CONCURRENCY)
        overlapped = []

        def barrier_put(*args, **kwargs):
            try:
                barrier.wait(timeout=60)
                overlapped.append(True)
            except threading.BrokenBarrierError:
                pass
            return self.real_put(*args, **kwargs)

        self.storage.put_chunk = barrier_put

        async def main():
            # Sized here rather than inherited: asyncio's default executor is
            # min(32, cpu_count + 4) threads, which is fewer than CONCURRENCY on
            # a small box and would break the barrier for want of workers.
            asyncio.get_running_loop().set_default_executor(
                concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY)
            )
            transport = httpx.ASGITransport(app=self.server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:

                async def one(n):
                    return await client.post(
                        f"/ingest?session_id={'c' * 24}{n:02d}&compression=gzip-js",
                        content=payload,
                        headers={"Content-Type": "application/octet-stream"},
                    )

                results = await asyncio.gather(*(one(n) for n in range(CONCURRENCY)))
                return [r.status_code for r in results]

        codes = await main()
        self.assertEqual(codes, [200] * CONCURRENCY)
        self.assertEqual(
            len(overlapped),
            CONCURRENCY,
            f"only {len(overlapped)} of {CONCURRENCY} writes were in flight together "
            "— the write is blocking the event loop",
        )

    async def test_every_chunk_still_lands(self):
        import httpx

        payload = self.body()

        async def main():
            transport = httpx.ASGITransport(app=self.server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                await asyncio.gather(
                    *(
                        client.post(
                            f"/ingest?session_id={'d' * 24}{n:02d}&compression=gzip-js",
                            content=payload,
                            headers={"Content-Type": "application/octet-stream"},
                        )
                        for n in range(CONCURRENCY)
                    )
                )

        await main()
        import psycopg

        with psycopg.connect(self.admin_url) as conn:
            stored = conn.execute(
                "SELECT count(*) FROM sessions WHERE session_id LIKE %s", ("d" * 24 + "%",)
            ).fetchone()[0]
        self.assertEqual(stored, CONCURRENCY)


if __name__ == "__main__":
    unittest.main()
