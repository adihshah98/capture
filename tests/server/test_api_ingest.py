"""The write side: /ingest, /order, and the sidecar indexes ingest derives."""

import gzip
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from servercase import ServerCase, request


class TestIngestValidation(ServerCase):
    def test_accepts_a_valid_batch(self):
        self.assertEqual(self.ingest("s" * 32), 200)

    def test_rejects_bad_session_id(self):
        self.assertEqual(self.ingest("../etc/passwd"), 400)

    def test_non_object_bodies_are_400_not_500(self):
        for body in (b"[]", b'"hi"', b"7", b"null"):
            with self.subTest(body=body):
                self.assertEqual(self.ingest("t" * 32, raw=body), 400)

    def test_object_without_events_is_rejected(self):
        self.assertEqual(self.ingest("u" * 32, payload={}), 400)

    def test_undecompressable_body_is_rejected(self):
        self.assertEqual(self.ingest("v" * 32, raw=b"not gzip", gzipped=False), 400)


class TestGzipBomb(ServerCase):
    env = {"MAX_DECOMPRESSED_BYTES": str(1024 * 1024)}

    def test_small_payload_still_accepted(self):
        self.assertEqual(self.ingest("w" * 32), 200)

    def test_bomb_is_refused_and_server_survives(self):
        bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
        self.assertLess(len(bomb), 100 * 1024)
        self.assertEqual(self.ingest("x" * 32, raw=bomb, gzipped=False), 400)
        self.assertEqual(request(f"{self.base}/config")[0], 200)


class TestQuota(ServerCase):
    env = {"MAX_PROJECT_BYTES": "1"}

    def test_over_quota_returns_429(self):
        self.ingest("y" * 32)
        self.assertEqual(self.ingest("z" * 32), 429)


class TestDiskFloor(ServerCase):
    env = {"MIN_FREE_DISK_BYTES": str(10**18)}

    def test_low_disk_returns_507(self):
        self.assertEqual(self.ingest("a" * 32), 507)


class TestLimitsDisabled(ServerCase):
    env = {"MAX_PROJECT_BYTES": "0", "MIN_FREE_DISK_BYTES": "0"}

    def test_zero_disables_the_checks(self):
        self.assertEqual(self.ingest("b" * 32), 200)


class TestOrderEvents(ServerCase):
    def test_marks_a_recorded_session_as_purchased(self):
        session = "o" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertEqual(self.order(session, {"event": "checkout_started"}), 200)
        self.assertEqual(
            self.order(
                session,
                {
                    "event": "checkout_completed",
                    "order_id": "1001",
                    "total": 59.5,
                    "currency": "USD",
                },
            ),
            200,
        )
        order = self.listed(session)["order"]
        self.assertEqual(order["order_id"], "1001")
        self.assertEqual(order["total"], 59.5)
        self.assertEqual(order["currency"], "USD")
        self.assertIn("purchased_at", order)
        self.assertIn("checkout_started_at", order)

    def test_marker_never_appears_as_a_chunk(self):
        session = "q" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertEqual(self.order(session, {"event": "checkout_completed"}), 200)
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertEqual(len(manifest["chunks"]), 1)
        self.assertNotIn("order.json", [c["name"] for c in manifest["chunks"]])
        self.assertEqual(self.listed(session)["chunk_count"], 1)

    def test_unrecorded_session_is_404(self):
        self.assertEqual(self.order("r" * 32, {"event": "checkout_completed"}), 404)


class TestVisitorId(ServerCase):
    EVENTS = [{"type": 4, "data": {}}]

    def test_visitor_id_is_stored_and_listed(self):
        session = "v" * 32
        status = self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-abc123"})
        self.assertEqual(status, 200)
        self.assertEqual(self.listed(session)["visitor_id"], "vis-abc123")

    def test_first_visitor_id_wins(self):
        session = "w" * 32
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-first"})
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-second"})
        self.assertEqual(self.listed(session)["visitor_id"], "vis-first")

    def test_invalid_visitor_ids_are_ignored(self):
        for i, bad in enumerate(["../../etc/passwd", "x" * 65, 42, {"nested": True}]):
            session = f"inv{i}" + "x" * 28
            status = self.ingest(session, {"events": self.EVENTS, "visitor_id": bad})
            self.assertEqual(status, 200, f"batch with visitor_id {bad!r} must still ingest")
            self.assertNotIn("visitor_id", self.listed(session))

    def test_marker_never_appears_as_a_chunk(self):
        session = "y" * 32
        self.ingest(session, {"events": self.EVENTS, "visitor_id": "vis-abc123"})
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertEqual(len(manifest["chunks"]), 1)
        self.assertNotIn("visitor.json", [c["name"] for c in manifest["chunks"]])
        self.assertEqual(self.listed(session)["chunk_count"], 1)

    def test_batches_without_visitor_id_still_ingest(self):
        session = "z" * 32
        self.assertEqual(self.ingest(session), 200)
        self.assertNotIn("visitor_id", self.listed(session))

    def test_entry_url_comes_from_the_first_batch(self):
        session = "u" * 32
        self.ingest(
            session,
            {
                "events": self.EVENTS,
                "visitor_id": "vis-u",
                "url": "https://brand.example/landing-a?x=1",
            },
        )
        self.ingest(
            session,
            {"events": self.EVENTS, "visitor_id": "vis-u", "url": "https://brand.example/checkout"},
        )
        listed = self.listed(session)
        self.assertEqual(listed["url"], "https://brand.example/landing-a?x=1")
        self.assertEqual(listed["visitor_id"], "vis-u")

    def test_non_string_url_is_ignored(self):
        session = "t" * 32
        self.assertEqual(self.ingest(session, {"events": self.EVENTS, "url": 42}), 200)
        self.assertNotIn("url", self.listed(session))

    def test_oversized_url_is_truncated(self):
        session = "s2" + "x" * 30
        self.ingest(session, {"events": self.EVENTS, "url": "https://brand.example/" + "a" * 3000})
        self.assertEqual(len(self.listed(session)["url"]), 2048)


