
import gzip
import json
import os
import shutil
import socket
import sqlite3
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

REPO = Path(__file__).resolve().parent.parent
UVICORN = REPO / ".venv" / "bin" / "uvicorn"

sys.path.insert(0, str(REPO))
from capture import analyze
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

class ServerCase(unittest.TestCase):
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
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
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
                raise RuntimeError(f"server exited during startup ({cls.proc.returncode})")
            try:
                if request(f"{cls.base}/config")[0] == 200:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        cls.tearDownClass()
        raise RuntimeError("server did not start")

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

        Tests assert on durable state, which used to mean reading
        cohorts/<id>.json and then index.db. The server owns its own pool in
        another process; this is the test process's own handle on the same
        database.
        """
        return analyze_store.connect()

    def stored_cohort(self, cohort_id, pool_key=""):
        """The cohort as persisted, or None. Mirrors analyze.read_cohort."""
        return analyze_store.read_cohort(self.cohort_store(), pool_key, cohort_id)

    def store_cohort(self, cohort, pool_key=""):
        """Overwrite persisted cohort state, to stage a mid-run condition."""
        return analyze_store.write_cohort(self.cohort_store(), pool_key, cohort)

    def stored_cuts(self, cohort_id, pool_key=""):
        return analyze_store.read_cuts(self.cohort_store(), pool_key, cohort_id)

    def ingest(self, session_id, payload=None, gzipped=True, token=None, raw=None, headers=None):
        body = raw if raw is not None else json.dumps(
            payload if payload is not None else {"events": [{"type": 4, "data": {}}]}
        ).encode()
        url = f"{self.base}/ingest?session_id={session_id}"
        if gzipped:
            body = gzip.compress(body)
            url += "&compression=gzip-js"
        if token:
            url += f"&token={token}"
        return request(url, method="POST", data=body,
                       headers={"Content-Type": "application/octet-stream", **(headers or {})})[0]

class TestIngestValidation(ServerCase):
    def test_accepts_a_valid_batch(self):
        self.assertEqual(self.ingest("s" * 32), 200)

    def test_rejects_bad_session_id(self):
        self.assertEqual(self.ingest("../etc/passwd"), 400)

    def test_non_object_bodies_are_400_not_500(self):
        for body in (b"[]", b'"hi"', b"7", b"null"):
            with self.subTest(body=body):
                self.assertEqual(self.ingest("t" * 32, raw=body), 400)

    def test_object_without_events_is_rejected(self):
        self.assertEqual(self.ingest("u" * 32, payload={}), 400)

    def test_undecompressable_body_is_rejected(self):
        self.assertEqual(self.ingest("v" * 32, raw=b"not gzip", gzipped=False), 400)

class TestGzipBomb(ServerCase):
    env = {"MAX_DECOMPRESSED_BYTES": str(1024 * 1024)}

    def test_small_payload_still_accepted(self):
        self.assertEqual(self.ingest("w" * 32), 200)

    def test_bomb_is_refused_and_server_survives(self):
        bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
        self.assertLess(len(bomb), 100 * 1024)
        self.assertEqual(self.ingest("x" * 32, raw=bomb, gzipped=False), 400)
        self.assertEqual(request(f"{self.base}/config")[0], 200)

class TestQuota(ServerCase):
    env = {"MAX_PROJECT_BYTES": "1"}

    def test_over_quota_returns_429(self):
        self.ingest("y" * 32)
        self.assertEqual(self.ingest("z" * 32), 429)

class TestDiskFloor(ServerCase):
    env = {"MIN_FREE_DISK_BYTES": str(10**18)}

    def test_low_disk_returns_507(self):
        self.assertEqual(self.ingest("a" * 32), 507)

class TestLimitsDisabled(ServerCase):
    env = {"MAX_PROJECT_BYTES": "0", "MIN_FREE_DISK_BYTES": "0"}

    def test_zero_disables_the_checks(self):
        self.assertEqual(self.ingest("b" * 32), 200)

class TestReplayAuthFailsClosed(ServerCase):
    env = {"REPLAY_AUTH_DISABLED": "", "REPLAY_PASSWORD": ""}

    def test_read_side_refuses_when_no_password_is_set(self):
        for path in ("/replay.html", "/api/sessions", "/api/projects"):
            with self.subTest(path=path):
                self.assertEqual(request(self.base + path)[0], 503)

    def test_normalised_paths_cannot_slip_past(self):
        for path in ("//replay.html", "/API/sessions"):
            with self.subTest(path=path):
                self.assertEqual(request(self.base + path)[0], 503)

    def test_write_side_stays_open(self):
        self.assertEqual(request(f"{self.base}/config")[0], 200)
        self.assertEqual(request(f"{self.base}/capture.js")[0], 200)

class TestReplayAuthWithPassword(ServerCase):
    env = {"REPLAY_AUTH_DISABLED": "", "REPLAY_PASSWORD": "s3cret"}

    def test_missing_and_wrong_credentials_are_rejected(self):
        import base64

        self.assertEqual(request(f"{self.base}/api/sessions")[0], 401)
        bad = base64.b64encode(b"any:nope").decode()
        self.assertEqual(
            request(f"{self.base}/api/sessions", headers={"Authorization": f"Basic {bad}"})[0], 401
        )

    def test_correct_password_is_accepted(self):
        import base64

        good = base64.b64encode(b"any:s3cret").decode()
        self.assertEqual(
            request(f"{self.base}/api/sessions", headers={"Authorization": f"Basic {good}"})[0], 200
        )

    def test_ingest_needs_no_password(self):
        self.assertEqual(self.ingest("c" * 32), 200)

class TestDelete(ServerCase):
    def test_delete_removes_only_the_named_session(self):
        keep, drop = "k" * 32, "d" * 32
        self.assertEqual(self.ingest(keep), 200)
        self.assertEqual(self.ingest(drop), 200)

        status, body = request(f"{self.base}/api/sessions/{drop}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["session_id"], drop)

        listed = [s["session_id"] for s in json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]]
        self.assertIn(keep, listed)
        self.assertNotIn(drop, listed)

    def test_deleting_twice_is_404(self):
        session = "e" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertEqual(request(f"{self.base}/api/sessions/{session}", method="DELETE")[0], 200)
        self.assertEqual(request(f"{self.base}/api/sessions/{session}", method="DELETE")[0], 404)

    def test_unknown_project_is_404(self):
        self.assertEqual(
            request(f"{self.base}/api/sessions/{'f' * 32}?project=cap_nope", method="DELETE")[0], 404
        )

class TestOrderEvents(ServerCase):
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

    def test_marks_a_recorded_session_as_purchased(self):
        session = "o" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertEqual(self.order(session, {"event": "checkout_started"}), 200)
        self.assertEqual(
            self.order(
                session,
                {"event": "checkout_completed", "order_id": "1001", "total": 59.5, "currency": "USD"},
            ),
            200,
        )
        order = self.listed(session)["order"]
        self.assertEqual(order["order_id"], "1001")
        self.assertEqual(order["total"], 59.5)
        self.assertEqual(order["currency"], "USD")
        self.assertIn("purchased_at", order)
        self.assertIn("checkout_started_at", order)

    def test_marker_never_appears_as_a_chunk(self):
        session = "q" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertEqual(self.order(session, {"event": "checkout_completed"}), 200)
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertEqual(len(manifest["chunks"]), 1)
        self.assertNotIn("order.json", [c["name"] for c in manifest["chunks"]])
        self.assertEqual(self.listed(session)["chunk_count"], 1)

    def test_unrecorded_session_is_404(self):
        self.assertEqual(self.order("r" * 32, {"event": "checkout_completed"}), 404)
class TestVisitorId(ServerCase):
    EVENTS = [{"type": 4, "data": {}}]

    def listed(self, session_id):
        sessions = json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
        return next(s for s in sessions if s["session_id"] == session_id)

    def test_visitor_id_is_stored_and_listed(self):
        session = "v" * 32
        status = self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-abc123"})
        self.assertEqual(status, 200)
        self.assertEqual(self.listed(session)["visitor_id"], "vis-abc123")

    def test_first_visitor_id_wins(self):
        session = "w" * 32
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-first"})
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-second"})
        self.assertEqual(self.listed(session)["visitor_id"], "vis-first")

    def test_invalid_visitor_ids_are_ignored(self):
        for i, bad in enumerate(["../../etc/passwd", "x" * 65, 42, {"nested": True}]):
            session = f"inv{i}" + "x" * 28
            status = self.ingest(session, {"events": self.EVENTS, "visitor_id": bad})
            self.assertEqual(status, 200, f"batch with visitor_id {bad!r} must still ingest")
            self.assertNotIn("visitor_id", self.listed(session))

    def test_marker_never_appears_as_a_chunk(self):
        session = "y" * 32
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-abc123"})
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertEqual(len(manifest["chunks"]), 1)
        self.assertNotIn("visitor.json", [c["name"] for c in manifest["chunks"]])
        self.assertEqual(self.listed(session)["chunk_count"], 1)

    def test_batches_without_visitor_id_still_ingest(self):
        session = "z" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertNotIn("visitor_id", self.listed(session))

    def test_entry_url_comes_from_the_first_batch(self):
        session = "u" * 32
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-u", "url": "https://brand.example/landing-a?x=1"})
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-u", "url": "https://brand.example/checkout"})
        listed = self.listed(session)
        self.assertEqual(listed["url"], "https://brand.example/landing-a?x=1")
        self.assertEqual(listed["visitor_id"], "vis-u")

    def test_non_string_url_is_ignored(self):
        session = "t" * 32
        self.assertEqual(self.ingest(session, {"events": self.EVENTS, "url": 42}), 200)
        self.assertNotIn("url", self.listed(session))

    def test_oversized_url_is_truncated(self):
        session = "s2" + "x" * 30
        self.ingest(session, {"events": self.EVENTS, "url": "https://brand.example/" + "a" * 3000})
        self.assertEqual(len(self.listed(session)["url"]), 2048)

class TestInteractions(ServerCase):
    def click(self, payload):
        return {"type": 5, "data": {"tag": "capture-click", "payload": payload}}

    def listed(self, session_id):
        sessions = json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
        return next(s for s in sessions if s["session_id"] == session_id)

    def test_clicks_are_counted_across_batches(self):
        session = "click" + "x" * 27
        self.ingest(session, {"events": [
            self.click({"selector": "button#buy", "text": "Buy now"}),
            self.click({"selector": "button#buy", "rage": True}),
            {"type": 3, "data": {"source": 2}},
        ]})
        self.ingest(session, {"events": [self.click({"selector": "a.nav"})]})
        self.assertEqual(self.listed(session)["interactions"], {"clicks": 3, "rage": 1})

    def test_sidecar_never_appears_as_a_chunk(self):
        session = "click2" + "x" * 26
        self.ingest(session, {"events": [self.click({"selector": "button#buy"})]})
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertNotIn("interactions.json", [c["name"] for c in manifest["chunks"]])

    def test_sessions_without_clicks_have_no_interactions(self):
        session = "click3" + "x" * 26
        self.ingest(session, {"events": [{"type": 3, "data": {"source": 2}}]})
        self.assertNotIn("interactions", self.listed(session))

    def test_malformed_click_events_never_break_ingest(self):
        session = "click4" + "x" * 26
        status = self.ingest(session, {"events": [
            {"type": 5, "data": {"tag": "capture-click", "payload": "junk"}},
            {"type": 5, "data": "junk"},
            {"type": 5},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(self.listed(session)["interactions"], {"clicks": 1, "rage": 0})

class TestDeviceIndex(ServerCase):
    def listed(self, session_id):
        sessions = json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
        return next(s for s in sessions if s["session_id"] == session_id)

    def test_first_meta_event_sets_device(self):
        session = "dev1" + "x" * 28
        self.ingest(session, {"events": [
            {"type": 4, "data": {"href": "https://brand.example/", "width": 390, "height": 844}},
            {"type": 4, "data": {"href": "https://brand.example/b", "width": 390, "height": 677}},
        ]})
        self.assertEqual(self.listed(session)["device"], {"width": 390, "height": 844})

    def test_device_survives_later_batches_and_never_lists_as_chunk(self):
        session = "dev2" + "x" * 28
        self.ingest(session, {"events": [{"type": 4, "data": {"width": 1496, "height": 900}}]})
        self.ingest(session, {"events": [{"type": 4, "data": {"width": 390, "height": 844}}]})
        self.assertEqual(self.listed(session)["device"]["width"], 1496)
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertNotIn("device.json", [c["name"] for c in manifest["chunks"]])

    def test_malformed_meta_events_never_break_ingest(self):
        session = "dev3" + "x" * 28
        status = self.ingest(session, {"events": [
            {"type": 4, "data": "junk"},
            {"type": 4, "data": {"width": "wide", "height": 10}},
            {"type": 4},
            {"type": 3, "data": {"source": 2}},
        ]})
        self.assertEqual(status, 200)
        self.assertNotIn("device", self.listed(session))

class TestHealthz(ServerCase):
    env = {"REPLAY_AUTH_DISABLED": "", "REPLAY_PASSWORD": "s3cret"}

    def test_reports_ok_when_everything_is_fine(self):
        status, body = request(f"{self.base}/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["checks"]["storage_writable"])
        self.assertTrue(payload["checks"]["bundles_present"])
        self.assertTrue(payload["checks"]["replay_auth_enabled"])

    def test_needs_no_password(self):
        self.assertEqual(request(f"{self.base}/healthz")[0], 200)

    def test_leaks_no_project_detail(self):
        body = request(f"{self.base}/healthz")[1].decode()
        self.assertNotIn("cap_", body)
        self.assertNotIn("s3cret", body)

class TestHealthzDegraded(ServerCase):
    env = {"MIN_FREE_DISK_BYTES": str(10**18)}

    def test_503_when_disk_is_below_the_floor(self):
        status, body = request(f"{self.base}/healthz")
        self.assertEqual(status, 503)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["checks"]["disk_above_floor"])

class TestHealthzUnwritableStorage(ServerCase):
    def test_503_when_storage_cannot_be_written(self):
        self.assertEqual(request(f"{self.base}/healthz")[0], 200)
        mode = self.data_dir.stat().st_mode
        os.chmod(self.data_dir, 0o500)
        try:
            status, body = request(f"{self.base}/healthz")
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(body)["checks"]["storage_writable"])

            self.assertEqual(request(f"{self.base}/config")[0], 200)
        finally:
            os.chmod(self.data_dir, mode)

def request_headers(url):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.headers
    except urllib.error.HTTPError as err:
        return err.code, err.headers

class TestIntegrationSnippetMatchesDocs(unittest.TestCase):
    def test_cli_snippet_is_the_documented_snippet(self):
        sys.path.insert(0, str(REPO))
        from capture import server

        doc = (REPO / "docs" / "integration.md").read_text()
        documented = doc.split("```html\n", 1)[1].split("```", 1)[0].strip()
        generated = server.integration_snippet(
            "https://capture.example.com", "cap_YOURTOKEN", indent=""
        ).strip()
        self.assertEqual(documented, generated)

class TestVisitorAssetCaching(ServerCase):
    def test_visitor_assets_are_cacheable_and_stale_servable(self):
        for path in ("/capture.js", "/recorder.bundle.js"):
            with self.subTest(path=path):
                status, headers = request_headers(self.base + path)
                self.assertEqual(status, 200)
                cache_control = headers.get("Cache-Control", "")
                self.assertIn("public", cache_control)
                self.assertIn("max-age=3600", cache_control)
                self.assertIn("stale-if-error=86400", cache_control)

    def test_everything_else_still_revalidates(self):
        for path in ("/replay.html", "/replay.bundle.js", "/config"):
            with self.subTest(path=path):
                self.assertEqual(request_headers(self.base + path)[1].get("Cache-Control"), "no-cache")

class TestProjectTokens(ServerCase):
    # No default project, so a tokenless batch has nowhere to land.
    default_project = False
    token = "cap_testtokentesttokentest"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pgfixture.seed_project(cls.admin_url, "Test", cls.token)

    def test_known_token_is_accepted(self):
        self.assertEqual(self.ingest("g" * 32, token=self.token), 200)

    def test_unknown_token_is_rejected(self):
        self.assertEqual(self.ingest("h" * 32, token="cap_wrong"), 401)

    def test_missing_token_is_rejected_once_projects_exist(self):
        self.assertEqual(self.ingest("i" * 32), 401)

    def test_order_endpoint_requires_a_known_token(self):
        session = "t" * 32
        self.assertEqual(self.ingest(session, token=self.token), 200)
        body = json.dumps({"event": "checkout_completed"}).encode()
        self.assertEqual(
            request(f"{self.base}/order?session_id={session}&token=cap_wrong", "POST", body)[0], 401
        )
        self.assertEqual(
            request(f"{self.base}/order?session_id={session}&token={self.token}", "POST", body)[0], 200
        )

    def test_listing_an_unconfigured_default_pool_is_a_404(self):
        """With no default project there is no pool to fall back to, which is
        what used to make a corrupt config leak every tenant into one bucket."""
        self.assertEqual(request(f"{self.base}/api/sessions")[0], 404)


class TestProjectIsolation(ServerCase):
    """A default project and a tenant project side by side."""

    token = "cap_isolationtokentesttoken"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pgfixture.seed_project(cls.admin_url, "Tenant", cls.token)

    def test_a_tenant_session_never_appears_in_the_default_pool(self):
        self.assertEqual(self.ingest("j" * 32, token=self.token), 200)
        self.assertEqual(self.ingest("k" * 32), 200)
        scoped = json.loads(request(f"{self.base}/api/sessions?project={self.token}")[1])
        default = json.loads(request(f"{self.base}/api/sessions")[1])
        self.assertEqual([s["session_id"] for s in scoped["sessions"]], ["j" * 32])
        self.assertEqual([s["session_id"] for s in default["sessions"]], ["k" * 32])

class TestDomainAllowlist(ServerCase):
    default_project = False
    token = "cap_domainstesttokentest"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pgfixture.seed_project(cls.admin_url, "Test", cls.token, domains=["brand.com"])

    def send(self, session_id, **headers):
        return self.ingest(session_id, token=self.token, headers=headers)

    def test_root_domain_is_accepted(self):
        self.assertEqual(self.send("a" * 32, Origin="https://brand.com"), 200)

    def test_subdomains_are_accepted(self):
        self.assertEqual(self.send("b" * 32, Origin="https://www.brand.com"), 200)

    def test_other_domains_are_rejected(self):
        self.assertEqual(self.send("c" * 32, Origin="https://evil.example"), 403)

    def test_lookalike_suffix_is_rejected(self):
        self.assertEqual(self.send("d" * 32, Origin="https://evilbrand.com"), 403)

    def test_missing_origin_is_rejected(self):
        self.assertEqual(self.send("e" * 32), 403)

    def test_referer_is_the_fallback(self):
        self.assertEqual(self.send("f" * 32, Referer="https://shop.brand.com/lp?x=1"), 200)

class TestProjectCli(unittest.TestCase):

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="capture-cli-"))
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.admin_url, self.app_url = pgfixture.fresh_database()

    def projects_in_db(self) -> dict:
        import psycopg

        with psycopg.connect(self.admin_url) as conn:
            rows = conn.execute(
                "SELECT p.storage_prefix, p.domains FROM projects p"
            ).fetchall()
        return {prefix: domains for prefix, domains in rows}

    def live_tokens(self) -> list:
        import psycopg

        with psycopg.connect(self.admin_url) as conn:
            rows = conn.execute(
                "SELECT token FROM project_tokens WHERE revoked_at IS NULL"
            ).fetchall()
        return [r[0] for r in rows]

    def run_cli(self, *args, env=None):
        result = subprocess.run(
            [str(REPO / ".venv" / "bin" / "python"), "-m", "capture.server", *args],
            cwd=REPO,
            env={
                **os.environ,
                "CAPTURE_DATA_DIR": str(self.data_dir),
                "CAPTURE_DATABASE_URL": self.app_url,
                "CAPTURE_DATABASE_ADMIN_URL": self.admin_url,
                "CAPTURE_PROJECT_CACHE_TTL_S": "0",
                **(env or {}),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout + result.stderr

    def test_startup_prints_no_deprecation_warning(self):

        out = self.run_cli("list-projects")
        self.assertNotIn("DeprecationWarning", out)
        self.assertNotIn("on_event is deprecated", out)

    def test_add_project_uses_the_configured_public_url(self):
        out = self.run_cli("add-project", "Acme", env={"CAPTURE_PUBLIC_URL": "https://cdn.example.net/"})
        self.assertIn("var host = 'https://cdn.example.net'", out)
        self.assertNotIn("capture.example.com", out)

    def test_public_url_file_is_used_when_the_env_var_is_absent(self):
        (self.data_dir / "public_url").write_text("https://from-file.example\n")
        out = self.run_cli("add-project", "Acme")
        self.assertIn("var host = 'https://from-file.example'", out)

    def test_placeholder_host_is_called_out(self):
        out = self.run_cli("add-project", "Acme")
        self.assertIn("capture.example.com", out)
        self.assertIn("placeholder host", out)

    def test_set_domains_round_trip(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        out = self.run_cli("set-domains", token, "https://Brand.com/path", "shop.example")
        self.assertIn("brand.com, shop.example", out)
        self.assertEqual(self.projects_in_db()[token], ["brand.com", "shop.example"])

        out = self.run_cli("set-domains", token)
        self.assertIn("any domain", out)
        self.assertEqual(self.projects_in_db()[token], [])

    def test_list_and_remove_round_trip(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        self.assertIn(token, self.run_cli("list-projects"))

        sessions = self.data_dir / "projects" / token / "sessions" / "s1"
        sessions.mkdir(parents=True)
        (sessions / "1-abcdef.json").write_text('{"events":[1]}')

        out = self.run_cli("remove-project", token)
        self.assertIn("revoked", out)
        self.assertTrue(sessions.exists(), "recordings must survive a plain revoke")
        # The project stays listed with its token revoked. Dropping the row
        # outright would orphan recordings that are still on disk.
        self.assertIn("no live token", self.run_cli("list-projects"))
        self.assertEqual(self.live_tokens(), [])

    def test_purge_deletes_the_recordings(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        sessions = self.data_dir / "projects" / token / "sessions" / "s1"
        sessions.mkdir(parents=True)
        (sessions / "1-abcdef.json").write_text('{"events":[1]}')

        self.run_cli("remove-project", token, "--purge")
        self.assertFalse((self.data_dir / "projects" / token).exists())

class TestSessionPaging(ServerCase):
    def list(self, query=""):
        status, body = request(f"{self.base}/api/sessions{query}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_pages_walk_every_session_exactly_once(self):
        ids = [f"pg{i}" + "x" * 28 for i in range(5)]
        for sid in ids:
            self.assertEqual(self.ingest(sid), 200)

        body = self.list()
        self.assertEqual(body["total"], 5)
        self.assertIsNone(body["cursor"])
        self.assertEqual(len(body["sessions"]), 5)

        body = self.list("?limit=2")
        self.assertEqual(body["total"], 5)
        seen = [s["session_id"] for s in body["sessions"]]
        self.assertEqual(len(seen), 2)
        while body["cursor"]:
            body = self.list(f"?limit=2&cursor={body['cursor']}")
            seen += [s["session_id"] for s in body["sessions"]]
        self.assertEqual(sorted(seen), sorted(ids))
        last_seen = [s["last_seen"] for s in self.list()["sessions"]]
        self.assertEqual(last_seen, sorted(last_seen, reverse=True))

    def test_single_session_endpoint_serves_deep_links(self):
        sid = "deep" + "x" * 28
        self.assertEqual(self.ingest(sid), 200)
        status, body = request(f"{self.base}/api/sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["session_id"], sid)
        self.assertEqual(request(f"{self.base}/api/sessions/{'gone' + 'x' * 28}")[0], 404)

class TestSessionFilters(ServerCase):
    EVENTS = [{"type": 4, "data": {}}]

    def order(self, session_id, payload):
        return request(
            f"{self.base}/order?session_id={session_id}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )[0]

    def listed_ids(self, query):
        status, body = request(f"{self.base}/api/sessions{query}")
        self.assertEqual(status, 200)
        return {s["session_id"] for s in json.loads(body)["sessions"]}

    def test_filters_and_search_run_serverside(self):
        plain = "plain" + "x" * 27
        started = "start" + "x" * 27
        bought = "buy" + "x" * 29
        ragey = "rage" + "x" * 28
        self.ingest(plain, {"events": self.EVENTS, "url": "https://brand.example/landing-a?utm=news"})
        self.ingest(started)
        self.assertEqual(self.order(started, {"event": "checkout_started"}), 200)
        self.ingest(bought)
        self.assertEqual(self.order(bought, {"event": "checkout_completed"}), 200)
        self.ingest(ragey, {"events": self.EVENTS + [
            {"type": 5, "data": {"tag": "capture-click", "payload": {"rage": True}}}
        ]})

        self.assertEqual(self.listed_ids("?filter=checkout"), {started, bought})
        self.assertEqual(self.listed_ids("?filter=purchased"), {bought})
        self.assertEqual(self.listed_ids("?filter=rage"), {ragey})
        self.assertEqual(self.listed_ids("?q=landing-a"), {plain})
        self.assertEqual(self.listed_ids("?q=utm%3Dnews"), {plain})
        self.assertEqual(self.listed_ids("?q=" + started[:12]), {started})
        self.assertEqual(self.listed_ids("?q=nothing-matches"), set())

class TestIndexRebuild(ServerCase):
    def test_index_rebuilds_from_disk_on_startup(self):
        ids = [f"reb{i}" + "x" * 28 for i in range(3)]
        for sid in ids:
            self.assertEqual(self.ingest(sid), 200)

        self.proc.terminate()
        self.proc.wait(timeout=10)
        for name in ("index.db", "index.db-wal", "index.db-shm"):
            (self.data_dir / name).unlink(missing_ok=True)
        type(self).start_server()

        deadline = time.time() + 10
        listed = set()
        while time.time() < deadline:
            listed = {
                s["session_id"]
                for s in json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
            }
            if listed == set(ids):
                break
            time.sleep(0.2)
        self.assertEqual(listed, set(ids))

class TestAnalysisStatus(ServerCase):
    # Isolate from a developer machine that has GEMINI_API_KEY in the environment.
    env = {"GEMINI_API_KEY": ""}

    def test_get_returns_cached_analysis(self):
        sid = "a" * 32
        self.assertEqual(self.ingest(sid), 200)
        session = self.data_dir / "sessions" / sid
        analyze.write_analysis(
            session, {"text": "hello", "model": "test", "frame_count": 1, "warnings": []}
        )
        status, body = request(f"{self.base}/api/sessions/{sid}/analysis")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["text"], "hello")
        self.assertEqual(data["status"], "done")

    def test_get_returns_running_job(self):
        sid = "b" * 32
        self.assertEqual(self.ingest(sid), 200)
        session = self.data_dir / "sessions" / sid
        analyze.write_job(session, "running")
        status, body = request(f"{self.base}/api/sessions/{sid}/analysis")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "running")

    def test_post_without_key_is_unavailable(self):
        sid = "c" * 32
        self.assertEqual(self.ingest(sid), 200)
        status, body = request(
            f"{self.base}/api/sessions/{sid}/analysis?force=1", method="POST", data=b""
        )
        self.assertEqual(status, 503)
        self.assertIn("not configured", json.loads(body)["error"])


class TestCohort(ServerCase):
    # Relaxed sampling defaults for fixtures; Batch off so cached write-ups settle sync.
    env = {
        "GEMINI_API_KEY": "test-key-not-used-when-cached",
        "ANALYZE_BATCH": "0",
    }

    def order(self, session_id, payload):
        return request(
            f"{self.base}/order?session_id={session_id}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )[0]

    def seed_analysis(self, sid):
        analyze.write_analysis(
            self.data_dir / "sessions" / sid,
            {
                "text": f"write-up for {sid[:8]}",
                "model": "test",
                "frame_count": 1,
                "warnings": [],
            },
        )

    def create_cohort_url(self, purchased=1, all_n=0, **extra):
        """POST URL that disables production ashwamag/≥5s filters for test fixtures."""
        params = {
            "purchased": purchased,
            "all": all_n,
            "min_duration_ms": 0,
            "url_contains": "",
        }
        params.update(extra)
        qs = urllib.parse.urlencode(params)
        return f"{self.base}/api/cohorts?{qs}"

    def analyze_cohort_url(self, cohort_id, **extra):
        qs = urllib.parse.urlencode(extra) if extra else ""
        return f"{self.base}/api/cohorts/{cohort_id}/analyze" + (f"?{qs}" if qs else "")

    def create_and_analyze(self, purchased=1, all_n=0, **extra):
        status, body = request(
            self.create_cohort_url(purchased=purchased, all_n=all_n, **extra),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        # Idle until Start; already-cached write-ups may enrich straight to done.
        self.assertIn(cohort["status"], ("idle", "done"))
        self.assertNotEqual(cohort["status"], "running")
        self.assertTrue(cohort.get("selection"))
        status, body = request(
            self.analyze_cohort_url(cohort["id"]), method="POST", data=b""
        )
        self.assertEqual(status, 202, body)
        return json.loads(body)

    def cohorts(self):
        status, body = request(f"{self.base}/api/cohorts")
        self.assertEqual(status, 200)
        return json.loads(body)["cohorts"]

    def wait_done(self, cohort_id, expect_done=None):
        deadline = time.time() + 10
        cohort = None
        while time.time() < deadline:
            status, body = request(f"{self.base}/api/cohorts/{cohort_id}")
            self.assertEqual(status, 200)
            cohort = json.loads(body)
            if cohort["status"] == "done" and (
                expect_done is None or cohort["progress"]["done"] == expect_done
            ):
                return cohort
            time.sleep(0.1)
        return cohort

    def test_get_without_cohort_is_404(self):
        status, body = request(f"{self.base}/api/cohorts/cohort-does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("no such cohort", json.loads(body)["error"])

    def test_list_reports_every_cohort_with_its_progress(self):
        sid = "listed" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        listed = {c["id"]: c for c in self.cohorts()}
        self.assertIn(cohort_id, listed)
        row = listed[cohort_id]
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["label"])
        # The listing's counts agree with the cohort's own detail view.
        detail = json.loads(request(f"{self.base}/api/cohorts/{cohort_id}")[1])
        self.assertEqual(row["progress"], detail["progress"])
        self.assertEqual(row["progress"]["done"], len(detail["sessions"]))

    def test_samples_purchased_and_all_and_reuses_cached_analysis(self):
        purchased = [f"buy{i}" + "x" * 28 for i in range(3)]
        plain = [f"all{i}" + "x" * 28 for i in range(4)]
        for sid in purchased:
            self.assertEqual(self.ingest(sid), 200)
            self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)
        for sid in plain:
            self.assertEqual(self.ingest(sid), 200)
            self.seed_analysis(sid)

        cohort = self.create_and_analyze(purchased=2, all_n=2)
        self.assertEqual(cohort["purchased_n"], 2)
        self.assertEqual(cohort["all_n"], 2)
        self.assertEqual(len(cohort["sessions"]), 4)
        self.assertIn("purchased", cohort.get("selection", ""))
        filters = {s["filter"] for s in cohort["sessions"]}
        self.assertEqual(filters, {"purchased", "all"})
        purchased_ids = {s["session_id"] for s in cohort["sessions"] if s["filter"] == "purchased"}
        all_ids = {s["session_id"] for s in cohort["sessions"] if s["filter"] == "all"}
        self.assertTrue(purchased_ids <= set(purchased))
        self.assertTrue(all_ids <= set(plain))
        self.assertFalse(purchased_ids & all_ids)

        cohort = self.wait_done(cohort["id"], expect_done=4)
        self.assertEqual(cohort["status"], "done")
        self.assertEqual(cohort["progress"]["done"], 4)
        self.assertEqual(cohort["progress"]["error"], 0)
        for entry in cohort["sessions"]:
            self.assertEqual(entry["status"], "done")
            self.assertEqual(entry["analysis"], "analysis.json")
            self.assertTrue(entry.get("text", "").startswith("write-up for"))
            self.assertIn("standout", entry)

        saved = self.stored_cohort(cohort["id"])
        self.assertEqual(saved["id"], cohort["id"])
        self.assertEqual(len(saved["sessions"]), 4)
        # Write-ups are response-only — not bloating the cohort file on disk.
        for entry in saved["sessions"]:
            self.assertNotIn("text", entry)

    def test_rejects_cohorts_over_500_and_supports_any_sample(self):
        for i in range(3):
            sid = f"any{i}" + "x" * 28
            self.assertEqual(self.ingest(sid), 200)
            if i == 0:
                self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)

        status, body = request(
            self.create_cohort_url(purchased=300, all_n=250),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 400, body)
        self.assertIn("500", json.loads(body)["error"])

        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode(
                {"any": 2, "min_duration_ms": 0, "url_contains": ""}
            ),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(cohort["any_n"], 2)
        self.assertEqual(len(cohort["sessions"]), 2)
        self.assertIn("matching", cohort.get("selection", ""))

    def test_pool_facets_and_slice_filtered_create(self):
        mobile = "facetm" + "x" * 26
        desktop = "facetd" + "x" * 26
        nonbuy = "facetn" + "x" * 26
        self.assertEqual(
            self.ingest(
                mobile,
                {
                    "events": [
                        {"type": 4, "data": {"width": 390, "height": 844}},
                    ],
                    "url": "https://brand.example/pages/ashwamag-01?utm_source=google",
                },
            ),
            200,
        )
        self.assertEqual(self.order(mobile, {"event": "checkout_completed"}), 200)
        self.assertEqual(
            self.ingest(
                desktop,
                {
                    "events": [
                        {"type": 4, "data": {"width": 1280, "height": 800}},
                    ],
                    "url": "https://brand.example/pages/ashwamag-01?utm_source=google",
                },
            ),
            200,
        )
        self.assertEqual(self.order(desktop, {"event": "checkout_completed"}), 200)
        self.assertEqual(
            self.ingest(
                nonbuy,
                {
                    "events": [
                        {"type": 4, "data": {"width": 390, "height": 844}},
                    ],
                    "url": "https://brand.example/pages/ashwamag-01?utm_source=google",
                },
            ),
            200,
        )
        self.seed_analysis(mobile)
        self.seed_analysis(desktop)
        self.seed_analysis(nonbuy)

        status, body = request(
            f"{self.base}/api/facets?"
            + urllib.parse.urlencode({"min_duration_ms": 0})
        )
        self.assertEqual(status, 200, body)
        facets = json.loads(body)
        self.assertGreaterEqual(facets["n"], 2)
        self.assertTrue(any(i["value"] == "mobile" for i in facets.get("device") or []))
        self.assertTrue(
            any(i["value"] == "/pages/ashwamag-01" for i in facets.get("page") or [])
        )

        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode(
                {
                    "any": 5,
                    "min_duration_ms": 0,
                    "url_contains": "",
                    "device": "mobile",
                    "utm_source": "google",
                    "page": "/pages/ashwamag-01",
                }
            ),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(
            set(s["session_id"] for s in cohort["sessions"]),
            {mobile, nonbuy},
        )
        self.assertEqual(cohort.get("slice", {}).get("device"), ["mobile"])

        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode(
                {
                    "a": 1,
                    "b": 1,
                    "min_duration_ms": 0,
                    "url_contains": "",
                    "outcome": "purchased",
                    "device": "mobile",
                    "page": "/pages/ashwamag-01",
                    "utm_source": "google",
                    "b_outcome": "all",
                    "b_device": "mobile",
                    "b_page": "/pages/ashwamag-01",
                    "b_utm_source": "google",
                }
            ),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(cohort["a_n"], 1)
        self.assertEqual(cohort["b_n"], 1)
        by_filter = {s["filter"]: s["session_id"] for s in cohort["sessions"]}
        self.assertEqual(by_filter.get("a"), mobile)
        self.assertEqual(by_filter.get("b"), nonbuy)

    def test_create_cohort_accepts_a_custom_label(self):
        sid = "named" + "x" * 27
        self.assertEqual(self.ingest(sid), 200)
        self.seed_analysis(sid)
        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode(
                {
                    "a": 1,
                    "min_duration_ms": 0,
                    "url_contains": "",
                    "label": "  Checkout check  ",
                }
            ),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(cohort["label"], "Checkout check")
        self.assertEqual(len(cohort["sessions"]), 1)

    def test_cohorts_accumulate_rather_than_overwrite(self):
        for i in range(2):
            sid = f"acc{i}" + "x" * 28
            self.assertEqual(self.ingest(sid), 200)
            self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)

        ids = []
        for _ in range(2):
            cohort = self.create_and_analyze(purchased=1, all_n=0)
            cohort_id = cohort["id"]
            ids.append(cohort_id)
            self.wait_done(cohort_id)
            # Ids embed a millisecond stamp; keep the two runs distinguishable.
            time.sleep(0.002)

        # Two runs, two distinct cohorts — the second did not overwrite the first.
        self.assertEqual(len(set(ids)), 2)
        listed = [c["id"] for c in self.cohorts()]
        for cohort_id in ids:
            self.assertIn(cohort_id, listed)
        # Newest first, so the later run sorts ahead of the earlier one.
        self.assertLess(listed.index(ids[1]), listed.index(ids[0]))
        # First cohort still exists with its membership.
        first = self.stored_cohort(ids[0])
        self.assertEqual(len(first["sessions"]), 1)

    def test_rename_sets_the_label_and_keeps_membership(self):
        sid = "rename" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        before = json.loads(request(f"{self.base}/api/cohorts/{cohort_id}")[1])

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}",
            method="PATCH",
            data=json.dumps({"label": "  Spring campaign check  "}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        renamed = json.loads(body)
        self.assertEqual(renamed["label"], "Spring campaign check")
        self.assertEqual(
            [s["session_id"] for s in renamed["sessions"]],
            [s["session_id"] for s in before["sessions"]],
        )
        self.assertEqual(self.cohorts()[0]["label"], "Spring campaign check")

    def test_rename_rejects_an_empty_label(self):
        sid = "rename2" + "x" * 25
        cohort_id = self._cohort_of_one(sid)
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}",
            method="PATCH",
            data=json.dumps({"label": "   "}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("empty", json.loads(body)["error"])

    def test_the_cohort_lifecycle_makes_only_declared_transitions(self):
        """Drive create → start → pause → start over HTTP and check the server
        never reported a status move COHORT_TRANSITIONS does not declare.

        The server runs in a subprocess, so its stdout is the only channel —
        cohort_transition logs rather than raises so a live run with a paid
        batch in flight cannot be stranded by an unforeseen move.
        """
        sid = "lifecyc" + "x" * 25
        self.assertEqual(self.ingest(sid), 200)
        self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
        self.seed_analysis(sid)
        cohort = self.create_and_analyze(purchased=1, all_n=0)
        cohort_id = cohort["id"]
        self.wait_done(cohort_id)

        for path in ("pause", "analyze"):
            request(f"{self.base}/api/cohorts/{cohort_id}/{path}", method="POST", data=b"")
        request(f"{self.base}/api/cohorts/{cohort_id}")

        offenders = [
            line
            for line in self.server_log().splitlines()
            if "not a declared transition" in line
        ]
        self.assertEqual(offenders, [])

    def test_deleting_a_session_removes_it_from_its_cohorts(self):
        """Otherwise the membership row outlives the recording, and
        enrich_cohort_status keeps counting it as a completed analysis."""
        sid = "delsess" + "x" * 25
        cohort_id = self._cohort_of_one(sid)
        _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
        self.assertEqual(json.loads(body)["progress"]["done"], 1)

        status, _ = request(f"{self.base}/api/sessions/{sid}", method="DELETE")
        self.assertEqual(status, 200)

        _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
        cohort = json.loads(body)
        self.assertEqual([s["session_id"] for s in cohort["sessions"]], [])
        self.assertEqual(cohort["progress"]["total"], 0)
        self.assertEqual(cohort["progress"]["done"], 0)
        self.assertEqual(self.stored_cohort(cohort_id)["sessions"], [])

    def test_deleting_one_session_leaves_the_others_in_place(self):
        sids = ["multi1" + "x" * 26, "multi2" + "x" * 26]
        for sid in sids:
            self.assertEqual(self.ingest(sid), 200)
            self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)
        cohort = self.create_and_analyze(purchased=2, all_n=0)
        self.wait_done(cohort["id"])
        self.assertEqual(len(self.stored_cohort(cohort["id"])["sessions"]), 2)

        self.assertEqual(request(f"{self.base}/api/sessions/{sids[0]}", method="DELETE")[0], 200)

        _, body = request(f"{self.base}/api/cohorts/{cohort['id']}")
        live = json.loads(body)
        self.assertEqual([s["session_id"] for s in live["sessions"]], [sids[1]])
        self.assertEqual(live["progress"]["done"], 1)

    def test_delete_removes_cohort_and_keeps_session_writeups(self):
        sid = "delcoh" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        analyze_store.write_cut(
            self.cohort_store(), "", cohort_id, "some-cut", {"label": "x"}
        )
        session_dir = self.data_dir / "sessions" / sid
        self.assertIsNotNone(analyze.read_analysis(session_dir))

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}", method="DELETE"
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        self.assertNotIn(cohort_id, [c["id"] for c in self.cohorts()])
        self.assertIsNone(self.stored_cohort(cohort_id))
        self.assertEqual(self.stored_cuts(cohort_id), {})
        # Deleting a cohort must not take the write-ups of its members with it.
        self.assertIsNotNone(analyze.read_analysis(session_dir))

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}", method="DELETE"
        )
        self.assertEqual(status, 404, body)

    def test_legacy_cohort_file_is_migrated_on_read(self):
        sid = "legacy" + "x" * 26
        self.assertEqual(self.ingest(sid), 200)
        self.seed_analysis(sid)
        legacy = {
            "id": "cohort-1700000000000",
            "label": "Sample 1+0",
            "created_at": 1700000000000,
            "status": "done",
            "purchased_n": 1,
            "all_n": 0,
            "sessions": [{"session_id": sid, "filter": "purchased", "status": "done"}],
        }
        (self.data_dir / "cohort.json").write_text(json.dumps(legacy))

        self.assertIn("cohort-1700000000000", [c["id"] for c in self.cohorts()])
        migrated = self.stored_cohort("cohort-1700000000000")
        self.assertEqual(migrated["label"], "Sample 1+0")
        self.assertEqual([s["session_id"] for s in migrated["sessions"]], [sid])
        # Both legacy files are consumed, so the migration runs exactly once.
        self.assertFalse((self.data_dir / "cohort.json").exists())
        self.assertFalse(
            (self.data_dir / "cohorts" / "cohort-1700000000000.json").exists()
        )

    def _cohort_of_one(self, sid):
        """A settled one-session cohort, with cached analysis so it lands done."""
        self.assertEqual(self.ingest(sid), 200)
        self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
        self.seed_analysis(sid)
        cohort = self.create_and_analyze(purchased=1, all_n=0)
        cohort_id = cohort["id"]
        self.wait_done(cohort_id)
        return cohort_id

    def test_retry_clears_the_recorded_error_for_that_row(self):
        sid = "retry1" + "x" * 26
        cohort_id = self._cohort_of_one(sid)

        # Simulate a row that failed during the run: status is re-derived from
        # disk, so the cached analysis has to go along with the error marker.
        session = self.data_dir / "sessions" / sid
        analyze.clear_analysis(session)
        analyze.write_job(session, "error", error="page reconstruction timed out")
        saved = self.stored_cohort(cohort_id)
        saved["sessions"][0]["status"] = "error"
        saved["sessions"][0]["error"] = "page reconstruction timed out"
        saved["status"] = "error"
        self.store_cohort(saved)

        _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
        self.assertEqual(json.loads(body)["sessions"][0]["status"], "error")

        # Restore the cache so the retry settles without a real Gemini call.
        self.seed_analysis(sid)

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/sessions/{sid}/retry",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 202, body)

        cohort = self.wait_done(cohort_id)
        entry = cohort["sessions"][0]
        self.assertEqual(entry["status"], "done")
        self.assertNotIn("error", entry)

    def test_retry_rejects_a_session_outside_the_cohort(self):
        sid = "retry2" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        other = "nother" + "x" * 26
        self.assertEqual(self.ingest(other), 200)

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/sessions/{other}/retry",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 404)
        self.assertIn("not in this cohort", json.loads(body)["error"])

    def test_retry_without_a_cohort_is_404(self):
        sid = "retry3" + "x" * 26
        self.assertEqual(self.ingest(sid), 200)
        status, body = request(
            f"{self.base}/api/cohorts/cohort-nope/sessions/{sid}/retry",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 404)
        self.assertIn("no such cohort", json.loads(body)["error"])


class TestAttribution(ServerCase):
    EVENTS = [{"type": 4, "data": {}}]

    def listed(self, session_id):
        sessions = json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
        return next(s for s in sessions if s["session_id"] == session_id)

    def test_utm_and_fbclid_indexed_at_ingest(self):
        sid = "utm" + "x" * 29
        url = (
            "https://brand.example/landing"
            "?utm_source=facebook&utm_medium=paid&utm_campaign=spring_sale"
            "&utm_content=creative_a&fbclid=IwAR123&page=2"
        )
        self.assertEqual(
            self.ingest(sid, {"events": self.EVENTS, "url": url}),
            200,
        )
        listed = self.listed(sid)
        self.assertEqual(
            listed["attribution"],
            {
                "utm_source": "facebook",
                "utm_medium": "paid",
                "utm_campaign": "spring_sale",
                "utm_content": "creative_a",
                "fbclid": True,
            },
        )

    def test_plain_url_has_no_attribution(self):
        sid = "plain" + "x" * 27
        self.assertEqual(
            self.ingest(sid, {"events": self.EVENTS, "url": "https://brand.example/p/1"}),
            200,
        )
        self.assertNotIn("attribution", self.listed(sid))

    def test_parse_attribution_helpers(self):
        from capture import server

        self.assertEqual(
            server.parse_attribution(
                "https://x.test/?utm_campaign=fall&fbclid=abc&utm_source=meta"
            ),
            {"utm_campaign": "fall", "fbclid": True, "utm_source": "meta"},
        )
        self.assertIsNone(server.parse_attribution("https://x.test/path"))
        self.assertIsNone(server.parse_attribution(None))


class TestCutEndpoint(ServerCase):
    env = {
        "GEMINI_API_KEY": "test-key-not-used-when-cached",
        "ANALYZE_BATCH": "0",
    }

    def order(self, session_id, payload):
        return request(
            f"{self.base}/order?session_id={session_id}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )[0]

    def seed(self, sid, purchased=False):
        self.assertEqual(self.ingest(sid), 200)
        if purchased:
            self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
        analyze.write_analysis(
            self.data_dir / "sessions" / sid,
            {"text": f"write-up for {sid[:6]}", "model": "test", "warnings": []},
        )

    def a_cohort(self):
        self.seed("cutbuy" + "x" * 26, purchased=True)
        self.seed("cutall" + "x" * 26)
        qs = urllib.parse.urlencode(
            {
                "purchased": 1,
                "all": 1,
                "min_duration_ms": 0,
                "url_contains": "",
            }
        )
        status, body = request(
            f"{self.base}/api/cohorts?{qs}", method="POST", data=b""
        )
        self.assertEqual(status, 201, body)
        cohort_id = json.loads(body)["id"]
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/analyze", method="POST", data=b""
        )
        self.assertEqual(status, 202, body)
        deadline = time.time() + 10
        while time.time() < deadline:
            _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
            if json.loads(body)["status"] == "done":
                break
            time.sleep(0.1)
        return cohort_id

    def test_uncached_cut_is_absent_rather_than_a_silent_model_call(self):
        cohort_id = self.a_cohort()
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/cut?outcome=purchased"
        )
        self.assertEqual(status, 404)
        data = json.loads(body)
        self.assertEqual(data["status"], "absent")
        self.assertEqual(data["cohort_id"], cohort_id)
        # Nothing was written, so no call was made.
        self.assertEqual(self.stored_cuts(cohort_id), {})

    def test_a_cached_cut_is_served_without_run(self):
        from capture import analyze

        cohort_id = self.a_cohort()
        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(
            self.data_dir / "sessions",
            cohort_id,
            key,
            {
                "cut": key,
                "label": "purchased",
                "n": 1,
                "analysis": {
                    "headline": "cached headline",
                    "patterns": [],
                    "friction": [],
                    "caveats": [],
                },
            },
        )
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/cut?outcome=purchased"
        )
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertTrue(data["cached"])
        self.assertEqual(data["analysis"]["headline"], "cached headline")

    def test_a_cut_cached_under_an_older_prompt_is_not_served(self):
        """The bug this guards: cut_key hashed only the filter, so a reworded
        CUT_PROMPT kept serving answers generated by the previous one — and the
        response still claimed `cached: true`, so there was no signal either.

        A key without the prompt hash is exactly what a pre-fix cut looks like.
        """
        from capture import analyze

        cohort_id = self.a_cohort()
        stale_key = json.dumps(
            {"filter": analyze.normalize_slice({"outcome": "purchased"})},
            sort_keys=True,
            separators=(",", ":"),
        )
        analyze.write_cut(
            self.data_dir / "sessions",
            cohort_id,
            stale_key,
            {"cut": stale_key, "analysis": {"headline": "answer from the old prompt"}},
        )
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/cut?outcome=purchased"
        )
        # Absent, not a cache hit: the old answer is unreachable and the cut
        # will be re-asked rather than silently reused.
        self.assertEqual(status, 404, body)
        self.assertEqual(json.loads(body)["status"], "absent")
        self.assertNotEqual(json.loads(body)["cut"], stale_key)

    def test_the_served_cut_key_carries_the_prompt_version(self):
        from capture import analyze

        cohort_id = self.a_cohort()
        _, body = request(f"{self.base}/api/cohorts/{cohort_id}/cut?outcome=purchased")
        served = json.loads(json.loads(body)["cut"])
        self.assertEqual(served["prompt"], analyze.prompt_version(analyze.CUT_PROMPT))

    def test_each_filter_is_cached_separately(self):
        from capture import analyze

        cohort_id = self.a_cohort()
        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(
            self.data_dir / "sessions", cohort_id, key, {"cut": key, "analysis": {}}
        )
        # A different filter is a different key, so it is still a miss.
        status, _ = request(
            f"{self.base}/api/cohorts/{cohort_id}/cut?utm_source=google"
        )
        self.assertEqual(status, 404)

    def test_cut_on_a_missing_cohort_is_404(self):
        status, body = request(f"{self.base}/api/cohorts/cohort-nope/cut")
        self.assertEqual(status, 404)
        self.assertIn("no such cohort", json.loads(body)["error"])

    def _cache_a_cut(self, cohort_id):
        from capture import analyze

        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(
            self.data_dir / "sessions",
            cohort_id,
            key,
            {"cut": key, "label": "purchased", "n": 1, "analysis": {"headline": "old"}},
        )
        return key

    def test_refresh_drops_the_cached_answer_and_queues_a_new_call(self):
        from capture import analyze

        cohort_id = self.a_cohort()
        key = self._cache_a_cut(cohort_id)
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/cut?outcome=purchased&refresh=1"
        )
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["status"], "running")
        # The stale answer is gone, so nothing can serve it while the run is in
        # flight — the point of the refresh.
        self.assertIsNone(
            analyze.read_cut(self.data_dir / "sessions", cohort_id, key)
        )

    def test_refresh_on_a_cut_that_was_never_cached_just_runs(self):
        cohort_id = self.a_cohort()
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/cut?outcome=purchased&refresh=1"
        )
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)["status"], "running")

    def test_without_refresh_the_cache_still_answers(self):
        cohort_id = self.a_cohort()
        self._cache_a_cut(cohort_id)
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/cut?outcome=purchased"
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["cached"])


class TestCutSlices(ServerCase):
    """Facets and free-form group slicing over a cohort with a known shape."""

    env = {
        "GEMINI_API_KEY": "test-key-not-used-when-cached",
        "ANALYZE_BATCH": "0",
    }

    SPECS = [
        ("p" * 32, "purchased", 360, "https://shop.test/p?utm_source=google"),
        ("c" * 32, "checkout", 390, "https://shop.test/collections/sale?utm_source=facebook"),
        ("r" * 32, "rest", 1440, "https://shop.test/p?utm_campaign=spring"),
        ("q" * 32, "purchased", 1280, "https://shop.test/products/cream"),
    ]

    def order(self, session_id, payload):
        return request(
            f"{self.base}/order?session_id={session_id}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )[0]

    def setUp(self):
        if getattr(self.__class__, "_seeded", None):
            return
        for sid, outcome, width, url in self.SPECS:
            events = [{"type": 4, "data": {"width": width, "height": 800}}]
            self.assertEqual(
                self.ingest(sid, {"events": events, "url": url}), 200
            )
            if outcome == "purchased":
                self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            elif outcome == "checkout":
                self.assertEqual(self.order(sid, {"event": "checkout_started"}), 200)
            analyze.write_analysis(
                self.data_dir / "sessions" / sid,
                {"text": f"write-up {outcome}", "model": "test", "warnings": []},
            )
        qs = urllib.parse.urlencode(
            {
                "purchased": 2,
                "all": 2,
                "min_duration_ms": 0,
                "url_contains": "",
            }
        )
        status, body = request(
            f"{self.base}/api/cohorts?{qs}", method="POST", data=b""
        )
        self.assertEqual(status, 201, body)
        cohort_id = json.loads(body)["id"]
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/analyze", method="POST", data=b""
        )
        self.assertEqual(status, 202, body)
        cohort_id = json.loads(body)["id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
            if json.loads(body)["status"] == "done":
                break
            time.sleep(0.1)
        self.__class__._seeded = cohort_id

    @property
    def cohort_id(self):
        return self.__class__._seeded

    def test_cohort_rows_carry_device_outcome_and_page(self):
        _, body = request(f"{self.base}/api/cohorts/{self.cohort_id}")
        rows = {s["session_id"][0]: s for s in json.loads(body)["sessions"]}
        self.assertEqual(rows["p"]["outcome"], "purchased")
        self.assertEqual(rows["p"]["device"], "mobile")
        self.assertEqual(rows["p"]["page"], "/p")
        self.assertEqual(rows["c"]["outcome"], "checkout")
        self.assertEqual(rows["c"]["device"], "mobile")
        self.assertEqual(rows["c"]["page"], "/collections/sale")
        self.assertEqual(rows["r"]["outcome"], "rest")
        self.assertEqual(rows["r"]["device"], "desktop")
        self.assertEqual(rows["q"]["page"], "/products/cream")

    def test_derived_fields_are_not_frozen_into_the_cohort_file(self):
        saved = self.stored_cohort(self.cohort_id)
        for entry in saved["sessions"]:
            self.assertNotIn("device", entry)
            self.assertNotIn("outcome", entry)
            self.assertNotIn("page", entry)
            self.assertNotIn("attribution", entry)

    def test_facets_report_only_values_present_in_the_cohort(self):
        status, body = request(f"{self.base}/api/cohorts/{self.cohort_id}/facets")
        self.assertEqual(status, 200, body)
        facets = json.loads(body)
        self.assertEqual(facets["n"], 4)
        self.assertEqual(
            {f["value"]: f["count"] for f in facets["outcome"]},
            {"purchased": 2, "checkout": 1, "rest": 1},
        )
        self.assertEqual(
            {f["value"]: f["count"] for f in facets["device"]},
            {"mobile": 2, "desktop": 2},
        )
        self.assertEqual(
            {f["value"]: f["count"] for f in facets["page"]},
            {"/p": 2, "/collections/sale": 1, "/products/cream": 1},
        )
        self.assertEqual(
            {f["value"] for f in facets["utm_source"]}, {"google", "facebook"}
        )
        self.assertEqual([f["value"] for f in facets["utm_campaign"]], ["spring"])
        # Presets are gone; the picker builds a group from these facets alone.
        self.assertNotIn("presets", facets)

    def test_outcome_facet_is_labelled_for_humans(self):
        _, body = request(f"{self.base}/api/cohorts/{self.cohort_id}/facets")
        labels = {f["value"]: f["label"] for f in json.loads(body)["outcome"]}
        self.assertEqual(labels["checkout"], "reached checkout")

    def cut(self, query):
        return request(f"{self.base}/api/cohorts/{self.cohort_id}/cut?{query}")

    def test_every_slice_combination_is_accepted(self):
        for query in (
            "",
            "outcome=purchased",
            "device=mobile",
            "page=/p",
            "outcome=purchased&device=mobile",
            "utm_source=google",
            "utm_campaign=spring",
            "page=/products/cream&outcome=purchased",
            "outcome=checkout&outcome=rest",
            "outcome=checkout,rest",
            "outcome=purchased&device=mobile&utm_source=google",
        ):
            status, body = self.cut(query)
            # Uncached, but a 404/absent proves the slice parsed and selected.
            self.assertEqual(status, 404, f"{query} -> {body}")
            self.assertEqual(json.loads(body)["status"], "absent")

    def test_repeated_and_comma_forms_are_the_same_cut(self):
        _, one = self.cut("outcome=checkout&outcome=rest")
        _, two = self.cut("outcome=checkout,rest")
        self.assertEqual(json.loads(one)["cut"], json.loads(two)["cut"])

    def test_value_order_does_not_change_the_cut_key(self):
        _, one = self.cut("outcome=checkout,rest")
        _, two = self.cut("outcome=rest,checkout")
        self.assertEqual(json.loads(one)["cut"], json.loads(two)["cut"])

    def test_different_slices_are_different_cuts(self):
        _, one = self.cut("device=mobile")
        _, two = self.cut("device=desktop")
        self.assertNotEqual(json.loads(one)["cut"], json.loads(two)["cut"])
        _, page_a = self.cut("page=/p")
        _, page_b = self.cut("page=/products/cream")
        self.assertNotEqual(json.loads(page_a)["cut"], json.loads(page_b)["cut"])

    def test_an_empty_filter_is_the_whole_cohort_cut(self):
        from capture import analyze

        _, body = self.cut("")
        self.assertEqual(json.loads(body)["cut"], analyze.cut_key(None))

    def test_bad_slice_values_are_rejected(self):
        for query, word in (
            ("outcome=nonsense", "outcome must be"),
            ("device=tablet", "device must be"),
            ("outcome=maybe", "outcome must be"),
        ):
            status, body = self.cut(query)
            self.assertEqual(status, 400, query)
            self.assertIn(word, json.loads(body)["error"])


class TestCohortUnavailable(ServerCase):
    env = {"GEMINI_API_KEY": ""}

    def test_sampling_needs_no_key_but_starting_analysis_does(self):
        """Sampling only reads the index, so it works without a key; starting
        analysis is what spends one, and that is what must refuse."""
        sid = "d" * 32
        self.assertEqual(self.ingest(sid), 200)
        status, body = request(
            f"{self.base}/api/cohorts?purchased=0&all=1&min_duration_ms=0&url_contains=",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)

        cohort_id = json.loads(body)["id"]
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/analyze", method="POST", data=b""
        )
        self.assertEqual(status, 503)
        self.assertIn("not configured", json.loads(body)["error"])

    def test_a_refusable_refresh_keeps_the_cached_answer(self):
        """Without a key the refresh cannot be served — it must leave the good
        answer standing rather than delete what it cannot replace."""
        from capture import analyze

        pool = self.data_dir / "sessions"
        cohort = analyze.write_cohort(
            pool,
            {"id": "cohort-keyless", "status": "done", "sessions": []},
        )
        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(pool, cohort["id"], key, {"cut": key, "analysis": {"headline": "old"}})
        status, _ = request(
            f"{self.base}/api/cohorts/{cohort['id']}/cut?outcome=purchased&refresh=1"
        )
        self.assertEqual(status, 503)
        still = analyze.read_cut(pool, cohort["id"], key)
        self.assertEqual(still["analysis"]["headline"], "old")


class TestCohortSampling(unittest.TestCase):
    def _conn(self, rows):
        from capture import analyze

        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE sessions (
                pool TEXT, session_id TEXT, first_seen INTEGER, last_seen INTEGER,
                chunk_count INTEGER, bytes INTEGER, visitor_id TEXT, url TEXT,
                "order" TEXT, interactions TEXT, device TEXT, attribution TEXT
            )
            """
        )
        conn.executemany(
            'INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            rows,
        )
        return analyze, conn

    def test_sample_prefers_newest_and_splits_filters(self):
        analyze, conn = self._conn(
            [
                ("", "oldbuy" + "x" * 26, 0, 10_000, 1, 1, None, "https://x.test/pages/ashwamag-01", json.dumps({"purchased_at": 1}), None, None, None),
                ("", "newbuy" + "x" * 26, 0, 30_000, 1, 1, None, "https://x.test/pages/ashwamag-01", json.dumps({"purchased_at": 2}), json.dumps({"clicks": 3}), None, None),
                ("", "oldall" + "x" * 26, 0, 5_000, 1, 1, None, "https://x.test/pages/ashwamag-01", None, None, None, None),
                ("", "newall" + "x" * 26, 0, 40_000, 1, 1, None, "https://x.test/pages/ashwamag-01", None, json.dumps({"clicks": 2}), None, None),
                ("", "nochunk" + "x" * 25, 0, 50_000, 0, 0, None, "https://x.test/pages/ashwamag-01", json.dumps({"purchased_at": 3}), None, None, None),
            ]
        )
        sampled = analyze.sample_cohort_rows(conn, "", purchased_n=1, all_n=1)
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("newbuy" + "x" * 26, "purchased"), ("newall" + "x" * 26, "all")],
        )

    def test_sample_requires_ashwamag_path_and_min_duration(self):
        analyze, conn = self._conn(
            [
                ("", "short" + "x" * 27, 0, 4_999, 1, 1, None, "https://x.test/pages/ashwamag-01", json.dumps({"purchased_at": 1}), None, None, None),
                ("", "other" + "x" * 27, 0, 20_000, 1, 1, None, "https://x.test/pages/other", json.dumps({"purchased_at": 1}), None, None, None),
                ("", "okbuy" + "x" * 27, 0, 20_000, 1, 1, None, "https://x.test/pages/ashwamag-01?x=1", json.dumps({"purchased_at": 1}), None, None, None),
                ("", "okall" + "x" * 27, 0, 20_000, 1, 1, None, "https://x.test/pages/ashwamag-01", None, None, None, None),
            ]
        )
        sampled = analyze.sample_cohort_rows(conn, "", purchased_n=5, all_n=5)
        self.assertEqual(
            {(s["session_id"], s["filter"]) for s in sampled},
            {("okbuy" + "x" * 27, "purchased"), ("okall" + "x" * 27, "all")},
        )

    def test_sample_any_mixes_outcomes_up_to_limit(self):
        analyze, conn = self._conn(
            [
                ("", "buy1" + "x" * 28, 0, 30_000, 1, 1, None, "https://x.test/pages/ashwamag-01", json.dumps({"purchased_at": 1}), json.dumps({"clicks": 2}), None, None),
                ("", "rest1" + "x" * 27, 0, 20_000, 1, 1, None, "https://x.test/pages/ashwamag-01", None, json.dumps({"clicks": 1}), None, None),
                ("", "buy2" + "x" * 28, 0, 10_000, 1, 1, None, "https://x.test/pages/ashwamag-01", json.dumps({"purchased_at": 2}), None, None, None),
            ]
        )
        sampled = analyze.sample_cohort_rows(conn, "", purchased_n=0, all_n=0, any_n=2)
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("buy1" + "x" * 28, "purchased"), ("rest1" + "x" * 27, "all")],
        )

    def test_sample_honors_cut_style_slice_filters(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "mob" + "x" * 29,
                    0,
                    40_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01?utm_source=google",
                    json.dumps({"purchased_at": 1}),
                    None,
                    json.dumps({"width": 390}),
                    json.dumps({"utm_source": "google"}),
                ),
                (
                    "",
                    "desk" + "x" * 28,
                    0,
                    50_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01?utm_source=google",
                    json.dumps({"purchased_at": 2}),
                    None,
                    json.dumps({"width": 1280}),
                    json.dumps({"utm_source": "google"}),
                ),
                (
                    "",
                    "fb" + "x" * 30,
                    0,
                    60_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01?utm_source=facebook",
                    None,
                    None,
                    json.dumps({"width": 390}),
                    json.dumps({"utm_source": "facebook"}),
                ),
            ]
        )
        sampled = analyze.sample_cohort_rows(
            conn,
            "",
            purchased_n=0,
            all_n=0,
            any_n=5,
            url_contains="",
            slice_spec={"device": ["mobile"], "utm_source": "google", "page": "/pages/ashwamag-01"},
        )
        self.assertEqual(
            [s["session_id"] for s in sampled],
            ["mob" + "x" * 29],
        )

    def test_sample_groups_a_and_b_with_independent_slices(self):
        analyze, conn = self._conn(
            [
                (
                    "",
                    "buy" + "x" * 29,
                    0,
                    50_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    json.dumps({"purchased_at": 1}),
                    None,
                    json.dumps({"width": 390}),
                    None,
                ),
                (
                    "",
                    "rest" + "x" * 28,
                    0,
                    40_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/ashwamag-01",
                    None,
                    None,
                    json.dumps({"width": 390}),
                    None,
                ),
                (
                    "",
                    "other" + "x" * 27,
                    0,
                    30_000,
                    1,
                    1,
                    None,
                    "https://x.test/pages/other",
                    None,
                    None,
                    json.dumps({"width": 390}),
                    None,
                ),
            ]
        )
        sampled = analyze.sample_cohort_rows(
            conn,
            "",
            purchased_n=0,
            all_n=0,
            a_n=1,
            b_n=1,
            url_contains="",
            slice_spec={"outcome": ["purchased"], "page": "/pages/ashwamag-01"},
            slice_b={"outcome": ["checkout", "rest"], "page": "/pages/ashwamag-01"},
        )
        self.assertEqual(
            [(s["session_id"], s["filter"]) for s in sampled],
            [("buy" + "x" * 29, "a"), ("rest" + "x" * 28, "b")],
        )

    def test_sample_time_window_and_earliest_order(self):
        analyze, conn = self._conn(
            [
                ("", "old" + "x" * 29, 0, 10_000, 1, 1, None, "https://x.test/pages/ashwamag-01", None, None, None, None),
                ("", "mid" + "x" * 29, 0, 20_000, 1, 1, None, "https://x.test/pages/ashwamag-01", None, None, None, None),
                ("", "new" + "x" * 29, 0, 30_000, 1, 1, None, "https://x.test/pages/ashwamag-01", None, None, None, None),
            ]
        )
        windowed = analyze.sample_cohort_rows(
            conn,
            "",
            purchased_n=0,
            all_n=0,
            any_n=5,
            url_contains="",
            start_ms=15_000,
            end_ms=25_000,
        )
        self.assertEqual([s["session_id"] for s in windowed], ["mid" + "x" * 29])

        earliest = analyze.sample_cohort_rows(
            conn,
            "",
            purchased_n=0,
            all_n=0,
            any_n=2,
            url_contains="",
            order=analyze.COHORT_ORDER_EARLIEST,
        )
        self.assertEqual(
            [s["session_id"] for s in earliest],
            ["old" + "x" * 29, "mid" + "x" * 29],
        )

    def test_standout_line_prefers_what_stands_out_section(self):
        from capture import analyze

        text = (
            "## What they did\nWent to cart.\n\n"
            "## What stands out\n- Shipping fee surprise at checkout.\n- Also size chart.\n"
        )
        self.assertEqual(analyze.standout_line(text), "Shipping fee surprise at checkout.")
        self.assertEqual(analyze.standout_line("## Hello\nPlain opener."), "Plain opener.")


if __name__ == "__main__":
    if not UVICORN.exists():
        sys.exit(f"missing {UVICORN} — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    unittest.main()
