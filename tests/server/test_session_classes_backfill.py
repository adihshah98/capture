"""One-shot copy of JSON / orders into the class columns 0007 added."""

import json
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from servercase import REPO, ServerCase

BACKFILL = (REPO / "migrations" / "0008_backfill_session_classes.sql").read_text()


class TestSessionClassesBackfill(ServerCase):
    async def test_backfill_copies_json_and_orders_into_class_columns(self):
        sid = "legbf" + "x" * 27
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    project_id, session_id, first_seen, last_seen, last_active,
                    chunk_count, bytes, url, device, attribution, outcome
                ) VALUES (
                    %s, %s, 0, 20000, 0, 1, 1, %s, %s::jsonb, %s::jsonb, 'rest'
                )
                """,
                (
                    self.project_id,
                    sid,
                    "https://shop.test/p?utm_campaign=Spring&utm_source=google",
                    json.dumps({"width": 375}),
                    json.dumps({"utm_source": "google", "utm_campaign": "Spring"}),
                ),
            )
            conn.execute(
                "INSERT INTO orders (project_id, session_id, purchased_at) VALUES (%s, %s, 1)",
                (self.project_id, sid),
            )
            conn.execute(BACKFILL)
            conn.commit()

        from capture import db

        async with db.connection(self.project_id) as conn:
            row = await db.fetchone(
                conn,
                "SELECT outcome, device_class, page, utm_source, utm_campaign,"
                " has_fbclid, is_meta FROM sessions WHERE session_id = %s",
                (sid,),
            )
        self.assertEqual(
            row,
            ("purchased", "mobile", "/p", "google", "spring", False, False),
        )

    async def test_backfill_does_not_overwrite_ingest_columns(self):
        sid = "keepbf" + "x" * 26
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    project_id, session_id, first_seen, last_seen, last_active,
                    chunk_count, bytes, url, attribution, outcome,
                    device_class, page, utm_campaign
                ) VALUES (
                    %s, %s, 0, 20000, 0, 1, 1, %s, %s::jsonb, 'rest',
                    'desktop', '/current', 'fresh'
                )
                """,
                (
                    self.project_id,
                    sid,
                    "https://shop.test/old?utm_campaign=stale",
                    json.dumps({"utm_campaign": "stale"}),
                ),
            )
            conn.execute(BACKFILL)
            conn.commit()

        from capture import db

        async with db.connection(self.project_id) as conn:
            row = await db.fetchone(
                conn,
                "SELECT device_class, page, utm_campaign FROM sessions" " WHERE session_id = %s",
                (sid,),
            )
        self.assertEqual(row, ("desktop", "/current", "fresh"))

    async def test_backfill_keeps_is_meta_false_when_there_is_no_campaign(self):
        """A row with no attribution JSON: `utm_source IN (...)` is SQL NULL.

        OR-ing that onto is_meta used to write NULL and abort the migration.
        """
        sid = "noutil" + "x" * 26
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    project_id, session_id, first_seen, last_seen, last_active,
                    chunk_count, bytes, url, device, attribution, outcome,
                    device_class, page, has_fbclid, is_meta
                ) VALUES (
                    %s, %s, 0, 20000, 0, 1, 1, %s, %s::jsonb, NULL, 'rest',
                    'desktop', '/products/ashwa-mag-1', false, false
                )
                """,
                (
                    self.project_id,
                    sid,
                    "https://rootlabs.co/products/ashwa-mag-1?srsltid=abc",
                    json.dumps({"width": 820, "height": 1090}),
                ),
            )
            conn.execute(BACKFILL)
            conn.commit()

        from capture import db

        async with db.connection(self.project_id) as conn:
            row = await db.fetchone(
                conn,
                "SELECT is_meta, has_fbclid, page FROM sessions WHERE session_id = %s",
                (sid,),
            )
        self.assertEqual(row, (False, False, "/products/ashwa-mag-1"))

    async def test_backfill_keeps_distinct_landing_pages(self):
        """0008's regexp must not collapse two product URLs into `/`."""
        a = "pagea" + "x" * 27
        b = "pageb" + "x" * 27
        with psycopg.connect(self.admin_url) as conn:
            for sid, url in (
                (a, "https://rootlabs.co/products/ashwa-mag-1?srsltid=abc"),
                (b, "https://rootlabs.co/pages/ashwamag-01?utm_source=meta"),
            ):
                conn.execute(
                    """
                    INSERT INTO sessions (
                        project_id, session_id, first_seen, last_seen, last_active,
                        chunk_count, bytes, url, outcome
                    ) VALUES (%s, %s, 0, 20000, 0, 1, 1, %s, 'rest')
                    """,
                    (self.project_id, sid, url),
                )
            conn.execute(BACKFILL)
            conn.commit()

        from capture import db

        async with db.connection(self.project_id) as conn:
            pages = {
                row[0]: row[1]
                for row in await db.fetchall(
                    conn,
                    "SELECT session_id, page FROM sessions WHERE session_id IN (%s, %s)",
                    (a, b),
                )
            }
        self.assertEqual(pages[a], "/products/ashwa-mag-1")
        self.assertEqual(pages[b], "/pages/ashwamag-01")

    async def test_recompute_page_overwrites_the_homepage_collapse(self):
        """0008 stored `/` for unreadable URLs; 0009 uses page_path instead."""
        sid = "collap" + "x" * 26
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    project_id, session_id, first_seen, last_seen, last_active,
                    chunk_count, bytes, url, outcome, page
                ) VALUES (
                    %s, %s, 0, 20000, 0, 1, 1, %s, 'rest', '/'
                )
                """,
                (
                    self.project_id,
                    sid,
                    "https://rootlabs.co/collections/all",
                ),
            )
            conn.commit()

        from capture import db

        with psycopg.connect(self.admin_url) as conn:
            db.recompute_session_pages(conn)
            conn.commit()

        async with db.connection(self.project_id) as conn:
            page = (
                await db.fetchone(
                    conn,
                    "SELECT page FROM sessions WHERE session_id = %s",
                    (sid,),
                )
            )[0]
        self.assertEqual(page, "/collections/all")
