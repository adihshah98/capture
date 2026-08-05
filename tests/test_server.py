"""Server tests: auth, ingest limits, storage quotas, and the delete path.

Each case boots a real uvicorn process against a throwaway CAPTURE_DATA_DIR,
so this exercises what actually ships — middleware order, env parsing and all
— rather than an in-process approximation. Only the standard library is used;
there is no test dependency to install.

Run: python -m unittest discover -s tests
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

REPO = Path(__file__).resolve().parent.parent
UVICORN = REPO / ".venv" / "bin" / "uvicorn"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(url, method="GET", data=None, headers=None):
    """Returns (status, body-bytes). HTTP errors are values here, not raises."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


class ServerCase(unittest.TestCase):
    """Boots one server per test class with the given environment."""

    env: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-test-"))
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        env = {
            **os.environ,
            "CAPTURE_DATA_DIR": str(cls.data_dir),
            "REPLAY_AUTH_DISABLED": "1",
            **cls.env,
        }
        cls.proc = subprocess.Popen(
            [str(UVICORN), "server:app", "--port", str(cls.port), "--log-level", "error"],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
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
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        shutil.rmtree(cls.data_dir, ignore_errors=True)

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
        # A list parses as JSON but has no .get; this used to be an unhandled 500.
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
        bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))  # ~8 KB on the wire
        self.assertLess(len(bomb), 100 * 1024)
        self.assertEqual(self.ingest("x" * 32, raw=bomb, gzipped=False), 400)
        # The point of the ceiling is that the process is still there.
        self.assertEqual(request(f"{self.base}/config")[0], 200)


class TestQuota(ServerCase):
    env = {"MAX_PROJECT_BYTES": "1"}

    def test_over_quota_returns_429(self):
        self.ingest("y" * 32)  # first write establishes usage
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
    """The Shopify custom pixel posts checkout outcomes to /order; they show
    up on the session in the list API but never as replayable chunks."""

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
        # deploy.sh and any uptime check must reach it unauthenticated.
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
        # The failure /config could never surface: process up, storage dead.
        self.assertEqual(request(f"{self.base}/healthz")[0], 200)
        mode = self.data_dir.stat().st_mode
        os.chmod(self.data_dir, 0o500)
        try:
            status, body = request(f"{self.base}/healthz")
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(body)["checks"]["storage_writable"])
            # /config still answers 200 — which is why it was a bad health check.
            self.assertEqual(request(f"{self.base}/config")[0], 200)
        finally:
            os.chmod(self.data_dir, mode)


class TestProjectTokens(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Written after boot: load_projects() reads from disk per request.
        cls.token = "cap_testtokentesttokentest"
        (cls.data_dir / "projects.json").write_text(
            json.dumps({cls.token: {"name": "Test", "created_at": 0}})
        )

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

    def test_project_sessions_are_isolated_from_the_default_pool(self):
        self.assertEqual(self.ingest("j" * 32, token=self.token), 200)
        scoped = json.loads(request(f"{self.base}/api/sessions?project={self.token}")[1])
        default = json.loads(request(f"{self.base}/api/sessions")[1])
        self.assertIn("j" * 32, [s["session_id"] for s in scoped["sessions"]])
        self.assertNotIn("j" * 32, [s["session_id"] for s in default["sessions"]])


class TestDomainAllowlist(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.token = "cap_domainstesttokentest"
        (cls.data_dir / "projects.json").write_text(
            json.dumps({cls.token: {"name": "Test", "domains": ["brand.com"]}})
        )

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
    """The CLI runs from a shell, not systemd, so it resolves its own paths."""

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="capture-cli-"))
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)

    def run_cli(self, *args, env=None):
        result = subprocess.run(
            [str(REPO / ".venv" / "bin" / "python"), "server.py", *args],
            cwd=REPO,
            env={**os.environ, "CAPTURE_DATA_DIR": str(self.data_dir), **(env or {})},
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.stdout + result.stderr

    def test_startup_prints_no_deprecation_warning(self):
        # on_event was deprecated and printed on every CLI invocation.
        out = self.run_cli("list-projects")
        self.assertNotIn("DeprecationWarning", out)
        self.assertNotIn("on_event is deprecated", out)

    def test_add_project_uses_the_configured_public_url(self):
        out = self.run_cli("add-project", "Acme", env={"CAPTURE_PUBLIC_URL": "https://cdn.example.net/"})
        self.assertIn('src="https://cdn.example.net/capture.js"', out)
        self.assertNotIn("capture.example.com", out)

    def test_public_url_file_is_used_when_the_env_var_is_absent(self):
        (self.data_dir / "public_url").write_text("https://from-file.example\n")
        out = self.run_cli("add-project", "Acme")
        self.assertIn('src="https://from-file.example/capture.js"', out)

    def test_placeholder_host_is_called_out(self):
        out = self.run_cli("add-project", "Acme")
        self.assertIn("capture.example.com", out)
        self.assertIn("placeholder host", out)

    def test_set_domains_round_trip(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        out = self.run_cli("set-domains", token, "https://Brand.com/path", "shop.example")
        self.assertIn("brand.com, shop.example", out)
        stored = json.loads((self.data_dir / "projects.json").read_text())
        self.assertEqual(stored[token]["domains"], ["brand.com", "shop.example"])

        out = self.run_cli("set-domains", token)
        self.assertIn("any domain", out)
        stored = json.loads((self.data_dir / "projects.json").read_text())
        self.assertEqual(stored[token]["domains"], [])

    def test_list_and_remove_round_trip(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        self.assertIn(token, self.run_cli("list-projects"))

        # Revoking keeps recordings unless --purge is passed.
        sessions = self.data_dir / "projects" / token / "sessions" / "s1"
        sessions.mkdir(parents=True)
        (sessions / "1-abcdef.json").write_text('{"events":[1]}')

        out = self.run_cli("remove-project", token)
        self.assertIn("revoked", out)
        self.assertTrue(sessions.exists(), "recordings must survive a plain revoke")
        self.assertNotIn(token, self.run_cli("list-projects"))

    def test_purge_deletes_the_recordings(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        sessions = self.data_dir / "projects" / token / "sessions" / "s1"
        sessions.mkdir(parents=True)
        (sessions / "1-abcdef.json").write_text('{"events":[1]}')

        self.run_cli("remove-project", token, "--purge")
        self.assertFalse((self.data_dir / "projects" / token).exists())


if __name__ == "__main__":
    if not UVICORN.exists():
        sys.exit(f"missing {UVICORN} — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    unittest.main()
