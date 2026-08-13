"""Per-session analysis: what /api/sessions/{id}/analysis reports and refuses."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from servercase import ServerCase, request

from capture.analyze import storage


class TestAnalysisStatus(ServerCase):
    # Isolate from a developer machine that has GEMINI_API_KEY in the environment.
    env = {"GEMINI_API_KEY": ""}

    def test_get_returns_cached_analysis(self):
        sid = "a" * 32
        self.assertEqual(self.ingest(sid), 200)
        storage.write_analysis(
            self.project_id,
            sid,
            {"text": "hello", "model": "test", "frame_count": 1, "warnings": []},
        )
        status, body = request(f"{self.base}/api/sessions/{sid}/analysis")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["text"], "hello")
        self.assertEqual(data["status"], "done")

    def test_get_returns_running_job(self):
        sid = "b" * 32
        self.assertEqual(self.ingest(sid), 200)
        storage.write_job(self.project_id, sid, "running")
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

    def test_cached_analysis_does_not_need_a_local_directory(self):
        """Staging never ingested onto disk; write-ups live in Postgres."""
        import shutil

        sid = "d" * 32
        self.assertEqual(self.ingest(sid), 200)
        storage.write_analysis(
            self.project_id,
            sid,
            {"text": "hello", "model": "test", "frame_count": 1, "warnings": []},
        )
        shutil.rmtree(self.data_dir / "sessions" / sid, ignore_errors=True)
        status, body = request(f"{self.base}/api/sessions/{sid}/analysis")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["text"], "hello")


if __name__ == "__main__":
    unittest.main()
