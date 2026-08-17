"""Shared fixture for the HTTP tests: a real uvicorn process on a free port,
backed by an ephemeral Postgres and a throwaway data dir.

Every test_api_*.py module drives the server through this, so the boot/teardown
sequence lives here once rather than per area.
"""

import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

REPO = Path(__file__).resolve().parent.parent.parent
UVICORN = REPO / ".venv" / "bin" / "uvicorn"

# capture.config reads the environment once, at import, and whichever test module
# imports capture.server first freezes that reading for the whole run. A few of
# these modules import the app in-process (parse_attribution, chat), so declare
# the same flag the server subprocess gets rather than leaving it to import
# order: without it the first in-process import caches an unconfigured-auth
# config and every later in-process request 503s.
os.environ.setdefault("REPLAY_AUTH_DISABLED", "1")

sys.path.insert(0, str(REPO))
from capture.analyze import store as analyze_store


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def request_headers(url):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.headers
    except urllib.error.HTTPError as err:
        return err.code, err.headers


class ServerCase(unittest.IsolatedAsyncioTestCase):
    """Boots a real server subprocess and talks to it over HTTP.

    Async-capable because the store these tests seed through is async now: a
    case that writes a cohort or an analysis directly (rather than via the HTTP
    API) needs to await it, which only happens if the case runs on a loop.
    """

    env: dict = {}

    default_project: bool = True

    @classmethod
    def prepare_data_dir(cls):
        """Seed the data dir before the server boots. Override to test bad state."""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-test-"))
        cls.prepare_data_dir()
        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        from capture import db as _db
        from capture import projects as _projects

        cls._db, cls._projects = _db, _projects
        cls._saved_db_url = os.environ.get("CAPTURE_DATABASE_URL")
        os.environ["CAPTURE_DATABASE_URL"] = cls.app_url
        _db.close_pool()
        _projects.invalidate()
        if cls.default_project:
            cls.project_id = pgfixture.seed_default_project(cls.admin_url)
        cls.run_env = {
            **os.environ,
            "CAPTURE_DATA_DIR": str(cls.data_dir),
            "CAPTURE_DATABASE_URL": cls.app_url,
            "CAPTURE_DATABASE_ADMIN_URL": cls.admin_url,
            "CAPTURE_PROJECT_CACHE_TTL_S": "0",
            "REPLAY_AUTH_DISABLED": "1",
            # print() is block-buffered when stdout is a file, so without this
            # the captured log stays empty until the process exits and any test
            # asserting on it passes vacuously.
            "PYTHONUNBUFFERED": "1",
            **cls.env,
        }
        cls.start_server()

    @classmethod
    def start_server(cls):
        """Boot uvicorn on a port nothing else is using.

        free_port() only reports a port that was free a moment ago, so between
        the probe and uvicorn's bind anything on the box — another copy of this
        suite, most of all — can take it. Two things go wrong then: uvicorn dies
        with EADDRINUSE, or worse, the /config probe is answered by whoever won
        the port and the class runs happily against a stranger's server. So a
        dead process means a new port and another go, and a probe only counts
        once ours is confirmed still alive.
        """
        for attempt in range(5):
            cls.port = free_port()
            cls.base = f"http://127.0.0.1:{cls.port}"
            outcome = cls._boot_once()
            if outcome == "ok":
                return
            if outcome == "timeout":
                break
            print(
                f"server exited during startup on port {cls.port} "
                f"({cls.proc.returncode}); retrying on a new port "
                f"(attempt {attempt + 1})"
            )
        cls.tearDownClass()
        raise RuntimeError("server did not start")

    @classmethod
    def _boot_once(cls) -> str:
        # Captured rather than discarded so tests can assert on what the server
        # reported — cohort_transition logs undeclared status moves instead of
        # raising, and out here that log is the only way to see one.
        cls.log_path = cls.data_dir / "server.log"
        cls.log_file = open(cls.log_path, "ab")
        cls.proc = subprocess.Popen(
            [str(UVICORN), "capture.server:app", "--port", str(cls.port), "--log-level", "error"],
            cwd=REPO,
            env=cls.run_env,
            stdout=cls.log_file,
            stderr=subprocess.STDOUT,
        )
        # Generous: each class now also provisions a database, and this can run
        # straight after a test that saturates the box.
        for _ in range(300):
            if cls.proc.poll() is not None:
                cls.log_file.close()
                return "died"
            try:
                # poll() again after the answer: a 200 from a port our own
                # process never managed to bind came from someone else.
                if request(f"{cls.base}/config")[0] == 200 and cls.proc.poll() is None:
                    return "ok"
            except Exception:
                pass
            time.sleep(0.1)
        cls.log_file.close()
        return "timeout"

    @classmethod
    def tearDownClass(cls):
        cls._db.close_pool()
        if cls._saved_db_url is None:
            os.environ.pop("CAPTURE_DATABASE_URL", None)
        else:
            os.environ["CAPTURE_DATABASE_URL"] = cls._saved_db_url
        cls._projects.invalidate()
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=10)
            raise
        finally:
            try:
                cls.log_file.close()
            except Exception:
                pass
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def server_log(self) -> str:
        """What the server process printed so far."""
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def cohort_store(self):
        """A fresh connection to the cohort tables the server process writes.

        Tests assert on durable state in Postgres. The server owns its own pool
        in another process; this is the test process's own handle on the same
        database.
        """
        return analyze_store.connect()

    async def stored_cohort(self, cohort_id, project_id=None):
        """The cohort as persisted, or None. Mirrors storage.read_cohort."""
        pid = project_id if project_id is not None else self.project_id
        return await analyze_store.read_cohort(self.cohort_store(), pid, cohort_id)

    async def store_cohort(self, cohort, project_id=None):
        """Overwrite persisted cohort state, to stage a mid-run condition."""
        pid = project_id if project_id is not None else self.project_id
        return await analyze_store.write_cohort(self.cohort_store(), pid, cohort)

    async def stored_cuts(self, cohort_id, project_id=None):
        pid = project_id if project_id is not None else self.project_id
        return await analyze_store.read_cuts(self.cohort_store(), pid, cohort_id)

    def ingest(self, session_id, payload=None, gzipped=True, token=None, raw=None, headers=None):
        body = (
            raw
            if raw is not None
            else json.dumps(
                payload if payload is not None else {"events": [{"type": 4, "data": {}}]}
            ).encode()
        )
        url = f"{self.base}/ingest?session_id={session_id}"
        if gzipped:
            body = gzip.compress(body)
            url += "&compression=gzip-js"
        if token:
            url += f"&token={token}"
        return request(
            url,
            method="POST",
            data=body,
            headers={"Content-Type": "application/octet-stream", **(headers or {})},
        )[0]

    def order(self, session_id, payload):
        return request(
            f"{self.base}/order?session_id={session_id}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )[0]

    def listed(self, session_id):
        sessions = json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
        return next(s for s in sessions if s["session_id"] == session_id)
