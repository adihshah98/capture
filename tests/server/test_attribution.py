"""The one attribution parser, and the agreement it exists to guarantee.

There were two implementations kept in step by a comment. They drifted: the
indexer dropped `utm_term`, the two disagreed on empty values, and one returned
`{}` where the other returned `None`. The divergence was invisible because each
side was tested against itself.

So the cases below are written as a *comparison* wherever the old parsers
disagreed. A future edit that reintroduces a second code path fails here rather
than in production, where the symptom is a session whose write-up cites a
campaign the cut picker cannot filter on.
"""

import unittest
from pathlib import Path

from capture import attribution


class TestParse(unittest.TestCase):
    def test_utm_params_and_the_fbclid_flag(self):
        self.assertEqual(
            attribution.parse(
                "https://x.test/p?utm_source=google&utm_medium=cpc"
                "&utm_campaign=spring&utm_content=hero&utm_term=shoes&fbclid=abc"
            ),
            {
                "utm_source": "google",
                "utm_medium": "cpc",
                "utm_campaign": "spring",
                "utm_content": "hero",
                "utm_term": "shoes",
                # The id itself is per-user and useless for analysis; its
                # presence is what is_meta_attribution reads.
                "fbclid": True,
            },
        )

    def test_utm_term_is_captured(self):
        """The key the indexer used to drop. A paid-search session had it in the
        prompt and not in the column that slicing reads."""
        self.assertEqual(attribution.parse("https://x.test/p?utm_term=only"), {"utm_term": "only"})

    def test_nothing_found_is_an_empty_dict(self):
        for url in ("https://x.test/p", "", "   ", None, 42):
            with self.subTest(url=url):
                self.assertEqual(attribution.parse(url), {})

    def test_empty_values_are_dropped(self):
        # "" would become a facet value indistinguishable from a real source.
        self.assertEqual(attribution.parse("https://x.test/p?utm_source=&fbclid="), {})

    def test_a_literal_zero_is_a_value(self):
        """The case the two parsers disagreed on: truthiness of the *string*,
        not of the parsed value. "0" is a real, if odd, campaign name."""
        self.assertEqual(attribution.parse("https://x.test/p?utm_source=0")["utm_source"], "0")

    def test_a_parameterised_fragment_is_read(self):
        # Some ad platforms hand back "#utm_source=..." rather than a query.
        self.assertEqual(
            attribution.parse("https://x.test/p#utm_source=meta"), {"utm_source": "meta"}
        )

    def test_a_client_side_route_is_not_read_as_parameters(self):
        self.assertEqual(attribution.parse("https://x.test/p#/cart"), {})

    def test_the_query_string_wins_over_the_fragment(self):
        parsed = attribution.parse("https://x.test/p?utm_source=google#utm_source=meta")
        self.assertEqual(parsed["utm_source"], "google")

    def test_values_are_capped(self):
        long = "x" * 5000
        self.assertEqual(
            len(attribution.parse(f"https://x.test/p?utm_campaign={long}")["utm_campaign"]),
            attribution.MAX_VALUE_CHARS,
        )

    def test_an_unparseable_url_is_not_an_error(self):
        for url in ("not a url", "://", "javascript:void(0)"):
            with self.subTest(url=url):
                self.assertIsInstance(attribution.parse(url), dict)


class TestParseOrNone(unittest.TestCase):
    """The storage boundary: the column wants NULL, not an empty JSON object."""

    def test_nothing_found_is_none(self):
        self.assertIsNone(attribution.parse_or_none("https://x.test/p"))
        self.assertIsNone(attribution.parse_or_none(None))

    def test_a_hit_is_the_same_dict_parse_returns(self):
        url = "https://x.test/p?utm_source=meta"
        self.assertEqual(attribution.parse_or_none(url), attribution.parse(url))


class TestBothCallersAgree(unittest.TestCase):
    """The regression this module exists to prevent.

    Every URL here is one the two old implementations answered differently.

    Checked against the module's two entry points rather than by importing
    capture.server: that module reads GEMINI_API_KEY and CAPTURE_DATABASE_URL at
    import, and pulling it in from a unit test imports it *before* the suites
    that arrange those env vars in setUpClass, which silently disables analysis
    for them. The binding itself -- that both callers use these functions -- is
    what test_no_second_parser_survives covers.
    """

    DIVERGENT = (
        "https://x.test/p?utm_source=google&utm_term=shoes&fbclid=abc",
        "https://x.test/p?utm_term=only",
        "https://x.test/p?fbclid=",
        "https://x.test/p?utm_source=0",
        "https://x.test/p#utm_source=meta",
        "https://x.test/p",
        None,
    )

    def test_the_two_entry_points_read_a_url_the_same_way(self):
        for url in self.DIVERGENT:
            with self.subTest(url=url):
                # The only permitted difference is the empty case: {} vs None,
                # which is the column's NULL and nothing more.
                self.assertEqual(attribution.parse(url), attribution.parse_or_none(url) or {})

    def test_no_second_parser_survives(self):
        """The twins are gone, not merely agreeing. A reintroduced local parser
        is the failure this whole module exists to make impossible."""
        root = Path(__file__).resolve().parent.parent.parent / "capture"
        # The route split moved the parser's callers into capture/api/, so the
        # whole package is swept rather than the two files that used to hold a
        # copy -- a reintroduced parser in a new router would otherwise pass.
        sources = [
            p
            for p in root.rglob("*.py")
            if p.name != "attribution.py" and "__pycache__" not in p.parts
        ]
        self.assertTrue(sources, "found no sources to check -- the path is wrong")
        offenders = []
        for source in sources:
            text = source.read_text()
            # parse_qs is the giveaway: reading params off a URL is this
            # module's job, and no caller has another reason to do it.
            if "parse_qs" in text:
                offenders.append(source.name)
        self.assertEqual(offenders, [], f"these parse URL parameters locally again: {offenders}")


if __name__ == "__main__":
    unittest.main()
