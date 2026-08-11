"""Who gets in: replay password, project tokens, pool isolation, domain allowlist."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
from servercase import ServerCase, request


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
            request(f"{self.base}/order?session_id={session}&token={self.token}", "POST", body)[0],
            200,
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


if __name__ == "__main__":
    unittest.main()
