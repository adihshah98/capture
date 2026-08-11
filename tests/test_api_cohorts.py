"""Cohorts over HTTP: sampling, listing, rename, delete, retry, and migration."""

import json
import sys
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from servercase import ServerCase, request

from capture import analyze
from capture.analyze import store as analyze_store


class TestCohort(ServerCase):
    # Relaxed sampling defaults for fixtures; Batch off so cached write-ups settle sync.
    env = {
        "GEMINI_API_KEY": "test-key-not-used-when-cached",
        "ANALYZE_BATCH": "0",
    }

    def seed_analysis(self, sid):
        analyze.write_analysis(
            self.data_dir / "sessions" / sid,
            {
                "text": f"write-up for {sid[:8]}",
                "model": "test",
                "frame_count": 1,
                "warnings": [],
            },
        )

    def create_cohort_url(self, purchased=1, all_n=0, **extra):
        """POST URL that disables production ashwamag/≥5s filters for test fixtures."""
        params = {
            "purchased": purchased,
            "all": all_n,
            "min_duration_ms": 0,
            "url_contains": "",
        }
        params.update(extra)
        qs = urllib.parse.urlencode(params)
        return f"{self.base}/api/cohorts?{qs}"

    def analyze_cohort_url(self, cohort_id, **extra):
        qs = urllib.parse.urlencode(extra) if extra else ""
        return f"{self.base}/api/cohorts/{cohort_id}/analyze" + (f"?{qs}" if qs else "")

    def create_and_analyze(self, purchased=1, all_n=0, **extra):
        status, body = request(
            self.create_cohort_url(purchased=purchased, all_n=all_n, **extra),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        # Idle until Start; already-cached write-ups may enrich straight to done.
        self.assertIn(cohort["status"], ("idle", "done"))
        self.assertNotEqual(cohort["status"], "running")
        self.assertTrue(cohort.get("selection"))
        status, body = request(self.analyze_cohort_url(cohort["id"]), method="POST", data=b"")
        self.assertEqual(status, 202, body)
        return json.loads(body)

    def cohorts(self):
        status, body = request(f"{self.base}/api/cohorts")
        self.assertEqual(status, 200)
        return json.loads(body)["cohorts"]

    def wait_done(self, cohort_id, expect_done=None):
        deadline = time.time() + 10
        cohort = None
        while time.time() < deadline:
            status, body = request(f"{self.base}/api/cohorts/{cohort_id}")
            self.assertEqual(status, 200)
            cohort = json.loads(body)
            if cohort["status"] == "done" and (
                expect_done is None or cohort["progress"]["done"] == expect_done
            ):
                return cohort
            time.sleep(0.1)
        return cohort

    def test_get_without_cohort_is_404(self):
        status, body = request(f"{self.base}/api/cohorts/cohort-does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("no such cohort", json.loads(body)["error"])

    def test_list_reports_every_cohort_with_its_progress(self):
        sid = "listed" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        listed = {c["id"]: c for c in self.cohorts()}
        self.assertIn(cohort_id, listed)
        row = listed[cohort_id]
        self.assertEqual(row["status"], "done")
        self.assertTrue(row["label"])
        # The listing's counts agree with the cohort's own detail view.
        detail = json.loads(request(f"{self.base}/api/cohorts/{cohort_id}")[1])
        self.assertEqual(row["progress"], detail["progress"])
        self.assertEqual(row["progress"]["done"], len(detail["sessions"]))

    def test_samples_purchased_and_all_and_reuses_cached_analysis(self):
        purchased = [f"buy{i}" + "x" * 28 for i in range(3)]
        plain = [f"all{i}" + "x" * 28 for i in range(4)]
        for sid in purchased:
            self.assertEqual(self.ingest(sid), 200)
            self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)
        for sid in plain:
            self.assertEqual(self.ingest(sid), 200)
            self.seed_analysis(sid)

        cohort = self.create_and_analyze(purchased=2, all_n=2)
        self.assertEqual(cohort["purchased_n"], 2)
        self.assertEqual(cohort["all_n"], 2)
        self.assertEqual(len(cohort["sessions"]), 4)
        self.assertIn("purchased", cohort.get("selection", ""))
        filters = {s["filter"] for s in cohort["sessions"]}
        self.assertEqual(filters, {"purchased", "all"})
        purchased_ids = {s["session_id"] for s in cohort["sessions"] if s["filter"] == "purchased"}
        all_ids = {s["session_id"] for s in cohort["sessions"] if s["filter"] == "all"}
        self.assertTrue(purchased_ids <= set(purchased))
        self.assertTrue(all_ids <= set(plain))
        self.assertFalse(purchased_ids & all_ids)

        cohort = self.wait_done(cohort["id"], expect_done=4)
        self.assertEqual(cohort["status"], "done")
        self.assertEqual(cohort["progress"]["done"], 4)
        self.assertEqual(cohort["progress"]["error"], 0)
        for entry in cohort["sessions"]:
            self.assertEqual(entry["status"], "done")
            self.assertEqual(entry["analysis"], "analysis.json")
            self.assertTrue(entry.get("text", "").startswith("write-up for"))
            self.assertIn("standout", entry)

        saved = self.stored_cohort(cohort["id"])
        self.assertEqual(saved["id"], cohort["id"])
        self.assertEqual(len(saved["sessions"]), 4)
        # Write-ups are response-only — not bloating the cohort file on disk.
        for entry in saved["sessions"]:
            self.assertNotIn("text", entry)

    def test_rejects_cohorts_over_500_and_supports_any_sample(self):
        for i in range(3):
            sid = f"any{i}" + "x" * 28
            self.assertEqual(self.ingest(sid), 200)
            if i == 0:
                self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)

        status, body = request(
            self.create_cohort_url(purchased=300, all_n=250),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 400, body)
        self.assertIn("500", json.loads(body)["error"])

        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode({"any": 2, "min_duration_ms": 0, "url_contains": ""}),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(cohort["any_n"], 2)
        self.assertEqual(len(cohort["sessions"]), 2)
        self.assertIn("matching", cohort.get("selection", ""))

    def test_pool_facets_and_slice_filtered_create(self):
        mobile = "facetm" + "x" * 26
        desktop = "facetd" + "x" * 26
        nonbuy = "facetn" + "x" * 26
        self.assertEqual(
            self.ingest(
                mobile,
                {
                    "events": [
                        {"type": 4, "data": {"width": 390, "height": 844}},
                    ],
                    "url": "https://brand.example/pages/ashwamag-01?utm_source=google",
                },
            ),
            200,
        )
        self.assertEqual(self.order(mobile, {"event": "checkout_completed"}), 200)
        self.assertEqual(
            self.ingest(
                desktop,
                {
                    "events": [
                        {"type": 4, "data": {"width": 1280, "height": 800}},
                    ],
                    "url": "https://brand.example/pages/ashwamag-01?utm_source=google",
                },
            ),
            200,
        )
        self.assertEqual(self.order(desktop, {"event": "checkout_completed"}), 200)
        self.assertEqual(
            self.ingest(
                nonbuy,
                {
                    "events": [
                        {"type": 4, "data": {"width": 390, "height": 844}},
                    ],
                    "url": "https://brand.example/pages/ashwamag-01?utm_source=google",
                },
            ),
            200,
        )
        self.seed_analysis(mobile)
        self.seed_analysis(desktop)
        self.seed_analysis(nonbuy)

        status, body = request(
            f"{self.base}/api/facets?" + urllib.parse.urlencode({"min_duration_ms": 0})
        )
        self.assertEqual(status, 200, body)
        facets = json.loads(body)
        self.assertGreaterEqual(facets["n"], 2)
        self.assertTrue(any(i["value"] == "mobile" for i in facets.get("device") or []))
        self.assertTrue(any(i["value"] == "/pages/ashwamag-01" for i in facets.get("page") or []))

        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode(
                {
                    "any": 5,
                    "min_duration_ms": 0,
                    "url_contains": "",
                    "device": "mobile",
                    "utm_source": "google",
                    "page": "/pages/ashwamag-01",
                }
            ),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(
            {s["session_id"] for s in cohort["sessions"]},
            {mobile, nonbuy},
        )
        self.assertEqual(cohort.get("slice", {}).get("device"), ["mobile"])

        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode(
                {
                    "a": 1,
                    "b": 1,
                    "min_duration_ms": 0,
                    "url_contains": "",
                    "outcome": "purchased",
                    "device": "mobile",
                    "page": "/pages/ashwamag-01",
                    "utm_source": "google",
                    "b_outcome": "all",
                    "b_device": "mobile",
                    "b_page": "/pages/ashwamag-01",
                    "b_utm_source": "google",
                }
            ),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(cohort["a_n"], 1)
        self.assertEqual(cohort["b_n"], 1)
        by_filter = {s["filter"]: s["session_id"] for s in cohort["sessions"]}
        self.assertEqual(by_filter.get("a"), mobile)
        self.assertEqual(by_filter.get("b"), nonbuy)

    def test_create_cohort_accepts_a_custom_label(self):
        sid = "named" + "x" * 27
        self.assertEqual(self.ingest(sid), 200)
        self.seed_analysis(sid)
        status, body = request(
            f"{self.base}/api/cohorts?"
            + urllib.parse.urlencode(
                {
                    "a": 1,
                    "min_duration_ms": 0,
                    "url_contains": "",
                    "label": "  Checkout check  ",
                }
            ),
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)
        cohort = json.loads(body)
        self.assertEqual(cohort["label"], "Checkout check")
        self.assertEqual(len(cohort["sessions"]), 1)

    def test_cohorts_accumulate_rather_than_overwrite(self):
        for i in range(2):
            sid = f"acc{i}" + "x" * 28
            self.assertEqual(self.ingest(sid), 200)
            self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)

        ids = []
        for _ in range(2):
            cohort = self.create_and_analyze(purchased=1, all_n=0)
            cohort_id = cohort["id"]
            ids.append(cohort_id)
            self.wait_done(cohort_id)
            # Ids embed a millisecond stamp; keep the two runs distinguishable.
            time.sleep(0.002)

        # Two runs, two distinct cohorts — the second did not overwrite the first.
        self.assertEqual(len(set(ids)), 2)
        listed = [c["id"] for c in self.cohorts()]
        for cohort_id in ids:
            self.assertIn(cohort_id, listed)
        # Newest first, so the later run sorts ahead of the earlier one.
        self.assertLess(listed.index(ids[1]), listed.index(ids[0]))
        # First cohort still exists with its membership.
        first = self.stored_cohort(ids[0])
        self.assertEqual(len(first["sessions"]), 1)

    def test_rename_sets_the_label_and_keeps_membership(self):
        sid = "rename" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        before = json.loads(request(f"{self.base}/api/cohorts/{cohort_id}")[1])

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}",
            method="PATCH",
            data=json.dumps({"label": "  Spring campaign check  "}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, body)
        renamed = json.loads(body)
        self.assertEqual(renamed["label"], "Spring campaign check")
        self.assertEqual(
            [s["session_id"] for s in renamed["sessions"]],
            [s["session_id"] for s in before["sessions"]],
        )
        self.assertEqual(self.cohorts()[0]["label"], "Spring campaign check")

    def test_rename_rejects_an_empty_label(self):
        sid = "rename2" + "x" * 25
        cohort_id = self._cohort_of_one(sid)
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}",
            method="PATCH",
            data=json.dumps({"label": "   "}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("empty", json.loads(body)["error"])

    def test_the_cohort_lifecycle_makes_only_declared_transitions(self):
        """Drive create → start → pause → start over HTTP and check the server
        never reported a status move COHORT_TRANSITIONS does not declare.

        The server runs in a subprocess, so its stdout is the only channel —
        cohort_transition logs rather than raises so a live run with a paid
        batch in flight cannot be stranded by an unforeseen move.
        """
        sid = "lifecyc" + "x" * 25
        self.assertEqual(self.ingest(sid), 200)
        self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
        self.seed_analysis(sid)
        cohort = self.create_and_analyze(purchased=1, all_n=0)
        cohort_id = cohort["id"]
        self.wait_done(cohort_id)

        for path in ("pause", "analyze"):
            request(f"{self.base}/api/cohorts/{cohort_id}/{path}", method="POST", data=b"")
        request(f"{self.base}/api/cohorts/{cohort_id}")

        offenders = [
            line for line in self.server_log().splitlines() if "not a declared transition" in line
        ]
        self.assertEqual(offenders, [])

    def test_deleting_a_session_removes_it_from_its_cohorts(self):
        """Otherwise the membership row outlives the recording, and
        enrich_cohort_status keeps counting it as a completed analysis."""
        sid = "delsess" + "x" * 25
        cohort_id = self._cohort_of_one(sid)
        _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
        self.assertEqual(json.loads(body)["progress"]["done"], 1)

        status, _ = request(f"{self.base}/api/sessions/{sid}", method="DELETE")
        self.assertEqual(status, 200)

        _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
        cohort = json.loads(body)
        self.assertEqual([s["session_id"] for s in cohort["sessions"]], [])
        self.assertEqual(cohort["progress"]["total"], 0)
        self.assertEqual(cohort["progress"]["done"], 0)
        self.assertEqual(self.stored_cohort(cohort_id)["sessions"], [])

    def test_deleting_one_session_leaves_the_others_in_place(self):
        sids = ["multi1" + "x" * 26, "multi2" + "x" * 26]
        for sid in sids:
            self.assertEqual(self.ingest(sid), 200)
            self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
            self.seed_analysis(sid)
        cohort = self.create_and_analyze(purchased=2, all_n=0)
        self.wait_done(cohort["id"])
        self.assertEqual(len(self.stored_cohort(cohort["id"])["sessions"]), 2)

        self.assertEqual(request(f"{self.base}/api/sessions/{sids[0]}", method="DELETE")[0], 200)

        _, body = request(f"{self.base}/api/cohorts/{cohort['id']}")
        live = json.loads(body)
        self.assertEqual([s["session_id"] for s in live["sessions"]], [sids[1]])
        self.assertEqual(live["progress"]["done"], 1)

    def test_delete_removes_cohort_and_keeps_session_writeups(self):
        sid = "delcoh" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        analyze_store.write_cut(self.cohort_store(), "", cohort_id, "some-cut", {"label": "x"})
        session_dir = self.data_dir / "sessions" / sid
        self.assertIsNotNone(analyze.read_analysis(session_dir))

        status, body = request(f"{self.base}/api/cohorts/{cohort_id}", method="DELETE")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        self.assertNotIn(cohort_id, [c["id"] for c in self.cohorts()])
        self.assertIsNone(self.stored_cohort(cohort_id))
        self.assertEqual(self.stored_cuts(cohort_id), {})
        # Deleting a cohort must not take the write-ups of its members with it.
        self.assertIsNotNone(analyze.read_analysis(session_dir))

        status, body = request(f"{self.base}/api/cohorts/{cohort_id}", method="DELETE")
        self.assertEqual(status, 404, body)

    def test_legacy_cohort_file_is_migrated_on_read(self):
        sid = "legacy" + "x" * 26
        self.assertEqual(self.ingest(sid), 200)
        self.seed_analysis(sid)
        legacy = {
            "id": "cohort-1700000000000",
            "label": "Sample 1+0",
            "created_at": 1700000000000,
            "status": "done",
            "purchased_n": 1,
            "all_n": 0,
            "sessions": [{"session_id": sid, "filter": "purchased", "status": "done"}],
        }
        (self.data_dir / "cohort.json").write_text(json.dumps(legacy))

        self.assertIn("cohort-1700000000000", [c["id"] for c in self.cohorts()])
        migrated = self.stored_cohort("cohort-1700000000000")
        self.assertEqual(migrated["label"], "Sample 1+0")
        self.assertEqual([s["session_id"] for s in migrated["sessions"]], [sid])
        # Both legacy files are consumed, so the migration runs exactly once.
        self.assertFalse((self.data_dir / "cohort.json").exists())
        self.assertFalse((self.data_dir / "cohorts" / "cohort-1700000000000.json").exists())

    def _cohort_of_one(self, sid):
        """A settled one-session cohort, with cached analysis so it lands done."""
        self.assertEqual(self.ingest(sid), 200)
        self.assertEqual(self.order(sid, {"event": "checkout_completed"}), 200)
        self.seed_analysis(sid)
        cohort = self.create_and_analyze(purchased=1, all_n=0)
        cohort_id = cohort["id"]
        self.wait_done(cohort_id)
        return cohort_id

    def test_retry_clears_the_recorded_error_for_that_row(self):
        sid = "retry1" + "x" * 26
        cohort_id = self._cohort_of_one(sid)

        # Simulate a row that failed during the run: status is re-derived from
        # disk, so the cached analysis has to go along with the error marker.
        session = self.data_dir / "sessions" / sid
        analyze.clear_analysis(session)
        analyze.write_job(session, "error", error="page reconstruction timed out")
        saved = self.stored_cohort(cohort_id)
        saved["sessions"][0]["status"] = "error"
        saved["sessions"][0]["error"] = "page reconstruction timed out"
        saved["status"] = "error"
        self.store_cohort(saved)

        _, body = request(f"{self.base}/api/cohorts/{cohort_id}")
        self.assertEqual(json.loads(body)["sessions"][0]["status"], "error")

        # Restore the cache so the retry settles without a real Gemini call.
        self.seed_analysis(sid)

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/sessions/{sid}/retry",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 202, body)

        cohort = self.wait_done(cohort_id)
        entry = cohort["sessions"][0]
        self.assertEqual(entry["status"], "done")
        self.assertNotIn("error", entry)

    def test_retry_rejects_a_session_outside_the_cohort(self):
        sid = "retry2" + "x" * 26
        cohort_id = self._cohort_of_one(sid)
        other = "nother" + "x" * 26
        self.assertEqual(self.ingest(other), 200)

        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/sessions/{other}/retry",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 404)
        self.assertIn("not in this cohort", json.loads(body)["error"])

    def test_retry_without_a_cohort_is_404(self):
        sid = "retry3" + "x" * 26
        self.assertEqual(self.ingest(sid), 200)
        status, body = request(
            f"{self.base}/api/cohorts/cohort-nope/sessions/{sid}/retry",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 404)
        self.assertIn("no such cohort", json.loads(body)["error"])


class TestCohortUnavailable(ServerCase):
    env = {"GEMINI_API_KEY": ""}

    def test_sampling_needs_no_key_but_starting_analysis_does(self):
        """Sampling only reads the index, so it works without a key; starting
        analysis is what spends one, and that is what must refuse."""
        sid = "d" * 32
        self.assertEqual(self.ingest(sid), 200)
        status, body = request(
            f"{self.base}/api/cohorts?purchased=0&all=1&min_duration_ms=0&url_contains=",
            method="POST",
            data=b"",
        )
        self.assertEqual(status, 201, body)

        cohort_id = json.loads(body)["id"]
        status, body = request(
            f"{self.base}/api/cohorts/{cohort_id}/analyze", method="POST", data=b""
        )
        self.assertEqual(status, 503)
        self.assertIn("not configured", json.loads(body)["error"])

    def test_a_refusable_refresh_keeps_the_cached_answer(self):
        """Without a key the refresh cannot be served — it must leave the good
        answer standing rather than delete what it cannot replace."""
        from capture import analyze

        pool = self.data_dir / "sessions"
        cohort = analyze.write_cohort(
            pool,
            {"id": "cohort-keyless", "status": "done", "sessions": []},
        )
        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(pool, cohort["id"], key, {"cut": key, "analysis": {"headline": "old"}})
        status, _ = request(
            f"{self.base}/api/cohorts/{cohort['id']}/cut?outcome=purchased&refresh=1"
        )
        self.assertEqual(status, 503)
        still = analyze.read_cut(pool, cohort["id"], key)
        self.assertEqual(still["analysis"]["headline"], "old")


if __name__ == "__main__":
    unittest.main()
