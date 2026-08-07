"""Cut analysis: filtering a cohort, keying the cache, shaping the model's reply."""

import asyncio
import inspect
import json
import re
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


class TestSelectRowsByCut(unittest.TestCase):
    def test_an_empty_filter_selects_the_whole_cohort(self):
        rows = [row("a", "purchased"), row("b"), row("c", "purchased")]
        selected, spec = analyze.select_rows_by_cut(rows, None)
        self.assertEqual([r["session_id"] for r in selected], ["a", "b", "c"])
        self.assertEqual(spec, {})

    def test_outcome_filter_selects_its_bucket(self):
        rows = [row("a", "purchased"), row("b"), row("c", "purchased")]
        selected, spec = analyze.select_rows_by_cut(rows, {"outcome": "purchased"})
        self.assertEqual([r["session_id"] for r in selected], ["a", "c"])
        self.assertEqual(spec, {"outcome": ["purchased"]})

    def test_meta_source_matches_fbclid_and_known_sources(self):
        rows = [
            row("meta1", utm_source="facebook"),
            row("goog", utm_source="google"),
            row("meta2", fbclid=True),
        ]
        selected, _ = analyze.select_rows_by_cut(rows, {"utm_source": "meta"})
        self.assertEqual({r["session_id"] for r in selected}, {"meta1", "meta2"})

    def test_filters_across_fields_are_anded(self):
        rows = [
            row("hit", "purchased", "mobile", utm_campaign="spring"),
            row("wrong_outcome", "rest", "mobile", utm_campaign="spring"),
            row("wrong_campaign", "purchased", "mobile", utm_campaign="fall"),
        ]
        selected, _ = analyze.select_rows_by_cut(
            rows, {"outcome": "purchased", "utm_campaign": "spring"}
        )
        self.assertEqual([r["session_id"] for r in selected], ["hit"])

    def test_a_filter_matching_nothing_selects_nothing(self):
        rows = [row("a", "purchased"), row("b", "rest")]
        selected, _ = analyze.select_rows_by_cut(rows, {"utm_campaign": "absent"})
        self.assertEqual(selected, [])


class TestCutKey(unittest.TestCase):
    def test_same_cut_hashes_the_same_regardless_of_key_order(self):
        one = analyze.cut_key({"outcome": "purchased", "utm_source": "meta"})
        two = analyze.cut_key({"utm_source": "META", "outcome": "purchased"})
        self.assertEqual(one, two)

    def test_different_filters_are_different_cuts(self):
        a = analyze.cut_key({"utm_source": "meta"})
        b = analyze.cut_key({"utm_source": "google"})
        self.assertNotEqual(a, b)

    def test_key_survives_a_json_round_trip(self):
        key = analyze.cut_key({"outcome": "purchased"})
        self.assertEqual(json.loads(key)["filter"], {"outcome": ["purchased"]})


