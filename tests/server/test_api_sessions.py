"""The read side: listing, paging, filtering, deletion, and the index behind them."""

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from servercase import ServerCase, request


class TestDelete(ServerCase):
    def test_delete_removes_only_the_named_session(self):
        keep, drop = "k" * 32, "d" * 32
        self.assertEqual(self.ingest(keep), 200)
        self.assertEqual(self.ingest(drop), 200)

        status, body = request(f"{self.base}/api/sessions/{drop}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["session_id"], drop)

        listed = [
            s["session_id"] for s in json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
        ]
        self.assertIn(keep, listed)
        self.assertNotIn(drop, listed)

    def test_deleting_twice_is_404(self):
        session = "e" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertEqual(request(f"{self.base}/api/sessions/{session}", method="DELETE")[0], 200)
        self.assertEqual(request(f"{self.base}/api/sessions/{session}", method="DELETE")[0], 404)

    def test_unknown_project_is_404(self):
        self.assertEqual(
            request(f"{self.base}/api/sessions/{'f' * 32}?project=cap_nope", method="DELETE")[0],
            404,
        )


class TestSessionPaging(ServerCase):
    def list(self, query=""):
        status, body = request(f"{self.base}/api/sessions{query}")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_pages_walk_every_session_exactly_once(self):
        ids = [f"pg{i}" + "x" * 28 for i in range(5)]
        for sid in ids:
            self.assertEqual(self.ingest(sid), 200)

        body = self.list()
        self.assertEqual(body["total"], 5)
        self.assertIsNone(body["cursor"])
        self.assertEqual(len(body["sessions"]), 5)

        body = self.list("?limit=2")
        self.assertEqual(body["total"], 5)
        seen = [s["session_id"] for s in body["sessions"]]
        self.assertEqual(len(seen), 2)
        while body["cursor"]:
            body = self.list(f"?limit=2&cursor={body['cursor']}")
            seen += [s["session_id"] for s in body["sessions"]]
        self.assertEqual(sorted(seen), sorted(ids))
        last_seen = [s["last_seen"] for s in self.list()["sessions"]]
        self.assertEqual(last_seen, sorted(last_seen, reverse=True))

    def test_single_session_endpoint_serves_deep_links(self):
        sid = "deep" + "x" * 28
        self.assertEqual(self.ingest(sid), 200)
        status, body = request(f"{self.base}/api/sessions/{sid}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["session_id"], sid)
        self.assertEqual(request(f"{self.base}/api/sessions/{'gone' + 'x' * 28}")[0], 404)


class TestSessionFilters(ServerCase):
    EVENTS = [{"type": 4, "data": {}}]

    def listed_ids(self, query):
        status, body = request(f"{self.base}/api/sessions{query}")
        self.assertEqual(status, 200)
        return {s["session_id"] for s in json.loads(body)["sessions"]}

    def test_filters_and_search_run_serverside(self):
        plain = "plain" + "x" * 27
        started = "start" + "x" * 27
        bought = "buy" + "x" * 29
        ragey = "rage" + "x" * 28
        self.ingest(
            plain, {"events": self.EVENTS, "url": "https://brand.example/landing-a?utm=news"}
        )
        self.ingest(started)
        self.assertEqual(self.order(started, {"event": "checkout_started"}), 200)
        self.ingest(bought)
        self.assertEqual(self.order(bought, {"event": "checkout_completed"}), 200)
        self.ingest(
            ragey,
            {
                "events": [
                    *self.EVENTS,
                    {"type": 5, "data": {"tag": "capture-click", "payload": {"rage": True}}},
                ]
            },
        )

        self.assertEqual(self.listed_ids("?filter=checkout"), {started, bought})
        self.assertEqual(self.listed_ids("?filter=purchased"), {bought})
        self.assertEqual(self.listed_ids("?filter=rage"), {ragey})
        self.assertEqual(self.listed_ids("?q=landing-a"), {plain})
        self.assertEqual(self.listed_ids("?q=utm%3Dnews"), {plain})
        self.assertEqual(self.listed_ids("?q=" + started[:12]), {started})
        self.assertEqual(self.listed_ids("?q=nothing-matches"), set())


class TestIndexRebuild(ServerCase):
    def test_index_rebuilds_from_disk_on_startup(self):
        ids = [f"reb{i}" + "x" * 28 for i in range(3)]
        for sid in ids:
            self.assertEqual(self.ingest(sid), 200)

        self.proc.terminate()
        self.proc.wait(timeout=10)
        for name in ("index.db", "index.db-wal", "index.db-shm"):
            (self.data_dir / name).unlink(missing_ok=True)
        type(self).start_server()

        deadline = time.time() + 10
        listed = set()
        while time.time() < deadline:
            listed = {
                s["session_id"]
                for s in json.loads(request(f"{self.base}/api/sessions")[1])["sessions"]
            }
            if listed == set(ids):
                break
            time.sleep(0.2)
        self.assertEqual(listed, set(ids))


if __name__ == "__main__":
    unittest.main()
