"""The non-recording surface: /healthz, cache headers on visitor assets, and the
snippet the docs promise."""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from servercase import REPO, ServerCase, request, request_headers


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


class TestIntegrationSnippetMatchesDocs(unittest.TestCase):
    def test_cli_snippet_is_the_documented_snippet(self):
        sys.path.insert(0, str(REPO))
        from capture import snippet

        doc = (REPO / "docs" / "integration.md").read_text()
        documented = doc.split("```html\n", 1)[1].split("```", 1)[0].strip()
        generated = snippet.integration_snippet(
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
                self.assertEqual(
                    request_headers(self.base + path)[1].get("Cache-Control"), "no-cache"
                )


if __name__ == "__main__":
    unittest.main()
