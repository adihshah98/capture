"""The chunk-name and data-root rules, with nothing else attached.

No database, no network, no disk: this is the fast tier the rest of the Python
suite cannot join while its configuration is read at import time. Runs in
milliseconds, so it can go on save.

test_modules_agree_on_the_data_root is a regression test. storage.py resolved
BASE_DIR one directory too deep after the move under capture/, so its local-disk
fallback looked in capture/data while server.py and projects.py used <repo>/data.
Every other test monkeypatches DATA_DIR, which is exactly why none of them saw it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capture import paths


class TestDataRoot(unittest.TestCase):
    def test_the_default_root_is_the_repo_not_the_package(self):
        self.assertEqual(paths.BASE_DIR.name, "capture")
        self.assertTrue((paths.BASE_DIR / "capture" / "__init__.py").is_file())
        self.assertEqual(paths.DATA_ROOT.parent, paths.BASE_DIR)

    def test_modules_agree_on_the_data_root(self):
        from capture import projects, server, storage

        for module in (server, projects, storage):
            self.assertEqual(
                module.DATA_DIR,
                paths.DATA_DIR,
                f"{module.__name__} disagrees about the session pool",
            )

    def test_default_pool_is_the_sessions_dir(self):
        self.assertEqual(paths.pool_for(""), paths.DATA_DIR)

    def test_a_named_project_gets_its_own_pool(self):
        self.assertEqual(paths.pool_for("abc123"), paths.PROJECTS_DIR / "abc123" / "sessions")


class TestChunkNames(unittest.TestCase):
    VALID = "1700000000000-a1b2c3d4-x7k2m9.json"

    def test_a_recorded_chunk_name_is_accepted(self):
        self.assertTrue(paths.SAFE_CHUNK.match(self.VALID))
        self.assertTrue(paths.SAFE_CHUNK.match("1700000000000-x7k2m9.json"))

    def test_traversal_and_junk_are_refused(self):
        for name in (
            "../../etc/passwd",
            "1700000000000-a1b2c3d4-x7k2m9.json.bak",
            "analysis.json",
            "window-state.json",
            "-x7k2m9.json",
            "17000000000000000000-x7k2m9.json",
        ):
            self.assertIsNone(paths.SAFE_CHUNK.match(name), name)

    def test_timestamp_comes_off_the_front(self):
        self.assertEqual(paths.chunk_timestamp(self.VALID), 1700000000000)
        self.assertEqual(paths.chunk_timestamp("1700000000000-x7k2m9.json"), 1700000000000)

    def test_an_unparseable_timestamp_sorts_first_rather_than_raising(self):
        self.assertEqual(paths.chunk_timestamp("nope-x7k2m9.json"), 0)

    def test_window_is_the_middle_segment_when_there_is_one(self):
        self.assertEqual(paths.chunk_window(self.VALID), "a1b2c3d4")
        self.assertIsNone(paths.chunk_window("1700000000000-x7k2m9.json"))


class TestSessionIds(unittest.TestCase):
    def test_ordinary_ids_are_accepted(self):
        for value in ("abc", "A-b_C-123", "x" * 64):
            self.assertTrue(paths.SAFE_ID.match(value), value)

    def test_separators_and_overlong_ids_are_refused(self):
        for value in ("", "x" * 65, "a/b", "a.b", "../x", "a b"):
            self.assertIsNone(paths.SAFE_ID.match(value), value)


if __name__ == "__main__":
    unittest.main()
