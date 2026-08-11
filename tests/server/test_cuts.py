"""Cut analysis: filtering a cohort, keying the cache, shaping the model's reply."""

import asyncio
import inspect
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pgfixture

from capture import analyze
from capture.analyze import slices


def row(sid, outcome="rest", device=None, page=None, **attrs):
    return {
        "session_id": sid,
        "outcome": outcome,
        "device": device,
        "page": page,
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
        edge = slices.MOBILE_MAX_WIDTH
        self.assertEqual(analyze.device_class({"width": edge - 1}), "mobile")
        self.assertEqual(analyze.device_class({"width": edge}), "desktop")
        self.assertIsNone(analyze.device_class({"width": 0}))
        self.assertIsNone(analyze.device_class(None))


class TestPagePath(unittest.TestCase):
    def test_strips_query_hash_and_trailing_slash(self):
        self.assertEqual(
            analyze.page_path("https://shop.test/products/cream/?utm_source=meta#x"),
            "/products/cream",
        )
        self.assertEqual(analyze.page_path("/p/"), "/p")
        self.assertEqual(analyze.page_path("/"), "/")

    def test_accepts_a_bare_path(self):
        self.assertEqual(analyze.page_path("/collections/sale"), "/collections/sale")

    def test_empty_is_none(self):
        self.assertIsNone(analyze.page_path(""))
        self.assertIsNone(analyze.page_path(None))

    def test_a_real_host_with_no_path_is_the_homepage(self):
        for url in (
            "https://shop.test",
            "https://shop.test/",
            "http://localhost:3000/",
            "http://127.0.0.1:3000/",
        ):
            with self.subTest(url=url):
                self.assertEqual(analyze.page_path(url), "/")

    def test_a_bare_host_still_resolves(self):
        self.assertEqual(analyze.page_path("shop.test/p"), "/p")
        self.assertEqual(analyze.page_path("localhost:3000/cart"), "/cart")

    def test_unreadable_entry_urls_are_unknown_not_the_homepage(self):
        # "/" would be indistinguishable from a real homepage visit: offered in
        # the facet picker, counted in a measured split, and stated to the model
        # as fact. None is the unknown every consumer already handles.
        for url in (
            "not a url at all",
            "null",
            "undefined",
            "about:blank",
            "javascript:void(0)",
            "data:text/html,<h1>hi</h1>",
            "file:///Users/x/index.html",
            "chrome-extension://abc/page.html",
            "#/spa/route",
            "?utm_source=x",
            "   ",
        ):
            with self.subTest(url=url):
                self.assertIsNone(analyze.page_path(url))

    def test_an_encoded_separator_does_not_merge_two_pages(self):
        # /a%2Fb is one segment containing a slash; /a/b is two segments.
        encoded = analyze.page_path("https://shop.test/a%2Fb")
        plain = analyze.page_path("https://shop.test/a/b")
        self.assertEqual(plain, "/a/b")
        self.assertNotEqual(encoded, plain)

    def test_ordinary_percent_encoding_is_still_decoded(self):
        self.assertEqual(analyze.page_path("https://shop.test/caf%C3%A9"), "/café")


class TestNormalizeSlice(unittest.TestCase):
    def test_multi_valued_outcome_and_device(self):
        spec = analyze.normalize_slice({"outcome": ["purchased", "checkout"], "device": "mobile"})
        self.assertEqual(spec["outcome"], ["checkout", "purchased"])
        self.assertEqual(spec["device"], ["mobile"])

    def test_legacy_all_becomes_the_two_non_purchase_buckets(self):
        self.assertEqual(
            analyze.normalize_slice({"outcome": "all"})["outcome"], ["checkout", "rest"]
        )

    def test_selecting_every_outcome_is_no_constraint(self):
        # Every session has an outcome, so all three select the same rows as
        # no filter at all.
        self.assertEqual(
            analyze.normalize_slice({"outcome": ["purchased", "checkout", "rest"]}), {}
        )

    def test_selecting_both_devices_still_means_a_known_device(self):
        # device_class() returns None for an unindexed viewport, so "mobile or
        # desktop" excludes those unknowns while no filter admits them. Dropping
        # the constraint here would widen the selection instead of narrowing it.
        self.assertEqual(
            analyze.normalize_slice({"device": ["mobile", "desktop"]}),
            {"device": ["desktop", "mobile"]},
        )

    def test_adding_a_device_never_selects_fewer_rows(self):
        rows = [
            {"session_id": "m", "device": "mobile"},
            {"session_id": "d", "device": "desktop"},
            {"session_id": "unknown", "device": None},
        ]

        def matched(spec):
            clean = analyze.normalize_slice(spec)
            return {r["session_id"] for r in rows if analyze.row_matches_slice(r, clean)}

        one = matched({"device": ["mobile"]})
        both = matched({"device": ["mobile", "desktop"]})
        self.assertEqual(one, {"m"})
        self.assertEqual(both, {"m", "d"})
        # The union of the parts, and nothing more — an unknown device is not
        # swept in by ticking every box.
        self.assertEqual(both, one | matched({"device": ["desktop"]}))

    def test_unknown_values_are_dropped(self):
        self.assertEqual(analyze.normalize_slice({"outcome": ["nope"]}), {})
        self.assertEqual(analyze.normalize_slice({"device": ["tablet"]}), {})

    def test_combined_constraints_are_kept_together(self):
        spec = analyze.normalize_slice(
            {
                "outcome": ["checkout"],
                "device": ["mobile"],
                "page": "https://shop.test/p/?x=1",
                "utm_campaign": "Spring",
            }
        )
        self.assertEqual(
            spec,
            {
                "outcome": ["checkout"],
                "device": ["mobile"],
                "page": "/p",
                "utm_campaign": "spring",
            },
        )

    def test_page_filter_normalizes_to_the_path(self):
        self.assertEqual(
            analyze.normalize_slice({"page": "/products/cream/"})["page"],
            "/products/cream",
        )


class TestMatchOutcomeAndDevice(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row("pm", "purchased", "mobile", page="/p", utm_source="google"),
            row("cm", "checkout", "mobile", page="/collections/sale", utm_source="facebook"),
            row("rd", "rest", "desktop", page="/p", utm_campaign="spring"),
            row("pd", "purchased", "desktop", page="/products/cream"),
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

    def test_page_slices(self):
        self.assertEqual(self.match({"page": "/p"}), ["pm", "rd"])
        self.assertEqual(self.match({"page": "/products/cream"}), ["pd"])

    def test_constraints_intersect(self):
        self.assertEqual(self.match({"outcome": ["purchased"], "device": ["mobile"]}), ["pm"])
        self.assertEqual(self.match({"device": ["mobile"], "utm_source": "google"}), ["pm"])
        self.assertEqual(self.match({"outcome": ["purchased"], "utm_campaign": "spring"}), [])
        self.assertEqual(self.match({"page": "/p", "device": ["desktop"]}), ["rd"])

    def test_unknown_device_cannot_satisfy_a_device_constraint(self):
        rows = [row("unknown", "purchased", None)]
        spec = analyze.normalize_slice({"device": ["mobile"]})
        self.assertFalse(analyze.row_matches_slice(rows[0], spec))

    def test_unknown_page_cannot_satisfy_a_page_constraint(self):
        rows = [row("unknown", "purchased", "mobile", page=None)]
        spec = analyze.normalize_slice({"page": "/p"})
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
        self.assertEqual(self.label({"page": "/p"}), "/p")
        self.assertEqual(
            self.label({"outcome": ["checkout"], "device": ["mobile"], "page": "/p"}),
            "reached checkout · mobile · /p",
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
        self.assertNotEqual(
            analyze.cut_key({"page": "/p"}),
            analyze.cut_key({"page": "/products/cream"}),
        )

    def test_key_survives_a_json_round_trip(self):
        key = analyze.cut_key({"outcome": "purchased"})
        self.assertEqual(json.loads(key)["filter"], {"outcome": ["purchased"]})


class TestCutKeyPromptVersion(unittest.TestCase):
    """A cut's answer depends on what the model was asked, not only on which
    sessions were selected. Keying on the filter alone meant a reworded
    CUT_PROMPT kept serving answers from the old one, with no invalidation
    path and no signal — the response still said `cached: true`."""

    def setUp(self):
        self._prompt = analyze.CUT_PROMPT
        self.addCleanup(setattr, analyze, "CUT_PROMPT", self._prompt)

    def key(self, spec=None):
        return analyze.cut_key(spec or {"outcome": "purchased"})

    def test_editing_the_prompt_changes_the_key(self):
        before = self.key()
        analyze.CUT_PROMPT = self._prompt + "\n\nAlways mention shipping."
        self.assertNotEqual(before, self.key())

    def test_reverting_the_prompt_restores_the_key(self):
        """A revert should reuse the cache it had, not pay for it again."""
        before = self.key()
        analyze.CUT_PROMPT = "something else entirely"
        analyze.CUT_PROMPT = self._prompt
        self.assertEqual(before, self.key())

    def test_the_prompt_hash_does_not_collapse_distinct_filters(self):
        self.assertNotEqual(self.key({"outcome": "purchased"}), self.key({"outcome": "rest"}))

    def test_whitespace_in_the_prompt_still_counts_as_a_change(self):
        """Content-addressed, so there is no judgement call about what counts
        as a meaningful edit."""
        before = self.key()
        analyze.CUT_PROMPT = self._prompt + " "
        self.assertNotEqual(before, self.key())

    def test_the_key_carries_the_hash_where_it_can_be_read(self):
        payload = json.loads(self.key())
        self.assertEqual(payload["prompt"], analyze.prompt_version(analyze.CUT_PROMPT))
        self.assertEqual(len(payload["prompt"]), 8)

    def test_a_key_from_before_this_change_no_longer_matches(self):
        """Cuts cached without a prompt hash are simply re-asked once."""
        legacy = json.dumps(
            {"filter": analyze.normalize_slice({"outcome": "purchased"})},
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertNotEqual(legacy, self.key())


class TestPromptVersion(unittest.TestCase):
    def test_it_is_stable_and_content_addressed(self):
        self.assertEqual(analyze.prompt_version("abc"), analyze.prompt_version("abc"))
        self.assertNotEqual(analyze.prompt_version("abc"), analyze.prompt_version("abd"))

    def test_it_is_short_enough_to_read_in_a_key(self):
        self.assertEqual(len(analyze.prompt_version("anything")), 8)


class TestNormalizeCutAnalysis(unittest.TestCase):
    def test_keeps_the_documented_shape_and_drops_junk(self):
        out = analyze.normalize_cut_analysis(
            {
                "headline": "  Buyers switch off the default supply size.  ",
                "journey": [
                    {
                        "observation": "Landed on /p at [00:02] and scrolled to the FAQ",
                        "share": "44% (4 of 9)",
                        "action": "Move the guarantee above the fold",
                    },
                    "",
                    12,
                    None,
                ],
                "attention": [{"observation": "40s on the guarantee at [01:10]", "share": "56%"}],
                "intent": [{"observation": "Tapped 'Add to cart' at [00:44]"}],
                "blockers": [
                    {
                        "observation": "Cart icon at [00:17] does nothing",
                        "share": "3 of 9",
                        "action": "Fix the cart icon on mobile",
                    }
                ],
                "caveats": ["only 2 sessions"],
                "extra_key": "ignored",
            }
        )
        self.assertEqual(out["headline"], "Buyers switch off the default supply size.")
        self.assertEqual(
            out["journey"],
            [
                {
                    "observation": "Landed on /p at [00:02] and scrolled to the FAQ",
                    "share": "44% (4 of 9)",
                    "action": "Move the guarantee above the fold",
                }
            ],
        )
        # Absent share/action become empty strings, never missing keys.
        self.assertEqual(out["attention"][0]["action"], "")
        self.assertEqual(out["intent"][0]["share"], "")
        self.assertEqual(out["blockers"][0]["action"], "Fix the cart icon on mobile")
        # Caveats stay plain strings.
        self.assertEqual(out["caveats"], ["only 2 sessions"])
        # No top-level recommendations → fall back to unique item actions.
        self.assertEqual(
            out["recommendations"],
            ["Move the guarantee above the fold", "Fix the cart icon on mobile"],
        )
        self.assertNotIn("extra_key", out)

    def test_recommendations_are_kept_at_the_top(self):
        out = analyze.normalize_cut_analysis(
            {
                "recommendations": [
                    "  Test 30-Night as the default  ",
                    "",
                    {"action": "Lead Meta creative with the guarantee"},
                    "Test 30-Night as the default",  # dedupe
                    12,
                    None,
                ],
                "journey": [
                    {
                        "observation": "Switched supply at [00:40]",
                        "action": "Should not replace the explicit list",
                    }
                ],
            }
        )
        self.assertEqual(
            out["recommendations"],
            [
                "Test 30-Night as the default",
                "Lead Meta creative with the guarantee",
            ],
        )

    def test_recommendations_fall_back_to_item_actions_when_absent(self):
        """Cached cuts from before the field still get a punch list."""
        out = analyze.normalize_cut_analysis(
            {
                "journey": [{"observation": "a at [00:01]", "action": "Do A"}],
                "attention": [{"observation": "b at [00:02]", "action": "Do A"}],
                "intent": [{"observation": "c at [00:03]", "action": "Do B"}],
                "blockers": [{"observation": "d at [00:04]"}],
            }
        )
        self.assertEqual(out["recommendations"], ["Do A", "Do B"])

    def test_every_insight_item_has_all_three_fields(self):
        """The renderer reads .observation/.share/.action without guarding."""
        out = analyze.normalize_cut_analysis(
            {key: [{"observation": "x at [00:01]"}] for key in analyze.CUT_INSIGHT_SECTIONS}
        )
        for key in analyze.CUT_INSIGHT_SECTIONS:
            self.assertEqual(set(out[key][0]), {"observation", "share", "action"}, key)
        self.assertEqual(out["recommendations"], [])

    def test_an_item_with_no_observation_is_dropped(self):
        out = analyze.normalize_cut_analysis(
            {"journey": [{"share": "44%", "action": "do a thing"}, {"observation": "  "}]}
        )
        self.assertEqual(out["journey"], [])

    def test_a_bare_string_is_accepted_as_the_observation(self):
        """The model sometimes falls back to the old flat shape."""
        out = analyze.normalize_cut_analysis({"attention": ["40s on the guarantee"]})
        self.assertEqual(
            out["attention"],
            [{"observation": "40s on the guarantee", "share": "", "action": ""}],
        )

    def test_every_documented_section_is_present(self):
        out = analyze.normalize_cut_analysis({})
        self.assertEqual(out["recommendations"], [])
        for key in analyze.CUT_SECTIONS:
            self.assertEqual(out[key], [], key)

    def test_garbage_becomes_an_empty_analysis(self):
        for junk in (None, [], "text", {"headline": 42}):
            out = analyze.normalize_cut_analysis(junk)
            self.assertEqual(out["headline"], "")
            self.assertEqual(out["recommendations"], [])
            for key in analyze.CUT_SECTIONS:
                self.assertEqual(out[key], [], key)

    def test_long_lists_and_strings_are_capped(self):
        out = analyze.normalize_cut_analysis(
            {
                "journey": [{"observation": f"item {i} at [00:0{i%10}]"} for i in range(50)],
                "recommendations": [f"rec {i}" for i in range(50)],
                "headline": "x" * 900,
            }
        )
        self.assertEqual(len(out["journey"]), analyze.CUT_ITEMS_PER_SECTION)
        self.assertEqual(len(out["recommendations"]), analyze.CUT_RECOMMENDATIONS_CAP)
        self.assertEqual(len(out["headline"]), 600)

    def test_overlong_fields_are_truncated_not_dropped(self):
        out = analyze.normalize_cut_analysis(
            {
                "journey": [
                    {
                        "observation": "o" * 2000,
                        "share": "s" * 200,
                        "action": "a" * 2000,
                    }
                ]
            }
        )
        item = out["journey"][0]
        self.assertEqual(len(item["observation"]), 900)
        self.assertEqual(len(item["share"]), 60)
        self.assertEqual(len(item["action"]), 400)

    def test_junk_entries_do_not_eat_the_item_budget(self):
        """A list of non-items must not trip the cap before real ones land."""
        items = [None] * 20 + [{"observation": "a real item with [00:05]"}]
        out = analyze.normalize_cut_analysis({"blockers": items})
        self.assertEqual(len(out["blockers"]), 1)
        self.assertEqual(out["blockers"][0]["observation"], "a real item with [00:05]")


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
            return analyze.empty_cut_analysis(), {}

        analyze.call_gemini_cut = fake
        rows = [row("a", "purchased"), row("b", "purchased")]
        out = asyncio.run(analyze.build_cut_analysis(rows, {"outcome": "checkout"}))

        self.assertEqual(calls, [], "no sessions match — nothing to read")
        self.assertEqual(out["n"], 0)
        self.assertIn("No analyzed sessions", out["analysis"]["headline"])

    def test_a_filtered_group_calls_the_model_and_records_membership(self):
        seen = {}

        async def fake(rows, label, given="", spec=None):
            seen["rows"] = [r["session_id"] for r in rows]
            seen["label"] = label
            seen["given"] = given
            return analyze.normalize_cut_analysis({"headline": "they hesitate"}), {"1": "p1"}

        analyze.call_gemini_cut = fake
        rows = [row("p1", "purchased"), row("n1"), row("n2")]
        out = asyncio.run(analyze.build_cut_analysis(rows, {"outcome": "purchased"}))

        self.assertEqual(seen["rows"], ["p1"])
        self.assertEqual(seen["label"], "purchased")
        self.assertEqual(out["n"], 1)
        self.assertEqual(out["session_ids"], ["p1"])
        # The map the model was shown is stored, not rebuilt from the rows.
        self.assertEqual(out["citations"], {"1": "p1"})
        self.assertEqual(out["analysis"]["headline"], "they hesitate")
        self.assertEqual(out["cut"], analyze.cut_key({"outcome": "purchased"}))

    def test_an_unfiltered_group_reads_the_whole_cohort(self):
        seen = {}

        async def fake(rows, label, given="", spec=None):
            seen["rows"] = [r["session_id"] for r in rows]
            seen["label"] = label
            seen["given"] = given
            return analyze.empty_cut_analysis(), {}

        analyze.call_gemini_cut = fake
        rows = [row("p1", "purchased"), row("n1"), row("n2")]
        out = asyncio.run(analyze.build_cut_analysis(rows, None))

        self.assertEqual(seen["rows"], ["p1", "n1", "n2"])
        # No filter means nothing is banned — but the group size is still
        # stated, so shares cannot borrow a total from the wider cohort.
        self.assertNotIn("Do not report", seen["given"])
        self.assertIn("exactly 3 session(s)", seen["given"])
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["label"], "all sessions")

    def test_side_text_labels_every_session_for_counting(self):
        text, _ = analyze._cut_side_text(
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

    def test_every_clusterable_field_is_stated_on_every_session(self):
        """A missing label would read as a difference and skew the clusters."""
        text, _ = analyze._cut_side_text([row("bare", "rest")], "SESSIONS")
        self.assertIn("device: unknown", text)
        self.assertIn("page: unknown", text)
        self.assertIn("utm_source: none", text)
        self.assertIn("utm_campaign: none", text)

    def test_known_values_still_win_over_the_placeholders(self):
        text, _ = analyze._cut_side_text(
            [
                row(
                    "s1",
                    "purchased",
                    "desktop",
                    page="/p",
                    utm_source="google",
                    utm_campaign="spring",
                )
            ],
            "SESSIONS",
        )
        self.assertIn("device: desktop", text)
        self.assertIn("page: /p", text)
        self.assertIn("utm_source: google", text)
        self.assertIn("utm_campaign: spring", text)
        self.assertNotIn("unknown", text)


class TestGivenFacts(unittest.TestCase):
    """The filter that selected a group must never come back as a finding."""

    def given(self, spec, n=9):
        return analyze._given_facts(analyze.normalize_slice(spec), n)

    def test_outcome_filter_bans_the_obvious_restatement(self):
        text = self.given({"outcome": ["purchased"]})
        self.assertIn("All 9 session(s) in this group purchased", text)
        # The exact tautology the first version of this prompt produced.
        self.assertIn("clicking buy", text)

    def test_every_premise_carries_the_group_size(self):
        """The bug: a bare "every session" let the model attach the cohort total.

        Each premise states n, so the only session count in context is right.
        """
        text = self.given({"page": "/products/cream", "device": ["mobile"]}, n=9)
        self.assertIn("This group contains exactly 9 session(s)", text)
        for line in text.splitlines():
            if "in this group" in line:
                self.assertIn("All 9 session(s)", line)

    def test_the_group_size_is_declared_even_with_no_filter(self):
        """An unfiltered cut still needs a denominator — that is where 46 came from."""
        text = analyze._given_facts({}, 9)
        self.assertIn("exactly 9 session(s)", text)
        self.assertIn("ONLY total", text)
        # Nothing is pinned, so nothing is banned from being reported.
        self.assertNotIn("cannot separate", text)

    def test_campaign_and_device_filters_are_declared(self):
        text = self.given({"utm_campaign": "spring", "device": ["mobile"]})
        self.assertIn("utm_campaign=spring", text)
        self.assertIn("were on mobile", text)

    def test_page_filter_is_declared(self):
        text = self.given({"page": "/products/cream"})
        self.assertIn("landed on /products/cream", text)

    def test_an_unconstrained_cut_declares_only_the_size(self):
        """Nothing is banned, but the denominator is still stated."""
        text = analyze._given_facts({}, 9)
        self.assertNotIn("Do not report", text)
        self.assertIn("exactly 9 session(s)", text)

    def test_points_clustering_at_the_dimensions_still_free(self):
        """A filtered field is constant, so it cannot separate two clusters."""
        text = self.given({"outcome": ["purchased"]})
        self.assertIn("cannot separate", text)
        self.assertIn("device", text)
        self.assertIn("landing page", text)
        self.assertIn("traffic source", text)
        self.assertIn("campaign", text)

    def test_a_filtered_dimension_is_not_offered_for_clustering(self):
        text = self.given({"device": ["mobile"]})
        cluster_line = next(ln for ln in text.splitlines() if "cannot separate" in ln)
        self.assertNotIn("device", cluster_line)
        self.assertIn("outcome", cluster_line)
        self.assertIn("landing page", cluster_line)

    def test_a_fully_constrained_cut_offers_no_free_dimensions(self):
        text = self.given(
            {
                "outcome": ["purchased"],
                "device": ["mobile"],
                "page": "/p",
                "utm_source": "google",
                "utm_campaign": "spring",
            }
        )
        self.assertNotIn("cannot separate", text)

    def test_build_passes_the_given_facts_to_the_model(self):
        seen = {}

        async def fake(rows, label, given="", spec=None):
            seen["given"] = given
            return analyze.empty_cut_analysis(), {}

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
        # One row matches the filter, so the premise must say 1 — not 2.
        self.assertIn("All 1 session(s) in this group purchased", seen["given"])
        self.assertIn("exactly 1 session(s)", seen["given"])


class TestShareDenominator(unittest.TestCase):
    """A share must count out of the group, not out of the wider cohort.

    The observed bug: a 9-session cut rendered "All 46 sessions landed ...",
    46 being the cohort total. The observation survives; the wrong share does not.
    """

    def norm(self, share, n):
        out = analyze.normalize_cut_analysis(
            {"journey": [{"observation": "They read the FAQ at [00:12].", "share": share}]},
            n,
        )
        return out["journey"][0]

    def test_a_share_matching_the_group_size_is_kept(self):
        self.assertEqual(self.norm("44% (4 of 9)", 9)["share"], "44% (4 of 9)")
        self.assertEqual(self.norm("9 of 9 sessions", 9)["share"], "9 of 9 sessions")

    def test_a_cohort_wide_total_is_dropped(self):
        item = self.norm("All 46 of 46 sessions", 9)
        self.assertEqual(item["share"], "")
        # The finding itself is still worth showing.
        self.assertIn("read the FAQ", item["observation"])

    def test_a_numerator_above_the_group_size_is_dropped(self):
        self.assertEqual(self.norm("12 of 9", 9)["share"], "")

    def test_a_share_with_no_explicit_total_is_left_alone(self):
        """Percent-only shares carry no denominator to check."""
        self.assertEqual(self.norm("44%", 9)["share"], "44%")

    def test_an_unknown_group_size_disables_the_check(self):
        """Cached cuts normalized without n must pass through unchanged."""
        self.assertEqual(self.norm("4 of 46", None)["share"], "4 of 46")

    def test_the_group_size_reaches_the_normalizer(self):
        """len(rows), not the cohort, is what call_gemini_cut validates against."""
        self.assertFalse(analyze._share_denominator_ok("All 46 of 46", 9))
        self.assertTrue(analyze._share_denominator_ok("All 9 of 9", 9))


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

    def test_asks_for_clusters_rather_than_an_average_session(self):
        """A mixed group must split, not come back as 'nothing was similar'."""
        prompt = analyze.CUT_PROMPT
        self.assertIn("clusters", prompt.lower())
        # The specific non-answer this is meant to prevent.
        self.assertIn('Never report "no common behaviour"', prompt)
        self.assertIn("say HOW they split", prompt)

    def test_names_the_metadata_dimensions_to_cluster_on(self):
        prompt = analyze.CUT_PROMPT
        for dimension in ("outcome", "device", "page", "utm_source", "utm_campaign"):
            self.assertIn(dimension, prompt, dimension)
        # Naming the split must not become a way to restate the filter.
        self.assertIn("State the behavioural difference, never the label", prompt)

    def test_a_minority_cluster_is_not_demoted_to_an_anecdote(self):
        prompt = analyze.CUT_PROMPT
        self.assertIn("is a real pattern, not an anecdote", prompt)

    def test_pins_the_citation_format_the_ui_links(self):
        """S<n> was the model's own invention; the UI now parses it, so it is pinned."""
        prompt = analyze.CUT_PROMPT
        self.assertIn("S<N>", prompt)
        self.assertIn("S4 [01:09]", prompt)
        # The ordinal must be the write-up's, not a number the model chose.
        self.assertIn("Never invent one", prompt)
        # Collapse the prompt's own wrapping before matching a sentence.
        flat = " ".join(prompt.split())
        self.assertIn("never cite a number above the group size", flat)
        # The near-miss formats the UI regex would not match.
        self.assertIn('never "Session 4", "#4", or "S4 at 1:09"', flat)

    def test_the_citation_map_is_the_numbering_the_model_was_shown(self):
        """The map is emitted by the loop that writes the headers, not rebuilt."""
        rows = [row("a", "purchased"), row("b", "rest"), row("c", "rest")]
        text, citations = analyze._cut_side_text(rows, "SESSIONS")
        self.assertEqual(citations, {"1": "a", "2": "b", "3": "c"})
        # Every number in the map must appear as that session's header.
        for n, sid in citations.items():
            self.assertIn(f"--- SESSIONS session {n} (", text)
            self.assertIn(f"write-up for {sid}", text)

    def test_reordering_rows_moves_the_map_with_them(self):
        """The bug this design removes: numbering and ids drifting apart."""
        rows = [row("a"), row("b"), row("c")]
        _, forward = analyze._cut_side_text(rows, "SESSIONS")
        _, reversed_ = analyze._cut_side_text(list(reversed(rows)), "SESSIONS")
        self.assertEqual(forward, {"1": "a", "2": "b", "3": "c"})
        # Not {"1": "a"} — the map follows the order the model actually saw.
        self.assertEqual(reversed_, {"1": "c", "2": "b", "3": "a"})

    def test_an_empty_group_has_an_empty_map(self):
        text, citations = analyze._cut_side_text([], "SESSIONS")
        self.assertEqual(citations, {})
        self.assertIn("no sessions in this group", text)

    def test_is_written_for_a_marketing_audience(self):
        prompt = analyze.CUT_PROMPT
        self.assertIn("MARKETING", prompt)
        self.assertIn('answer "so what?"', prompt)
        # The engineer-facing framing this replaced must not creep back.
        self.assertNotIn("a designer or developer could go change tomorrow", prompt)

    def test_asks_for_shares_as_percentages(self):
        prompt = analyze.CUT_PROMPT
        self.assertIn("44% (4 of 9)", prompt)
        self.assertIn("Marketers think in shares", prompt)

    def test_requires_an_action_per_item(self):
        prompt = analyze.CUT_PROMPT
        self.assertIn('"action"', prompt)
        # Non-actions that would make the field worthless.
        self.assertIn('Never "investigate further" or "monitor this"', prompt)

    def test_allows_hedged_inference_but_not_asserted_mind_reading(self):
        """Marketing insight needs interpretation; it still must be marked."""
        prompt = analyze.CUT_PROMPT
        self.assertIn("suggests", prompt)
        self.assertIn("may indicate", prompt)
        self.assertIn("NOT OK", prompt)
        # The observed/inferred split has to stay explicit.
        self.assertIn("Keep observation and interpretation separate", prompt)

    def test_asks_for_the_object_item_shape(self):
        prompt = analyze.CUT_PROMPT
        self.assertIn("never a bare string", prompt)
        for field in ('"observation"', '"share"', '"action"'):
            self.assertIn(field, prompt)

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
        self.assertIn('"recommendations"', prompt)
        for key in analyze.CUT_SECTIONS:
            self.assertIn(f'"{key}"', prompt)
        # Nothing should still be asking the model to contrast two sides.
        self.assertNotIn("a_distinctive", prompt)
        self.assertNotIn("b_distinctive", prompt)
        self.assertNotIn("Group B", prompt)


class TestCutCache(pgfixture.CohortStoreMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
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

    def test_clearing_a_cut_frees_it_to_be_asked_again(self):
        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(self.pool, "cohort-1", key, {"n": 3})
        self.assertTrue(analyze.clear_cut(self.pool, "cohort-1", key))
        self.assertIsNone(analyze.read_cut(self.pool, "cohort-1", key))

    def test_clearing_one_cut_leaves_its_neighbours_cached(self):
        k1 = analyze.cut_key({"outcome": "purchased"})
        k2 = analyze.cut_key({"utm_source": "meta"})
        analyze.write_cut(self.pool, "cohort-1", k1, {"n": 1})
        analyze.write_cut(self.pool, "cohort-1", k2, {"n": 2})
        analyze.clear_cut(self.pool, "cohort-1", k1)
        self.assertIsNone(analyze.read_cut(self.pool, "cohort-1", k1))
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", k2)["n"], 2)

    def test_clearing_a_cut_that_was_never_cached_is_a_no_op(self):
        key = analyze.cut_key({"outcome": "purchased"})
        self.assertFalse(analyze.clear_cut(self.pool, "cohort-1", key))
        self.assertFalse(analyze.clear_cut(self.pool, "../../etc/passwd", key))

    def test_a_cleared_cut_can_be_rewritten(self):
        key = analyze.cut_key({"outcome": "purchased"})
        analyze.write_cut(self.pool, "cohort-1", key, {"n": 3})
        analyze.clear_cut(self.pool, "cohort-1", key)
        analyze.write_cut(self.pool, "cohort-1", key, {"n": 4})
        self.assertEqual(analyze.read_cut(self.pool, "cohort-1", key)["n"], 4)


def split_rows(converting=8, flat=8, untagged=0):
    """A group that splits hard on source: meta buys, google does not."""
    rows = [
        row(
            f"m{i}",
            "purchased" if i < converting - 3 else "rest",
            device="mobile",
            page="/p/cream",
            utm_source="meta",
        )
        for i in range(converting)
    ]
    rows += [
        row(f"g{i}", "rest", device="desktop", page="/p/cream", utm_source="google")
        for i in range(flat)
    ]
    rows += [row(f"u{i}", "rest", device="mobile", page="/p/other") for i in range(untagged)]
    return rows


class TestCrossTabFloor(unittest.TestCase):
    """The floor is what keeps a two-session fluke out of a marketing deck."""

    def test_a_real_split_is_reported_with_exact_counts(self):
        block = analyze.cross_tab_block(split_rows(), {})
        self.assertIn("traffic source × outcome", block)
        self.assertIn("meta: 8 sessions", block)
        self.assertIn("google: 8 sessions", block)

    def test_a_cell_below_the_session_floor_is_suppressed(self):
        # Two meta sessions cannot be a pattern however lopsided they look.
        rows = [row(f"g{i}", "rest", device="desktop", utm_source="google") for i in range(10)]
        rows += [row(f"m{i}", "purchased", device="mobile", utm_source="meta") for i in range(2)]
        block = analyze.cross_tab_block(rows, {})
        self.assertNotIn("meta:", block)

    def test_a_cell_below_the_coverage_floor_is_suppressed(self):
        # 3 sessions clears the count floor but is only 8% of the group.
        rows = [row(f"g{i}", "rest", device="desktop", utm_source="google") for i in range(35)]
        rows += [row(f"m{i}", "purchased", device="mobile", utm_source="meta") for i in range(3)]
        block = analyze.cross_tab_block(rows, {})
        self.assertNotIn("meta:", block)

    def test_a_big_cell_at_the_baseline_is_suppressed(self):
        # Half the group, but converting exactly like everyone else.
        rows = [
            row(f"m{i}", "purchased" if i % 2 else "rest", device="mobile", utm_source="meta")
            for i in range(10)
        ]
        rows += [
            row(f"g{i}", "purchased" if i % 2 else "rest", device="desktop", utm_source="google")
            for i in range(10)
        ]
        block = analyze.cross_tab_block(rows, {})
        self.assertNotIn("traffic source × outcome", block.split("Examined")[0])

    def test_suppressed_cells_are_counted_not_hidden(self):
        block = analyze.cross_tab_block(split_rows(untagged=4), {})
        self.assertIn("Examined and not reported:", block)

    def test_coverage_is_stated_when_attribution_is_sparse(self):
        # The shape of real dev data: a handful of tagged sessions in a big pool.
        rows = [row(f"n{i}", "rest") for i in range(36)]
        rows += [row(f"m{i}", "purchased", utm_source="meta") for i in range(4)]
        block = analyze.cross_tab_block(rows, {})
        self.assertIn("4 of 40 sessions carry a utm_source", block)
        self.assertNotIn("meta:", block)

    def test_a_group_smaller_than_the_floor_gets_no_block(self):
        self.assertEqual(analyze.cross_tab_block([row("a"), row("b")], {}), "")

    def test_an_empty_group_gets_no_block(self):
        self.assertEqual(analyze.cross_tab_block([], {}), "")


class TestCrossTabDimensions(unittest.TestCase):
    def test_a_filtered_dimension_is_never_an_axis(self):
        # device is constant under this filter, so it cannot separate anything.
        block = analyze.cross_tab_block(split_rows(), {"device": ["mobile"]})
        self.assertNotIn("device", block)

    def test_filtering_on_source_drops_every_source_pairing(self):
        block = analyze.cross_tab_block(split_rows(), {"utm_source": "meta"})
        self.assertNotIn("traffic source", block)

    def test_an_fbclid_only_session_counts_as_meta(self):
        rows = [row(f"f{i}", "purchased", fbclid=True) for i in range(6)]
        rows += [row(f"g{i}", "rest", utm_source="google") for i in range(6)]
        block = analyze.cross_tab_block(rows, {})
        self.assertIn("meta (fbclid)", block)

    def test_unknown_is_not_treated_as_a_segment(self):
        # Untagged sessions must not clump into a "none" cell that reads as a
        # channel: direct, organic and untagged are not the same thing.
        rows = [row(f"u{i}", "purchased" if i < 6 else "rest") for i in range(12)]
        block = analyze.cross_tab_block(rows, {})
        self.assertNotIn("none", block.lower().split("examined")[0])

    def test_the_block_states_that_its_numbers_win(self):
        block = analyze.cross_tab_block(split_rows(), {})
        self.assertIn("EXACT", block)


class TestCrossTabSingleOutcomeGroups(unittest.TestCase):
    """A purchased-only cut has no purchase rate to grade lift against."""

    def uniform(self):
        return [
            row(
                f"s{i}",
                "purchased",
                device="mobile" if i < 6 else "desktop",
                page="/p",
                utm_source="meta" if i < 6 else "google",
            )
            for i in range(12)
        ]

    def test_a_real_split_survives_when_outcome_is_pinned(self):
        # Lift is 0 for every cell here by construction; grading on it would
        # have hidden a clean meta/mobile vs google/desktop split.
        block = analyze.cross_tab_block(self.uniform(), {"outcome": ["purchased"]})
        self.assertIn("meta: 6 sessions", block)
        self.assertIn("google: 6 sessions", block)

    def test_no_purchase_rate_is_quoted_for_a_uniform_group(self):
        block = analyze.cross_tab_block(self.uniform(), {"outcome": ["purchased"]})
        self.assertNotIn("purchased ·", block)
        self.assertNotIn("pts vs baseline", block)

    def test_the_header_explains_why_there_is_no_baseline(self):
        block = analyze.cross_tab_block(self.uniform(), {"outcome": ["purchased"]})
        self.assertIn("shares one outcome", block)

    def test_a_mixed_group_still_grades_on_lift(self):
        block = analyze.cross_tab_block(split_rows(), {})
        self.assertIn("pts vs baseline", block)
        self.assertIn("group baseline", block)

    def test_size_gates_still_apply_without_lift(self):
        rows = [row(f"s{i}", "purchased", utm_source="meta") for i in range(10)]
        rows += [row("odd", "purchased", utm_source="google")]
        block = analyze.cross_tab_block(rows, {"outcome": ["purchased"]})
        self.assertNotIn("google", block.split("Examined")[0])


class TestPromptsTeachTheMeasuredBlock(unittest.TestCase):
    """Computed numbers are only useful if the prompts defer to them."""

    def test_the_cut_prompt_makes_the_block_authoritative(self):
        self.assertIn("Measured splits", analyze.CUT_PROMPT)
        self.assertIn("exact", analyze.CUT_PROMPT.lower())

    def test_the_cut_prompt_asks_for_combinations(self):
        self.assertIn("×", analyze.CUT_PROMPT)
        self.assertIn("Cross the dimensions", analyze.CUT_PROMPT)

    def test_the_cut_prompt_still_demands_page_evidence_for_a_split(self):
        # A measured row quoted without write-up evidence is the failure mode
        # the block could otherwise introduce.
        self.assertIn("WHAT differs", analyze.CUT_PROMPT)

    def test_the_chat_prompt_defers_to_the_block(self):
        self.assertIn("Measured splits", analyze.CHAT_PROMPT)

    def test_the_chat_prompt_no_longer_forbids_every_precise_metric(self):
        # It must still refuse numbers that are in neither the write-ups nor
        # the block, but the block is now a legitimate source.
        self.assertIn("Measured splits", analyze.CHAT_PROMPT.split("Counting")[0])


class TestCrossTabReachesBothReaders(unittest.TestCase):
    """The cut and the chat must see identical measured splits."""

    def test_the_block_is_in_the_shared_context_header(self):
        rows = split_rows()
        header, _, _ = analyze.cut_context(rows, "all sessions", "", {})
        self.assertIn("Measured splits", header)
        self.assertIn("meta: 8 sessions", header)

    def test_cut_and_chat_headers_match(self):
        # Both readers build the header the same way from the same spec, so a
        # chat can never answer over a thinner view than the cut on screen.
        rows = split_rows()
        slice_spec = {"page": "/p/cream"}
        seen = {}

        async def fake_cut(rows_, label_, given_="", spec_=None):
            seen["cut"] = analyze.cut_context(rows_, label_, given_, spec_)[0]
            return analyze.empty_cut_analysis(), {}

        saved_call, saved_enabled = analyze.call_gemini_cut, analyze.ANALYSIS_ENABLED
        analyze.call_gemini_cut, analyze.ANALYSIS_ENABLED = fake_cut, True
        try:
            asyncio.run(analyze.build_cut_analysis(rows, slice_spec))
            selected, label, given, _, spec = asyncio.run(analyze.chat_context(rows, slice_spec))
        finally:
            analyze.call_gemini_cut = saved_call
            analyze.ANALYSIS_ENABLED = saved_enabled

        seen["chat"] = analyze.cut_context(selected, label, given, spec)[0]
        self.assertEqual(seen["cut"], seen["chat"])
        self.assertIn("Measured splits", seen["chat"])
        self.assertIn("meta: 8 sessions", seen["chat"])


if __name__ == "__main__":
    unittest.main()