class TestNormalizeCutAnalysis(unittest.TestCase):
    def test_keeps_the_documented_shape_and_drops_junk(self):
        out = analyze.normalize_cut_analysis(
            {
                "headline": "  Buyers switch off the default supply size.  ",
                "path": ["landed on /p at [00:02]", "", 12, None],
                "stalls": ["40s on the guarantee at [01:10]"],
                "clicks": ["tapped 'Add to cart' at [00:44]"],
                "friction": ["cart icon at [00:17] does nothing"],
                "caveats": ["only 2 sessions"],
                "extra_key": "ignored",
            }
        )
        self.assertEqual(out["headline"], "Buyers switch off the default supply size.")
        self.assertEqual(out["path"], ["landed on /p at [00:02]"])
        self.assertEqual(out["stalls"], ["40s on the guarantee at [01:10]"])
        self.assertEqual(out["clicks"], ["tapped 'Add to cart' at [00:44]"])
        self.assertEqual(out["friction"], ["cart icon at [00:17] does nothing"])
        self.assertEqual(out["caveats"], ["only 2 sessions"])
        self.assertNotIn("extra_key", out)
        # The two-sided fields are gone, not merely unused.
        self.assertNotIn("a_distinctive", out)
        self.assertNotIn("b_distinctive", out)

    def test_every_documented_section_is_present(self):
        out = analyze.normalize_cut_analysis({})
        for key in ("path", "stalls", "clicks", "friction", "caveats"):
            self.assertEqual(out[key], [], key)

    def test_garbage_becomes_an_empty_analysis(self):
        for junk in (None, [], "text", {"headline": 42}):
            out = analyze.normalize_cut_analysis(junk)
            self.assertEqual(out["headline"], "")
            for key in analyze.CUT_SECTIONS:
                self.assertEqual(out[key], [], key)

    def test_long_lists_and_strings_are_capped(self):
        out = analyze.normalize_cut_analysis(
            {"path": [f"item {i}" for i in range(50)], "headline": "x" * 900}
        )
        self.assertEqual(len(out["path"]), analyze.CUT_ITEMS_PER_SECTION)
        self.assertEqual(len(out["headline"]), 600)

    def test_junk_entries_do_not_eat_the_item_budget(self):
        """A list of non-strings must not trip the cap before real items land."""
        items = [None] * 20 + ["a real item with [00:05]"]
        out = analyze.normalize_cut_analysis({"friction": items})
        self.assertEqual(out["friction"], ["a real item with [00:05]"])


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

    def test_an_empty_group_skips_the_model_call(self):
        calls = []

        async def fake(*args):
            calls.append(args)
            return analyze.empty_cut_analysis()

        analyze.call_gemini_cut = fake
        rows = [row("a", "purchased"), row("b", "purchased")]
        out = asyncio.run(analyze.build_cut_analysis(rows, {"outcome": "checkout"}))

        self.assertEqual(calls, [], "no sessions match — nothing to read")
        self.assertEqual(out["n"], 0)
        self.assertIn("No analyzed sessions", out["analysis"]["headline"])

    def test_a_filtered_group_calls_the_model_and_records_membership(self):
        seen = {}

        async def fake(rows, label, given=""):
            seen["rows"] = [r["session_id"] for r in rows]
            seen["label"] = label
            seen["given"] = given
            return analyze.normalize_cut_analysis({"headline": "they hesitate"})

        analyze.call_gemini_cut = fake
        rows = [row("p1", "purchased"), row("n1"), row("n2")]
        out = asyncio.run(analyze.build_cut_analysis(rows, {"outcome": "purchased"}))

        self.assertEqual(seen["rows"], ["p1"])
        self.assertEqual(seen["label"], "purchased")
        self.assertEqual(out["n"], 1)
        self.assertEqual(out["session_ids"], ["p1"])
        self.assertEqual(out["analysis"]["headline"], "they hesitate")
        self.assertEqual(out["cut"], analyze.cut_key({"outcome": "purchased"}))

    def test_an_unfiltered_group_reads_the_whole_cohort(self):
        seen = {}

        async def fake(rows, label, given=""):
            seen["rows"] = [r["session_id"] for r in rows]
            seen["label"] = label
            seen["given"] = given
            return analyze.empty_cut_analysis()

        analyze.call_gemini_cut = fake
        rows = [row("p1", "purchased"), row("n1"), row("n2")]
        out = asyncio.run(analyze.build_cut_analysis(rows, None))

        self.assertEqual(seen["rows"], ["p1", "n1", "n2"])
        # No filter means nothing is already known, so nothing is banned.
        self.assertEqual(seen["given"], "")
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["label"], "all sessions")

    def test_side_text_labels_every_session_for_counting(self):
        text = analyze._cut_side_text(
            [row("s1", "purchased", "mobile", utm_source="meta"), row("s2", "checkout")],
            "SESSIONS",
        )
        self.assertIn("SESSIONS — 2 session(s)", text)
        self.assertIn("SESSIONS session 1", text)
        self.assertIn("SESSIONS session 2", text)
        self.assertIn("outcome: purchased", text)
        self.assertIn("reached checkout", text)
        self.assertIn("device: mobile", text)
        self.assertIn("write-up for s1", text)


class TestGivenFacts(unittest.TestCase):
    """The filter that selected a group must never come back as a finding."""

    def given(self, spec):
        return analyze._given_facts(analyze.normalize_slice(spec))

    def test_outcome_filter_bans_the_obvious_restatement(self):
        text = self.given({"outcome": ["purchased"]})
        self.assertIn("Every session in this group purchased", text)
        # The exact tautology the first version of this prompt produced.
        self.assertIn("clicking buy", text)

    def test_campaign_and_device_filters_are_declared(self):
        text = self.given({"utm_campaign": "spring", "device": ["mobile"]})
        self.assertIn("utm_campaign=spring", text)
        self.assertIn("was on mobile", text)

    def test_an_unconstrained_cut_has_nothing_to_declare(self):
        self.assertEqual(analyze._given_facts({}), "")

    def test_build_passes_the_given_facts_to_the_model(self):
        seen = {}

        async def fake(rows, label, given=""):
            seen["given"] = given
            return analyze.empty_cut_analysis()

        saved_enabled, saved_call = analyze.ANALYSIS_ENABLED, analyze.call_gemini_cut
        analyze.ANALYSIS_ENABLED = True
        analyze.call_gemini_cut = fake
        analyze._gate_loop = None
        try:
            rows = [row("a", "purchased"), row("b", "rest")]
            asyncio.run(analyze.build_cut_analysis(rows, {"outcome": ["purchased"]}))
        finally:
            analyze.ANALYSIS_ENABLED = saved_enabled
            analyze.call_gemini_cut = saved_call
            analyze._gate_loop = None
        self.assertIn("Every session in this group purchased", seen["given"])


