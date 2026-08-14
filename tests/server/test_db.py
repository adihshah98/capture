"""Migration file selection, with nothing else attached.

macOS tar packs AppleDouble forks as ._0001_init.sql next to the real file.
glob('*.sql') matches both; the fork is binary and sorts first, so migrate()
used to UnicodeDecodeError before applying anything.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from capture import db


class TestMigrationFiles(unittest.TestCase):
    def test_appledouble_forks_are_not_migrations(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "0001_init.sql").write_text("-- real\n")
            (d / "0002_next.sql").write_text("-- also real\n")
            (d / "._0001_init.sql").write_bytes(bytes(range(163)))
            with mock.patch.object(db, "MIGRATIONS_DIR", d):
                names = [p.name for p in db.migration_files()]
        self.assertEqual(names, ["0001_init.sql", "0002_next.sql"])

    def test_session_class_backfill_is_shipped(self):
        names = [p.name for p in db.migration_files()]
        self.assertIn("0008_backfill_session_classes.sql", names)
        self.assertIn("0009_recompute_page.sql", names)


if __name__ == "__main__":
    unittest.main()
