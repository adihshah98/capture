"""One URL → attribution parser.

Pure: urllib and re, nothing else. It lives at the top of the package rather than
inside a router or inside capture/analyze because both sides need it and neither
may import the other — server.py cannot import from the analysis package (that is
what analysis_port.py is for), and the analysis package must stay drivable without
projects or config.

There is a second, divergent copy inside capture/analyze that re-parses `url` at
read time for the prompt metadata. The two genuinely disagree today: a session
that arrived with ?utm_term=… has that term in the prompt but not in the indexed
attribution column that slicing reads. docs/deep-dive/01-ingest-analyze-seam.md
records why, and docs/analyze-design-review.md proposes collapsing them. This
module is the neutral home that makes that possible without either side reaching
across the boundary — but the collapse itself is not done, and doing it means
touching the analysis package.

What ingest writes here is frozen: record_chunk upserts attribution with
coalesce(existing, new), so the first chunk carrying a URL sets it permanently.
Re-parsing later cannot fix an already-indexed session.
"""

import re
import urllib.parse

ATTRIBUTION_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "fbclid")


def parse_attribution(url: str | None) -> dict | None:
    """Pull utm_* / fbclid from an entry URL for indexing and segment rollups."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    params: dict = {}
    for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=False).items():
        if values and values[0]:
            params[key] = values[0]
    # Some storefronts put the campaign in the fragment. Only treat it as a query
    # string when it actually looks like one, or a bare #section becomes a param.
    fragment = parsed.fragment or ""
    if fragment and re.match(r"^[^/]+=", fragment):
        for key, values in urllib.parse.parse_qs(fragment, keep_blank_values=False).items():
            if key not in params and values and values[0]:
                params[key] = values[0]
    out: dict[str, str | bool] = {}
    for key in ATTRIBUTION_KEYS:
        if key not in params:
            continue
        if key == "fbclid":
            # Only its presence matters; the value is an opaque click id.
            out["fbclid"] = True
        else:
            out[key] = str(params[key])[:256]
    return out or None
