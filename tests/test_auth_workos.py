# No WorkOS network calls: session cookies are minted with the same HMAC
# scheme the server uses (SESSION_SECRET pinned via the environment), which
# exercises validation, expiry, and scoping without the code-exchange path.

import base64
import hashlib
import hmac
import json
import time
import unittest
import urllib.error
import urllib.request

from test_server import ServerCase, request

SECRET = "test-session-secret"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # /login 302s to api.workos.com; following it would leave the test box.
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


def session_cookie(email, orgs, secret=SECRET, exp_in=3600):
    payload = json.dumps({"email": email, "orgs": orgs, "exp": int(time.time()) + exp_in})
    raw = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"capture_session={raw}.{sig}"


class TestWorkOSScoping(ServerCase):
    env = {
        "REPLAY_AUTH_DISABLED": "",
        "WORKOS_API_KEY": "sk_test_fake",
        "WORKOS_CLIENT_ID": "client_test_fake",
        "SESSION_SECRET": SECRET,
        "CAPTURE_OPERATORS": "op@example.com",
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.data_dir / "projects.json").write_text(
            json.dumps(
                {
                    "cap_alpha": {"name": "Alpha", "org_id": "org_alpha"},
                    "cap_beta": {"name": "Beta", "org_id": "org_beta"},
                }
            )
        )
        for token in ("cap_alpha", "cap_beta"):
            session = cls.data_dir / "projects" / token / "sessions" / ("s" * 32)
            session.mkdir(parents=True)
            (session / "1700000000000-abc123.json").write_text(
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
        self.assertIn("api.workos.com/user_management/authorize", location)
        self.assertIn("client_test_fake", location)

    def test_callback_without_matching_state_is_rejected(self):
        status, _ = request(f"{self.base}/auth/callback?code=x&state=y")
        self.assertEqual(status, 401)

    def test_basic_credentials_do_not_work_in_workos_mode(self):
        creds = base64.b64encode(b"any:whatever").decode()
        status, _ = request(
            f"{self.base}/api/sessions?project=cap_alpha",
            headers={"Authorization": f"Basic {creds}"},
        )
        self.assertEqual(status, 401)

    def test_forged_signature_is_rejected(self):
        cookie = session_cookie("who@evil.com", ["org_alpha"], secret="wrong-secret")
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 401)

    def test_expired_session_is_rejected(self):
        cookie = session_cookie("a@alpha.com", ["org_alpha"], exp_in=-10)
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 401)

    def test_member_sees_only_their_own_project(self):
        cookie = session_cookie("a@alpha.com", ["org_alpha"])
        status, body = self.api("/api/projects", cookie)
        self.assertEqual(status, 200)
        tokens = [p["token"] for p in json.loads(body)["projects"]]
        self.assertEqual(tokens, ["cap_alpha"])

    def test_member_reads_their_project(self):
        cookie = session_cookie("a@alpha.com", ["org_alpha"])
        status, body = self.api("/api/sessions?project=cap_alpha", cookie)
        self.assertEqual(status, 200)
        ids = [s["session_id"] for s in json.loads(body)["sessions"]]
        self.assertIn("s" * 32, ids)

    def test_cross_project_read_is_403(self):
        cookie = session_cookie("a@alpha.com", ["org_alpha"])
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
        cookie = session_cookie("a@alpha.com", ["org_alpha"])
        self.assertEqual(self.api("/api/sessions", cookie)[0], 403)

    def test_orgless_user_sees_nothing(self):
        cookie = session_cookie("stranger@nowhere.com", [])
        status, body = self.api("/api/projects", cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["projects"], [])
        self.assertEqual(self.api("/api/sessions?project=cap_alpha", cookie)[0], 403)

    def test_operator_sees_everything(self):
        cookie = session_cookie("op@example.com", [])
        tokens = [p["token"] for p in json.loads(self.api("/api/projects", cookie)[1])["projects"]]
        self.assertEqual(sorted(tokens), ["cap_alpha", "cap_beta"])
        self.assertEqual(self.api("/api/sessions?project=cap_beta", cookie)[0], 200)

    def test_member_can_delete_within_their_project(self):
        cookie = session_cookie("b@beta.com", ["org_beta"])
        status, _ = request(
            f"{self.base}/api/sessions/{'s' * 32}?project=cap_beta",
            method="DELETE",
            headers={"Cookie": cookie},
        )
        self.assertEqual(status, 200)

    def test_ingest_still_needs_only_the_project_token(self):
        self.assertEqual(self.ingest("w" * 32, token="cap_alpha"), 200)


if __name__ == "__main__":
    unittest.main()
