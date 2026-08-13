"""The scratch file a Gemini Batch submission streams its JSONL through.

Small surface, but it sits on the paid path and holds visitor data, so both of
its properties are pinned here rather than assumed: submissions must not share a
file, and the file must not be readable by other users on the box.

No database and no network — this is the fast tier.
"""

import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from capture.analyze import gemini


class TestBatchJsonlPath(unittest.TestCase):
    def test_two_submissions_never_share_a_file(self):
        """The bug this replaced: the name was `capture-batch-<epoch_ms>.jsonl`,
        so two cohorts submitting inside the same millisecond got one path. The
        caller unlinks in a `finally`, so whichever finished first deleted the
        other's payload while it was still being uploaded.

        Looping rather than mocking the clock: a thousand calls take microseconds
        each, which is exactly the window the old name collided in.
        """
        with TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            paths = [gemini._batch_jsonl_path(work_dir) for _ in range(1000)]

        self.assertEqual(len(set(paths)), len(paths), "batch scratch paths collided")

    def test_the_file_is_created_and_private(self):
        """0600, because the default base is a shared world-readable directory
        and each line carries a session's frames and campaign metadata."""
        with TemporaryDirectory() as tmp:
            path = gemini._batch_jsonl_path(Path(tmp))

            self.assertTrue(path.is_file(), "mkstemp should have created the file")
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(
                mode,
                0o600,
                f"batch scratch is {oct(mode)}; group/other must not read it",
            )

    def test_the_caller_can_still_truncate_and_write(self):
        """`mkstemp` creates the file, where the old code returned a name for a
        file that did not exist yet. The submit path opens it with "w", so this
        pins that the pre-created file does not turn into an append or a refusal.
        """
        with TemporaryDirectory() as tmp:
            path = gemini._batch_jsonl_path(Path(tmp))
            path.write_text("first\n")
            with path.open("w") as out:
                out.write("second\n")

            self.assertEqual(path.read_text(), "second\n")

    def test_a_missing_work_dir_is_created(self):
        with TemporaryDirectory() as tmp:
            nested = Path(tmp) / "cohorts" / "scratch"
            path = gemini._batch_jsonl_path(nested)

            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, nested)


if __name__ == "__main__":
    unittest.main()
