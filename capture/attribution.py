"""Where a visitor came from, read off their entry URL.

One parser, deliberately. There were two: `server.parse_attribution` for the
index and `analyze._session_attribution` for the prompt metadata, kept in step by
a comment saying "change both together". They diverged anyway -- which is the
whole argument for this module existing rather than the comment being reworded.

What had drifted by the time they were merged:

  * `utm_term` was captured by analyze and not by the indexer, so a session that
    arrived on a paid search term had it in the model's context and not in the
    column that slicing and cut-matching read.
  * empty values were dropped by one (`if params.get(key)`) and kept by the other
    (`if key not in params`), so `?utm_source=0` disagreed.
  * one returned `{}` for "nothing found" and the other `None`.

The union of the keys is what ships: `utm_term` because the write-up prompt uses
it, `fbclid` because the slice language filters on it. Both callers needed both;
neither had both.

Note what this does *not* fix. `sessions.attribution` is written once at ingest
under `coalesce(sessions.attribution, excluded.attribution)` -- first write wins,
permanently -- so rows indexed before this module was introduced keep whatever the
old parser found. Correcting them means re-reading `sessions.url` and rewriting
the column; see capture/backfill.py.
"""

import re
import urllib.parse

# utm_term is analysis-only today (it reaches the write-up prompt, and no slice
# filters on it) but it is captured for everyone: the cost is one dict key, and
# the alternative is a second key list that drifts from this one.
UTM_PARAMS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")

# Some ad platforms hand back "#utm_source=..." rather than a query string, so the
# fragment is parsed too -- but only when it looks like parameters rather than a
# client-side route ("#/cart").
_FRAGMENT_IS_PARAMS = re.compile(r"^[^/]+=")

# Long enough for a real campaign name, short enough that a junk URL cannot bloat
# a row. Applies to values only; `fbclid` is stored as a flag, not a value.
MAX_VALUE_CHARS = 256


def _params(url: str) -> dict:
    """Query-string parameters, with the fragment as a fallback source."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return {}

    found: dict = {}
    for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=False).items():
        if values and values[0]:
            found[key] = values[0]

    fragment = parsed.fragment or ""
    if fragment and _FRAGMENT_IS_PARAMS.match(fragment):
        for key, values in urllib.parse.parse_qs(fragment, keep_blank_values=False).items():
            # The query string wins: a platform echoing a param into the fragment
            # must not overwrite what the visitor actually arrived with.
            if key not in found and values and values[0]:
                found[key] = values[0]
    return found


def parse(url: str | None) -> dict:
    """utm_* and an fbclid flag off an entry URL. `{}` when there is nothing.

    Always a dict, so callers can index it without a None check. The indexer wants
    NULL rather than an empty JSON object in the column, so it calls
    `parse_or_none` instead -- that distinction belongs at the storage boundary,
    not in the parsing.
    """
    if not isinstance(url, str) or not url.strip():
        return {}

    params = _params(url)
    out: dict[str, str | bool] = {}
    for key in UTM_PARAMS:
        # Truthiness, not membership: `?utm_source=` carries no more information
        # than no utm_source at all, and storing "" would make it a facet value.
        if params.get(key):
            out[key] = str(params[key])[:MAX_VALUE_CHARS]
    # Stored as a flag: the click id itself is per-user and of no analytical use,
    # but its presence is what `is_meta_attribution` reads.
    if params.get("fbclid"):
        out["fbclid"] = True
    return out


def parse_or_none(url: str | None) -> dict | None:
    """`parse`, but `None` rather than `{}` when nothing was found.

    For the `sessions.attribution` column, where an empty object and SQL NULL are
    different states and only NULL means "this visit carried no campaign".
    """
    return parse(url) or None


# The name the ingest and session routes import. They were written against the
# earlier version of this module, whose single entry point returned None for
# "nothing found" -- which is exactly parse_or_none's contract.
parse_attribution = parse_or_none
