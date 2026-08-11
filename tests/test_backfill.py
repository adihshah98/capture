"""Backfill: on-disk pools become project-scoped rows, and running it twice
changes nothing."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture
import psycopg


def chunk(ts: int, window: str = "aaaa1111", suffix: str = "abc123") -> str:
    return f"{ts}-{window}-{suffix}.json"


def write_session(pool: Path, session_id: str, *, url=None, visitor=None,
                  order=None, clicks=0, rage=0, width=None):
    d = pool / session_id
    d.mkdir(parents=True, exist_ok=True)
    events = [{"type": 3, "data": {"source": 2}, "timestamp": 1000}]
    if width:
        events.append({"type": 4, "data": {"width": width, "height": 800}, "timestamp": 1000})
    for _ in range(clicks):
        events.append(
            {"type": 5, "timestamp": 1500,
             "data": {"tag": "capture-click", "payload": {"rage": rage > 0}}}
        )
    (d / chunk(1785700000000)).write_text(
        json.dumps({"window_id": "aaaa1111", "url": url, "received_at": 1785700000000,
                    "events": events})
    )
    if visitor or url:
        (d / "visitor.json").write_text(json.dumps(
            {k: v for k, v in (("visitor_id", visitor), ("url", url)) if v}
        ))
    if order:
        (d / "order.json").write_text(json.dumps(order))
    if clicks:
        (d / "interactions.json").write_text(json.dumps({"clicks": clicks, "rage": rage}))
    if width:
        (d / "device.json").write_text(json.dumps({"width": width, "height": 800}))
    (d / "window-state.json").write_text(json.dumps(
        {"aaaa1111": {"first": 1000, "last": 2000, "active": 1500, "bytes": 512}}
    ))


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class TestBackfill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-backfill-"))
        legacy = cls.data_dir / "sessions"
        write_session(legacy, "legacy-one", url="https://shop.test/?utm_source=meta",
                      visitor="v1", clicks=3, rage=1, width=390)
        write_session(legacy, "legacy-two",
                      order={"checkout_started_at": 10, "purchased_at": 20,
                             "order_id": "#1001", "total": 49.5, "currency": "USD"})
        tenant = cls.data_dir / "projects" / "cap_tenant" / "sessions"
        write_session(tenant, "tenant-one", url="https://brand.test/")
        (cls.data_dir / "projects.json").write_text(json.dumps(
            {"cap_tenant": {"name": "Brand", "org_id": "org_42", "domains": ["brand.test"]}}
        ))

        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        cls.run_backfill()

    @classmethod
    def run_backfill(cls):
        """Run the shipped CLI in its own process. Importing it here instead
        would leak CAPTURE_DATA_DIR and a re-imported server module into every
        test that runs afterwards."""
        repo = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [str(repo / ".venv" / "bin" / "python"), "-m", "capture.backfill"],
            cwd=repo,
            env={
                **os.environ,
                "CAPTURE_DATA_DIR": str(cls.data_dir),
                "CAPTURE_DATABASE_URL": cls.app_url,
                "CAPTURE_DATABASE_ADMIN_URL": cls.admin_url,
            },
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(f"backfill failed: {result.stdout}\n{result.stderr}")
        return result.stdout

    def rows(self, sql, params=()):
        with psycopg.connect(self.admin_url) as conn:
            return conn.execute(sql, params).fetchall()

    def test_every_pool_becomes_a_project(self):
        self.assertEqual(self.rows("SELECT count(*) FROM sessions")[0][0], 3)
        names = sorted(r[0] for r in self.rows("SELECT name FROM projects"))
        self.assertEqual(names, ["Brand", "Default"])

    def test_the_legacy_pool_is_the_default_project(self):
        rows = self.rows("SELECT name FROM projects WHERE is_default")
        self.assertEqual([r[0] for r in rows], ["Default"])

    def test_storage_prefix_locates_each_pool_on_disk(self):
        rows = dict(self.rows("SELECT name, storage_prefix FROM projects"))
        self.assertEqual(rows, {"Default": "", "Brand": "cap_tenant"})

    def test_the_tenant_token_is_recorded_separately_from_identity(self):
        rows = self.rows(
            "SELECT t.token, p.name, p.org_id FROM project_tokens t"
            " JOIN projects p ON p.id = t.project_id"
        )
        self.assertEqual(rows, [("cap_tenant", "Brand", "org_42")])

    def test_no_session_is_left_without_a_project(self):
        orphans = self.rows("SELECT count(*) FROM sessions WHERE project_id IS NULL")
        self.assertEqual(orphans[0][0], 0)

    def test_sessions_land_in_the_right_project(self):
        rows = self.rows(
            "SELECT p.name, s.session_id FROM sessions s"
            " JOIN projects p ON p.id = s.project_id ORDER BY s.session_id"
        )
        self.assertEqual(
            rows,
            [("Default", "legacy-one"), ("Default", "legacy-two"), ("Brand", "tenant-one")],
        )

    def test_interaction_counters_carry_over(self):
        rows = self.rows("SELECT clicks, rage FROM sessions WHERE session_id = 'legacy-one'")
        self.assertEqual(rows, [(3, 1)])

    def test_attribution_is_parsed_from_the_entry_url(self):
        rows = self.rows("SELECT attribution FROM sessions WHERE session_id = 'legacy-one'")
        self.assertEqual(rows[0][0], {"utm_source": "meta"})

    def test_orders_become_their_own_row(self):
        rows = self.rows(
            "SELECT purchased_at, order_id, total, currency FROM orders"
            " JOIN sessions USING (project_id, session_id) WHERE session_id = 'legacy-two'"
        )
        self.assertEqual(rows, [(20, "#1001", 49.5, "USD")])

    def test_window_state_becomes_rows(self):
        rows = self.rows(
            "SELECT window_id, first_seen, last_seen, bytes FROM session_windows"
            " WHERE session_id = 'legacy-one'"
        )
        self.assertEqual(rows, [("aaaa1111", 1000, 2000, 512)])

    def test_running_it_again_changes_nothing(self):
        before = self.rows(
            "SELECT project_id, session_id, chunk_count, bytes, clicks FROM sessions"
            " ORDER BY session_id"
        )
        projects_before = self.rows("SELECT count(*) FROM projects")[0][0]
        tokens_before = self.rows("SELECT count(*) FROM project_tokens")[0][0]
        self.run_backfill()
        after = self.rows(
            "SELECT project_id, session_id, chunk_count, bytes, clicks FROM sessions"
            " ORDER BY session_id"
        )
        self.assertEqual(before, after)
        self.assertEqual(self.rows("SELECT count(*) FROM projects")[0][0], projects_before)
        self.assertEqual(self.rows("SELECT count(*) FROM project_tokens")[0][0], tokens_before)


LEGACY_INDEX_DDL = """
CREATE TABLE sessions (
    pool TEXT NOT NULL, session_id TEXT NOT NULL,
    first_seen INTEGER, last_seen INTEGER, chunk_count INTEGER, bytes INTEGER,
    visitor_id TEXT, url TEXT, "order" TEXT, interactions TEXT, device TEXT,
    attribution TEXT, last_active INTEGER, duration INTEGER, windows INTEGER,
    PRIMARY KEY (pool, session_id)
)
"""


@unittest.skipUnless(pgfixture.available(), pgfixture.UNAVAILABLE)
class TestBackfillPrefersTheIndex(unittest.TestCase):
    """At production size the chunk files are tens of GB, so re-deriving the
    index from them is an hour of parsing. index.db already has every column."""

    @classmethod
    def setUpClass(cls):
        import sqlite3

        cls.data_dir = Path(tempfile.mkdtemp(prefix="capture-idx-"))
        pool = cls.data_dir / "projects" / "cap_indexed" / "sessions"
        (cls.data_dir / "projects.json").write_text(
            json.dumps({"cap_indexed": {"name": "Indexed"}})
        )

        # A chunk that cannot be parsed. If the backfill reads the index it will
        # never look at this; if it falls back to parsing, the row comes out empty.
        d = pool / "indexed-session"
        d.mkdir(parents=True)
        (d / chunk(1785700000000)).write_text("{ truncated garbage")
        (d / "window-state.json").write_text(json.dumps(
            {"w1": {"first": 500, "last": 9500, "active": 9000, "bytes": 4242}}
        ))

        index = sqlite3.connect(cls.data_dir / "index.db")
        index.execute(LEGACY_INDEX_DDL)
        index.execute(
            'INSERT INTO sessions (pool, session_id, first_seen, last_seen, chunk_count,'
            ' bytes, visitor_id, url, "order", interactions, device, attribution,'
            " last_active, duration, windows)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cap_indexed", "indexed-session", 500, 9500, 7, 123456, "v-idx",
                "https://shop.test/?utm_source=meta",
                json.dumps({"purchased_at": 9000, "total": 12.5, "currency": "GBP"}),
                json.dumps({"clicks": 11, "rage": 3}),
                json.dumps({"width": 414, "height": 896}),
                json.dumps({"utm_source": "meta"}),
                9000, 8800, 2,
            ),
        )
        index.commit()
        index.close()

        cls.admin_url, cls.app_url = pgfixture.fresh_database()
        TestBackfill.run_backfill.__func__(cls)

    def rows(self, sql, params=()):
        with psycopg.connect(self.admin_url) as conn:
            return conn.execute(sql, params).fetchall()

    def test_the_row_comes_from_the_index_not_the_unparseable_chunk(self):
        row = self.rows(
            "SELECT first_seen, last_seen, chunk_count, bytes, clicks, rage, visitor_id,"
            " windows, duration FROM sessions WHERE session_id = 'indexed-session'"
        )
        self.assertEqual(row, [(500, 9500, 7, 123456, 11, 3, "v-idx", 2, 8800)])

    def test_json_columns_survive_the_move(self):
        device, attribution = self.rows(
            "SELECT device, attribution FROM sessions WHERE session_id = 'indexed-session'"
        )[0]
        self.assertEqual(device, {"width": 414, "height": 896})
        self.assertEqual(attribution, {"utm_source": "meta"})

    def test_the_order_is_split_into_its_own_row(self):
        self.assertEqual(
            self.rows("SELECT purchased_at, total, currency FROM orders"),
            [(9000, 12.5, "GBP")],
        )

    def test_window_rows_come_from_the_sidecar(self):
        self.assertEqual(
            self.rows(
                "SELECT window_id, first_seen, last_seen, bytes FROM session_windows"
            ),
            [("w1", 500, 9500, 4242)],
        )

    def test_bytes_used_is_recomputed_on_the_project(self):
        self.assertEqual(
            self.rows("SELECT bytes_used FROM projects WHERE storage_prefix = 'cap_indexed'"),
            [(123456,)],
        )

    def test_rerunning_is_still_idempotent(self):
        before = self.rows("SELECT session_id, bytes, clicks FROM sessions ORDER BY session_id")
        TestBackfill.run_backfill.__func__(type(self))
        after = self.rows("SELECT session_id, bytes, clicks FROM sessions ORDER BY session_id")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
