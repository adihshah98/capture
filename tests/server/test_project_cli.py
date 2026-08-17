"""The `capture.cli` project commands, driven as a subprocess like an operator would."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
from servercase import REPO


class TestProjectCli(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="capture-cli-"))
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.admin_url, self.app_url = pgfixture.fresh_database()

    async def projects_in_db(self) -> dict:
        import psycopg

        with psycopg.connect(self.admin_url) as conn:
            rows = conn.execute("SELECT p.storage_prefix, p.domains FROM projects p")
        return dict(rows)

    async def live_tokens(self) -> list:
        import psycopg

        with psycopg.connect(self.admin_url) as conn:
            rows = conn.execute("SELECT token FROM project_tokens WHERE revoked_at IS NULL")
        return [r[0] for r in rows]

    def run_cli(self, *args, env=None):
        result = subprocess.run(
            [str(REPO / ".venv" / "bin" / "python"), "-m", "capture.cli", *args],
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

    def test_the_cli_does_not_load_the_web_app(self):
        """It used to live in server.py, so inserting one row imported FastAPI and
        registered every route. Warnings from that import also landed in CLI
        output, which is what the test above was originally written to catch."""
        out = self.run_cli(
            "list-projects",
            env={"PYTHONWARNINGS": "error"},
        )
        self.assertNotIn("Traceback", out)

        loaded = subprocess.run(
            [
                str(REPO / ".venv" / "bin" / "python"),
                "-c",
                "import sys; from capture import cli; "
                "print(int('fastapi' in sys.modules), int('capture.server' in sys.modules))",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(loaded.stdout.strip(), "0 0", loaded.stderr)

    def test_add_project_uses_the_configured_public_url(self):
        out = self.run_cli(
            "add-project", "Acme", env={"CAPTURE_PUBLIC_URL": "https://cdn.example.net/"}
        )
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

    async def test_set_domains_round_trip(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        out = self.run_cli("set-domains", token, "https://Brand.com/path", "shop.example")
        self.assertIn("brand.com, shop.example", out)
        self.assertEqual(await self.projects_in_db()[token], ["brand.com", "shop.example"])

        out = self.run_cli("set-domains", token)
        self.assertIn("any domain", out)
        self.assertEqual(await self.projects_in_db()[token], [])

    async def test_list_and_remove_round_trip(self):
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
        self.assertEqual(await self.live_tokens(), [])

    def test_purge_deletes_the_recordings(self):
        created = self.run_cli("add-project", "Acme")
        token = created.split("token: ")[1].split()[0]
        sessions = self.data_dir / "projects" / token / "sessions" / "s1"
        sessions.mkdir(parents=True)
        (sessions / "1-abcdef.json").write_text('{"events":[1]}')

        self.run_cli("remove-project", token, "--purge")
        self.assertFalse((self.data_dir / "projects" / token).exists())


if __name__ == "__main__":
    unittest.main()