class TestInteractions(ServerCase):
    def click(self, payload):
        return {"type": 5, "data": {"tag": "capture-click", "payload": payload}}

    def test_clicks_are_counted_across_batches(self):
        session = "click" + "x" * 27
        self.ingest(
            session,
            {
                "events": [
                    self.click({"selector": "button#buy", "text": "Buy now"}),
                    self.click({"selector": "button#buy", "rage": True}),
                    {"type": 3, "data": {"source": 2}},
                ]
            },
        )
        self.ingest(session, {"events": [self.click({"selector": "a.nav"})]})
        self.assertEqual(self.listed(session)["interactions"], {"clicks": 3, "rage": 1})

    def test_sidecar_never_appears_as_a_chunk(self):
        session = "click2" + "x" * 26
        self.ingest(session, {"events": [self.click({"selector": "button#buy"})]})
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertNotIn("interactions.json", [c["name"] for c in manifest["chunks"]])

    def test_sessions_without_clicks_have_no_interactions(self):
        session = "click3" + "x" * 26
        self.ingest(session, {"events": [{"type": 3, "data": {"source": 2}}]})
        self.assertNotIn("interactions", self.listed(session))

    def test_malformed_click_events_never_break_ingest(self):
        session = "click4" + "x" * 26
        status = self.ingest(
            session,
            {
                "events": [
                    {"type": 5, "data": {"tag": "capture-click", "payload": "junk"}},
                    {"type": 5, "data": "junk"},
                    {"type": 5},
                ]
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.listed(session)["interactions"], {"clicks": 1, "rage": 0})


class TestDeviceIndex(ServerCase):
    def test_first_meta_event_sets_device(self):
        session = "dev1" + "x" * 28
        self.ingest(
            session,
            {
                "events": [
                    {
                        "type": 4,
                        "data": {"href": "https://brand.example/", "width": 390, "height": 844},
                    },
                    {
                        "type": 4,
                        "data": {"href": "https://brand.example/b", "width": 390, "height": 677},
                    },
                ]
            },
        )
        self.assertEqual(self.listed(session)["device"], {"width": 390, "height": 844})

    def test_device_survives_later_batches_and_never_lists_as_chunk(self):
        session = "dev2" + "x" * 28
        self.ingest(session, {"events": [{"type": 4, "data": {"width": 1496, "height": 900}}]})
        self.ingest(session, {"events": [{"type": 4, "data": {"width": 390, "height": 844}}]})
        self.assertEqual(self.listed(session)["device"]["width"], 1496)
        manifest = json.loads(request(f"{self.base}/api/sessions/{session}/chunks")[1])
        self.assertNotIn("device.json", [c["name"] for c in manifest["chunks"]])

    def test_malformed_meta_events_never_break_ingest(self):
        session = "dev3" + "x" * 28
        status = self.ingest(
            session,
            {
                "events": [
                    {"type": 4, "data": "junk"},
                    {"type": 4, "data": {"width": "wide", "height": 10}},
                    {"type": 4},
                    {"type": 3, "data": {"source": 2}},
                ]
            },
        )
        self.assertEqual(status, 200)
        self.assertNotIn("device", self.listed(session))


class TestAttribution(ServerCase):
    EVENTS = [{"type": 4, "data": {}}]

    def test_utm_and_fbclid_indexed_at_ingest(self):
        sid = "utm" + "x" * 29
        url = (
            "https://brand.example/landing"
            "?utm_source=facebook&utm_medium=paid&utm_campaign=spring_sale"
            "&utm_content=creative_a&fbclid=IwAR123&page=2"
        )
        self.assertEqual(
            self.ingest(sid, {"events": self.EVENTS, "url": url}),
            200,
        )
        listed = self.listed(sid)
        self.assertEqual(
            listed["attribution"],
            {
                "utm_source": "facebook",
                "utm_medium": "paid",
                "utm_campaign": "spring_sale",
                "utm_content": "creative_a",
                "fbclid": True,
            },
        )

    def test_plain_url_has_no_attribution(self):
        sid = "plain" + "x" * 27
        self.assertEqual(
            self.ingest(sid, {"events": self.EVENTS, "url": "https://brand.example/p/1"}),
            200,
        )
        self.assertNotIn("attribution", self.listed(sid))

    def test_parse_attribution_helpers(self):
        # Lives in capture/attribution.py now: it is a pure parser both the web
        # layer and the analysis package need, so it belongs to neither.
        from capture.attribution import parse_attribution

        self.assertEqual(
            parse_attribution("https://x.test/?utm_campaign=fall&fbclid=abc&utm_source=meta"),
            {"utm_campaign": "fall", "fbclid": True, "utm_source": "meta"},
        )
        self.assertIsNone(parse_attribution("https://x.test/path"))
        self.assertIsNone(parse_attribution(None))


if __name__ == "__main__":
    unittest.main()
