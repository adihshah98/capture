"""Cut analysis: splitting a cohort, keying the cache, shaping the model's reply."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze


def row(sid, outcome="rest", device=None, **attrs):
    return {
        "session_id": sid,
        "outcome": outcome,
        "device": device,
        "attribution": attrs or None,
        "text": f"write-up for {sid}",
    }


class TestOutcomeAndDeviceClass(unittest.TestCase):
    def test_outcome_is_three_disjoint_buckets(self):
        self.assertEqual(analyze.outcome_class({"purchased_at": 1}), "purchased")
        # A purchase implies checkout, but "checkout" means reached-and-did-not-buy.
        self.assertEqual(
            analyze.outcome_class({"purchased_at": 1, "checkout_started_at": 1}),
            "purchased",
        )
        self.assertEqual(analyze.outcome_class({"checkout_started_at": 1}), "checkout")
        self.assertEqual(analyze.outcome_class({}), "rest")
        self.assertEqual(analyze.outcome_class(None), "rest")

    def test_device_splits_on_the_shared_threshold(self):
        edge = analyze.MOBILE_MAX_WIDTH
        self.assertEqual(analyze.device_class({"width": edge - 1}), "mobile")
        self.assertEqual(analyze.device_class({"width": edge}), "desktop")
        self.assertIsNone(analyze.device_class({"width": 0}))
        self.assertIsNone(analyze.device_class(None))


class TestNormalizeSlice(unittest.TestCase):
    def test_multi_valued_outcome_and_device(self):
        spec = analyze.normalize_slice(
            {"outcome": ["purchased", "checkout"], "device": "mobile"}
        )
        self.assertEqual(spec["outcome"], ["checkout", "purchased"])
        self.assertEqual(spec["device"], ["mobile"])

    def test_legacy_all_becomes_the_two_non_purchase_buckets(self):
        self.assertEqual(
            analyze.normalize_slice({"outcome": "all"})["outcome"], ["checkout", "rest"]
        )

    def test_selecting_every_value_is_no_constraint(self):
        self.assertEqual(
            analyze.normalize_slice({"outcome": ["purchased", "checkout", "rest"]}), {}
        )
        self.assertEqual(analyze.normalize_slice({"device": ["mobile", "desktop"]}), {})

    def test_unknown_values_are_dropped(self):
        self.assertEqual(analyze.normalize_slice({"outcome": ["nope"]}), {})
        self.assertEqual(analyze.normalize_slice({"device": ["tablet"]}), {})

    def test_combined_constraints_are_kept_together(self):
        spec = analyze.normalize_slice(
            {"outcome": ["checkout"], "device": ["mobile"], "utm_campaign": "Spring"}
        )
        self.assertEqual(
            spec, {"outcome": ["checkout"], "device": ["mobile"], "utm_campaign": "spring"}
        )


class TestMatchOutcomeAndDevice(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("pm", "purchased", "mobile", utm_source="google"),
            row("cm", "checkout", "mobile", utm_source="facebook"),
            row("rd", "rest", "desktop", utm_campaign="spring"),
            row("pd", "purchased", "desktop"),
        ]

    def match(self, spec):
        norm = analyze.normalize_slice(spec)
        return [r["session_id"] for r in self.rows if analyze.row_matches_slice(r, norm)]

    def test_each_outcome_selects_its_own_bucket(self):
        self.assertEqual(self.match({"outcome": ["purchased"]}), ["pm", "pd"])
        self.assertEqual(self.match({"outcome": ["checkout"]}), ["cm"])
        self.assertEqual(self.match({"outcome": ["rest"]}), ["rd"])

    def test_the_three_buckets_are_disjoint_and_total(self):
        seen = []
        for value in analyze.OUTCOME_VALUES:
            got = self.match({"outcome": [value]})
            self.assertFalse(set(got) & set(seen), "buckets must not overlap")
            seen.extend(got)
        self.assertEqual(sorted(seen), sorted(r["session_id"] for r in self.rows))

    def test_device_slices(self):
        self.assertEqual(self.match({"device": ["mobile"]}), ["pm", "cm"])
        self.assertEqual(self.match({"device": ["desktop"]}), ["rd", "pd"])

    def test_constraints_intersect(self):
        self.assertEqual(self.match({"outcome": ["purchased"], "device": ["mobile"]}), ["pm"])
        self.assertEqual(
            self.match({"device": ["mobile"], "utm_source": "google"}), ["pm"]
        )
        self.assertEqual(
            self.match({"outcome": ["purchased"], "utm_campaign": "spring"}), []
        )

    def test_unknown_device_cannot_satisfy_a_device_constraint(self):
        rows = [row("unknown", "purchased", None)]
        spec = analyze.normalize_slice({"device": ["mobile"]})
        self.assertFalse(analyze.row_matches_slice(rows[0], spec))


class TestSliceLabel(unittest.TestCase):
    def label(self, spec):
        return analyze.slice_label(analyze.normalize_slice(spec))

    def test_names_the_common_unions_directly(self):
        self.assertEqual(self.label({"outcome": ["purchased", "checkout"]}), "reached checkout")
        self.assertEqual(self.label({"outcome": ["checkout", "rest"]}), "did not purchase")

    def test_single_buckets_and_combinations(self):
        self.assertEqual(self.label({"outcome": ["purchased"]}), "purchased")
        self.assertEqual(self.label({"device": ["mobile"]}), "mobile")
        self.assertEqual(
            self.label({"outcome": ["checkout"], "device": ["mobile"]}),
            "reached checkout · mobile",
        )
        self.assertEqual(self.label({}), "everyone else")


class TestSplitRowsByCut(unittest.TestCase):
    def test_default_split_is_purchased_vs_rest(self):
        rows = [row("a", "purchased"), row("b"), row("c", "purchased")]
        a, b, spec_a, spec_b = analyze.split_rows_by_cut(rows, None, None)
        self.assertEqual([r["session_id"] for r in a], ["a", "c"])
        self.assertEqual([r["session_id"] for r in b], ["b"])
        self.assertEqual(spec_a, {"outcome": ["purchased"]})
        self.assertEqual(spec_b, {"outcome": ["checkout", "rest"]})

    def test_empty_b_means_everyone_not_in_a(self):
        rows = [
            row("meta1", utm_source="facebook"),
            row("goog", utm_source="google"),
            row("meta2", fbclid=True),
        ]
        a, b, _, _ = analyze.split_rows_by_cut(rows, {"utm_source": "meta"}, None)
        self.assertEqual({r["session_id"] for r in a}, {"meta1", "meta2"})
        self.assertEqual([r["session_id"] for r in b], ["goog"])

    def test_a_wins_when_a_session_matches_both_sides(self):
        rows = [row("both", "purchased", utm_source="google")]
        a, b, _, _ = analyze.split_rows_by_cut(
            rows, {"outcome": "purchased"}, {"utm_source": "google"}
        )
        self.assertEqual([r["session_id"] for r in a], ["both"])
        self.assertEqual(b, [])

    def test_campaign_a_vs_campaign_b(self):
        rows = [
            row("x", utm_campaign="spring"),
            row("y", utm_campaign="fall"),
            row("z", utm_campaign="spring"),
        ]
        a, b, _, _ = analyze.split_rows_by_cut(
            rows, {"utm_campaign": "spring"}, {"utm_campaign": "fall"}
        )
        self.assertEqual({r["session_id"] for r in a}, {"x", "z"})
        self.assertEqual([r["session_id"] for r in b], ["y"])


class TestCutKey(unittest.TestCase):
    def test_same_cut_hashes_the_same_regardless_of_key_order(self):
        one = analyze.cut_key(
            {"outcome": "purchased", "utm_source": "meta"}, {"utm_source": "google"}
        )
        two = analyze.cut_key(
            {"utm_source": "META", "outcome": "purchased"}, {"utm_source": "google"}
        )
        self.assertEqual(one, two)

    def test_swapping_the_sides_is_a_different_cut(self):
        a = analyze.cut_key({"utm_source": "meta"}, {"utm_source": "google"})
        b = analyze.cut_key({"utm_source": "google"}, {"utm_source": "meta"})
        self.assertNotEqual(a, b)

    def test_key_survives_a_json_round_trip(self):
        key = analyze.cut_key({"outcome": "purchased"}, None)
        self.assertEqual(json.loads(key)["a"], {"outcome": ["purchased"]})


class TestNormalizeCutAnalysis(unittest.TestCase):
    def test_keeps_the_documented_shape_and_drops_junk(self):
        out = analyze.normalize_cut_analysis(
            {
                "headline": "  Meta buyers reach checkout faster.  ",
                "shared": ["both browse PDPs", "", 12, None],
                "a_distinctive": ["3 of 5 use the size chart"],
                "b_distinctive": [],
                "caveats": ["only 2 sessions on side B"],
                "extra_key": "ignored",
            }
        )
        self.assertEqual(out["headline"], "Meta buyers reach checkout faster.")
        self.assertEqual(out["shared"], ["both browse PDPs"])
        self.assertEqual(out["a_distinctive"], ["3 of 5 use the size chart"])
        self.assertEqual(out["b_distinctive"], [])
        self.assertEqual(out["caveats"], ["only 2 sessions on side B"])
        self.assertNotIn("extra_key", out)

    def test_garbage_becomes_an_empty_analysis(self):
        for junk in (None, [], "text", {"headline": 42}):
            out = analyze.normalize_cut_analysis(junk)
            self.assertEqual(out["headline"], "")
            self.assertEqual(out["shared"], [])

    def test_long_lists_and_strings_are_capped(self):
        out = analyze.normalize_cut_analysis(
            {"shared": [f"item {i}" for i in range(50)], "headline": "x" * 900}
        )
        self.assertEqual(len(out["shared"]), 8)
        self.assertEqual(len(out["headline"]), 400)


class TestBuildCutAnalysis(unittest.TestCase):
    def setUp(self):
        self._enabled = analyze.ANALYSIS_ENABLED
        self._call = analyze.call_gemini_cut
        analyze.ANALYSIS_ENABLED = True
        analyze._gate_loop = None
        self.addCleanup(self.restore)

    def restore(self):
        analyze.ANALYSIS_ENABLED = self._enabled
        analyze.call_gemini_cut = self._call
        analyze._gate_loop = None

    def test_one_sided_cut_skips_the_model_call(self):
        calls = []

        async def fake(*args):
            calls.append(args)
            return analyze.empty_cut_analysis()

        analyze.call_gemini_cut = fake
        rows = [row("a", "purchased"), row("b", "purchased")]
        out = asyncio.run(analyze.build_cut_analysis(rows, {"outcome": "purchased"}, None))

        self.assertEqual(calls, [], "no sessions on side B — nothing to contrast")
        self.assertEqual(out["n_b"], 0)
        self.assertIn("No analyzed sessions", out["analysis"]["headline"])

    def test_two_sided_cut_calls_the_model_and_records_membership(self):
        seen = {}

        async def fake(rows_a, rows_b, label_a, label_b):
            seen["a"] = [r["session_id"] for r in rows_a]
            seen["b"] = [r["session_id"] for r in rows_b]
            seen["labels"] = (label_a, label_b)
            return analyze.normalize_cut_analysis({"headline": "they differ"})

        analyze.call_gemini_cut = fake
        rows = [row("p1", "purchased"), row("n1"), row("n2")]
        out = asyncio.run(analyze.build_cut_analysis(rows, None, None))

        self.assertEqual(seen["a"], ["p1"])
        self.assertEqual(seen["b"], ["n1", "n2"])
        self.assertEqual(out["n_a"], 1)
        self.assertEqual(out["n_b"], 2)
        self.assertEqual(out["session_ids_a"], ["p1"])
        self.assertEqual(out["session_ids_b"], ["n1", "n2"])
        self.assertEqual(out["analysis"]["headline"], "they differ")
        self.assertEqual(out["cut"], analyze.cut_key(None, None))

    def test_side_text_labels_every_session_for_counting(self):
        text = analyze._cut_side_text(
            [row("s1", "purchased", utm_source="meta"), row("s2")], "GROUP A"
        )
        self.assertIn("GROUP A — 2 session(s)", text)
        self.assertIn("GROUP A session 1", text)
        self.assertIn("GROUP A session 2", text)
        self.assertIn("outcome: purchased", text)
        self.assertIn("did not purchase", text)
        self.assertIn("write-up for s1", text)


class TestCutCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = Path(self.tmp.name) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trips_a_cut_under_its_key(self):
        key = analyze.cut_key({"outcome": "purchased"}, None)
        self.assertIsNone(analyze.read_cut(self.pool, "cohort-1", key))
        analyze.write_cut(self.pool, "cohort-1", key, {"cut": key, "n_a": 3})
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", key)["n_a"], 3)

    def test_cuts_of_one_cohort_do_not_collide(self):
        k1 = analyze.cut_key({"outcome": "purchased"}, None)
        k2 = analyze.cut_key({"utm_source": "meta"}, None)
        analyze.write_cut(self.pool, "cohort-1", k1, {"n_a": 1})
        analyze.write_cut(self.pool, "cohort-1", k2, {"n_a": 2})
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", k1)["n_a"], 1)
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", k2)["n_a"], 2)

    def test_cohorts_do_not_share_a_cut_cache(self):
        key = analyze.cut_key({"outcome": "purchased"}, None)
        analyze.write_cut(self.pool, "cohort-1", key, {"n_a": 1})
        self.assertIsNone(analyze.read_cut(self.pool, "cohort-2", key))

    def test_a_bad_cohort_id_never_reaches_the_filesystem(self):
        with self.assertRaises(ValueError):
            analyze.cuts_path(self.pool, "../../etc/passwd")
        self.assertEqual(analyze.read_cuts(self.pool, "../../etc/passwd"), {})


if __name__ == "__main__":
    unittest.main()