class TestCutOutputBudget(unittest.TestCase):
    """Five sections of concrete items need room; too small a cap summarises."""

    def test_the_answer_has_room_beyond_the_thinking_budget(self):
        source = inspect.getsource(analyze.call_gemini_cut)
        max_out = int(re.search(r"max_output_tokens=(\d+)", source).group(1))
        thinking = int(re.search(r"thinking_budget=(\d+)", source).group(1))
        answer_room = max_out - thinking
        sections = len(analyze.CUT_SECTIONS)
        # ~225 tokens for a 900-char item; the prompt asks for at least 3 each.
        needed = sections * 3 * 225
        self.assertGreater(
            answer_room,
            needed,
            f"{answer_room} tokens of answer room cannot hold {sections} "
            f"sections x 3 concrete items (~{needed})",
        )


class TestCutPromptContent(unittest.TestCase):
    """The prompt has to keep asking for the things the first version lost."""

    def test_demands_concrete_evidence(self):
        prompt = analyze.CUT_PROMPT
        for phrase in ("timestamps", "scroll depth", "verbatim", "quote"):
            self.assertIn(phrase, prompt.lower(), f"prompt lost: {phrase}")

    def test_keeps_the_on_screen_versus_looked_at_distinction(self):
        self.assertIn("never", analyze.CUT_PROMPT.lower())
        self.assertIn("was shown", analyze.CUT_PROMPT)

    def test_names_friction_as_the_priority(self):
        self.assertIn("rage click", analyze.CUT_PROMPT.lower())

    def test_demands_evidence_in_every_item(self):
        """Depth regression guard: vague items must be explicitly disallowed."""
        prompt = analyze.CUT_PROMPT
        self.assertIn("[MM:SS]", prompt)
        self.assertIn("too vague to keep", prompt)
        # An item budget that is too small is what drove the summarising.
        self.assertIn("3-8 items", prompt)
        self.assertNotIn("2-5 items", prompt)

    def test_keeps_banning_the_filter_restatement(self):
        """The tautology guard has to survive the move to a single group."""
        prompt = analyze.CUT_PROMPT
        self.assertIn("BANNED", prompt)
        self.assertIn("Could I have predicted this from the group's definition", prompt)
        self.assertIn("Buyers buy.", prompt)

    def test_asks_for_the_single_group_schema(self):
        prompt = analyze.CUT_PROMPT
        self.assertIn('"headline"', prompt)
        for key in analyze.CUT_SECTIONS:
            self.assertIn(f'"{key}"', prompt)
        # Nothing should still be asking the model to contrast two sides.
        self.assertNotIn("a_distinctive", prompt)
        self.assertNotIn("b_distinctive", prompt)
        self.assertNotIn("Group B", prompt)


class TestCutCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = Path(self.tmp.name) / "sessions"
        self.pool.mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trips_a_cut_under_its_key(self):
        key = analyze.cut_key({"outcome": "purchased"})
        self.assertIsNone(analyze.read_cut(self.pool, "cohort-1", key))
        analyze.write_cut(self.pool, "cohort-1", key, {"cut": key, "n": 3})
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", key)["n"], 3)

    def test_cuts_of_one_cohort_do_not_collide(self):
        k1 = analyze.cut_key({"outcome": "purchased"})
        k2 = analyze.cut_key({"utm_source": "meta"})
        analyze.write_cut(self.pool, "cohort-1", k1, {"n": 1})
        analyze.write_cut(self.pool, "cohort-1", k2, {"n": 2})
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", k1)["n"], 1)
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", k2)["n"], 2)

    def test_cohorts_do_not_share_a_cut_cache(self):
        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(self.pool, "cohort-1", key, {"n": 1})
        self.assertIsNone(analyze.read_cut(self.pool, "cohort-2", key))

    def test_a_bad_cohort_id_never_reaches_the_filesystem(self):
        with self.assertRaises(ValueError):
            analyze.cuts_path(self.pool, "../../etc/passwd")
        self.assertEqual(analyze.read_cuts(self.pool, "../../etc/passwd"), {})


if __name__ == "__main__":
    unittest.main()
