
import base64
import hashlib
import hmac
import http.server
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
from test_server import ServerCase, request

SECRET = "test-session-secret"

MEMBERSHIPS = {"user_alpha": ["org_alpha"], "user_beta": ["org_beta"]}
STUB_CALLS = []

class WorkOSStub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        user = (query.get("user_id") or [""])[0]
        STUB_CALLS.append(user)
        body = json.dumps(
            {"data": [{"organization_id": org} for org in MEMBERSHIPS.get(user, [])]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass

STUB = http.server.ThreadingHTTPServer(("127.0.0.1", 0), WorkOSStub)
threading.Thread(target=STUB.serve_forever, daemon=True).start()

WORKOS_ENV = {
    "REPLAY_AUTH_DISABLED": "",
    "WORKOS_API_KEY": "sk_test_fake",
    "WORKOS_CLIENT_ID": "client_test_fake",
    "WORKOS_API": f"http://127.0.0.1:{STUB.server_address[1]}",
    "SESSION_SECRET": SECRET,
    "CAPTURE_OPERATORS": "op@example.com",
}

class NoRedirect(urllib.request.HTTPRedirectHandler):

    def redirect_request(self, *args, **kwargs):
        return None

def request_no_redirect(url, headers=None):
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(req, timeout=10) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()

def sign_cookie(payload, secret=SECRET):
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"capture_session={raw}.{sig}"

def session_cookie(email, user_id, secret=SECRET, exp_in=3600):
    return sign_cookie(
        {"email": email, "user_id": user_id, "exp": int(time.time()) + exp_in}, secret
    )

class TestWorkOSScoping(ServerCase):

    # Only the two org-scoped projects exist here, as before.
    default_project = False
    env = {**WORKOS_ENV, "ORG_CACHE_TTL_S": "0"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for token, name, org in (
            ("cap_alpha", "Alpha", "org_alpha"),
            ("cap_beta", "Beta", "org_beta"),
        ):
            pgfixture.seed_project(cls.admin_url, name, token)
            pgfixture.set_project_org(cls.admin_url, token, org)
            pgfixture.seed_session(cls.admin_url, token, "s" * 32)
            # The read and delete paths still resolve the chunk directory.
            session = cls.data_dir / "projects" / token / "sessions" / ("s" * 32)
            session.mkdir(parents=True, exist_ok=True)
            (session / f"{int(time.time() * 1000)}-abc123.json").write_text(
                json.dumps({"events": [{"type": 4, "data": {}}]})
            )

    def api(self, path, cookie):
        return request(f"{self.base}{path}", headers={"Cookie": cookie})

    def test_replay_html_redirects_to_login(self):
        status, headers, _ = request_no_redirect(f"{self.base}/replay.html")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/login")

    def test_api_401_points_at_login(self):
        status, body = request(f"{self.base}/api/sessions")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body).get("login"), "/login")

    def test_login_redirects_to_workos(self):
        status, headers, _ = request_no_redirect(f"{self.base}/login")
        self.assertEqual(status, 302)
        location = headers.get("location", "")
        self.assertIn("/user_management/authorize", location)
        self.assertIn("client_test_fake", location)

    def test_callback_without_matching_state_restarts_login(self):
        status, headers, _ = request_no_redirect(f"{self.base}/auth/callback?code=x&state=y")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/login")

    def test_callback_with_code_but_no_state_restarts_login(self):

        status, headers, _ = request_no_redirect(f"{self.base}/auth/callback?code=x")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("location"), "/login")

    def test_basic_credentials_do_not_work_in_workos_mode(self):
        creds = base64.b64encode(b"any:whatever").decode()
        status, _ = request(
            f"{self.base}/api/sessions?project=cap_alpha",
            headers={"Authorization": f"Basic {creds}"},
        )
        self.assertEqual(status, 401)

    def test_forged_signature_is_rejected(self):
        cookie = session_cookie("who@evil.com", "user_alpha", secret="wrong-secret")
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 401)

    def test_expired_session_is_rejected(self):
        cookie = session_cookie("a@alpha.com", "user_alpha", exp_in=-10)
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 401)

    def test_legacy_org_snapshot_cookie_is_rejected(self):
        cookie = sign_cookie(
            {"email": "a@alpha.com", "orgs": ["org_alpha"], "exp": int(time.time()) + 3600}
        )
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 401)

    def test_member_sees_only_their_own_project(self):
        cookie = session_cookie("a@alpha.com", "user_alpha")
        status, body = self.api("/api/projects", cookie)
        self.assertEqual(status, 200)
        parsed = json.loads(body)
        self.assertEqual([p["token"] for p in parsed["projects"]], ["cap_alpha"])
        self.assertEqual(parsed["email"], "a@alpha.com")

    def test_member_reads_their_project(self):
        cookie = session_cookie("a@alpha.com", "user_alpha")
        status, body = self.api("/api/sessions?project=cap_alpha", cookie)
        self.assertEqual(status, 200)
        ids = [s["session_id"] for s in json.loads(body)["sessions"]]
        self.assertIn("s" * 32, ids)

    def test_cross_project_read_is_403(self):
        cookie = session_cookie("a@alpha.com", "user_alpha")
        self.assertEqual(self.api("/api/sessions?project=cap_beta", cookie)[0], 403)
        self.assertEqual(
            self.api(f"/api/sessions/{'s' * 32}/chunks?project=cap_beta", cookie)[0], 403
        )
        self.assertEqual(
            self.api(
                f"/api/sessions/{'s' * 32}/chunks/1700000000000-abc123.json?project=cap_beta",
                cookie,
            )[0],
            403,
        )

    def test_legacy_pool_is_hidden_from_members(self):
        cookie = session_cookie("a@alpha.com", "user_alpha")
        self.assertEqual(self.api("/api/sessions", cookie)[0], 403)

    def test_orgless_user_sees_nothing(self):
        cookie = session_cookie("stranger@nowhere.com", "user_none")
        status, body = self.api("/api/projects", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["projects"], [])
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 403)

    def test_membership_changes_apply_without_relogin(self):
        cookie = session_cookie("late@alpha.com", "user_late")
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 403)
        MEMBERSHIPS["user_late"] = ["org_alpha"]
        try:
            self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 200)
        finally:
            del MEMBERSHIPS["user_late"]
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 403)

    def test_operator_sees_everything_without_membership_lookup(self):
        cookie = session_cookie("op@example.com", "user_op")
        tokens = [p["token"] for p in json.loads(self.api("/api/projects", cookie)[1])["projects"]]
        self.assertEqual(sorted(tokens), ["cap_alpha", "cap_beta"])
        self.assertEqual(self.api("/api/sessions?project=cap_beta", cookie)[0], 200)
        self.assertNotIn("user_op", STUB_CALLS)

    def test_member_can_delete_within_their_project(self):
        cookie = session_cookie("b@beta.com", "user_beta")
        status, _ = request(
            f"{self.base}/api/sessions/{'s' * 32}?project=cap_beta",
            method="DELETE",
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)

    def test_ingest_still_needs_only_the_project_token(self):
        self.assertEqual(self.ingest("w" * 32, token="cap_alpha"), 200)

class TestMembershipCache(ServerCase):
    env = {**WORKOS_ENV, "ORG_CACHE_TTL_S": "60"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pgfixture.seed_project(cls.admin_url, "Alpha", "cap_alpha")
        pgfixture.set_project_org(cls.admin_url, "cap_alpha", "org_alpha")

    def test_membership_lookup_is_cached(self):
        cookie = session_cookie("a@alpha.com", "user_cached")
        MEMBERSHIPS["user_cached"] = ["org_alpha"]
        try:
            for _ in range(3):
                status, _ = request(
                    f"{self.base}/api/sessions?project=cap_alpha", headers={"Cookie": cookie}
                )
                self.assertEqual(status, 200)
        finally:
            del MEMBERSHIPS["user_cached"]
        self.assertEqual(STUB_CALLS.count("user_cached"), 1)

if __name__ == "__main__":
    unittest.main()
