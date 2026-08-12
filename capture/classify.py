"""Session class labels shared by ingest and analyze.

Ingest materialises these onto `sessions` so sampling can filter in SQL.
Analyze (cuts, facets, labels) reads the same functions so a cut never
disagrees with the cohort that selected its rows.

Keep this module a leaf: stdlib only. `capture.analyze.slices` re-exports the
matchers that build on these classifiers.
"""

from __future__ import annotations

import re
import urllib.parse

META_SOURCES = frozenset({"facebook", "meta", "fb", "instagram", "ig"})

# Same threshold the session detail panel uses, so one session is never called
# mobile in one place and desktop in another.
MOBILE_MAX_WIDTH = 768
DEVICE_VALUES = ("mobile", "desktop")

# purchased ⊃ checkout: a purchase implies checkout was reached, so "checkout"
# here means reached checkout and did NOT purchase — the three are disjoint.
OUTCOME_VALUES = ("purchased", "checkout", "rest")

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")


def device_class(device: dict | None) -> str | None:
    """mobile/desktop from the indexed viewport, or None when unknown."""
    if not isinstance(device, dict):
        return None
    width = device.get("width")
    if not isinstance(width, int) or width <= 0:
        return None
    return "mobile" if width < MOBILE_MAX_WIDTH else "desktop"


def outcome_class(order: dict | None) -> str:
    """purchased | checkout (reached, did not buy) | rest."""
    if not isinstance(order, dict):
        return "rest"
    if order.get("purchased_at"):
        return "purchased"
    if order.get("checkout_started_at"):
        return "checkout"
    return "rest"


def is_meta_attribution(attribution: dict | None) -> bool:
    attrs = attribution if isinstance(attribution, dict) else {}
    if attrs.get("fbclid"):
        return True
    source = str(attrs.get("utm_source") or "").strip().lower()
    return source in META_SOURCES


# A hostname we would accept on an entry URL that arrived without a scheme.
# Deliberately strict: urlparse will read a host out of almost anything, and
# whatever is not a host is not a landing page.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?::\d{1,5})?$")


def _unquote_segments(path: str) -> str:
    """Percent-decode each segment, keeping encoded separators encoded.

    Decoding the whole path turns %2F into a separator, so `/a%2Fb` — one
    segment containing a slash — would read as the two-segment `/a/b` and merge
    two distinct pages into one facet. Decoding per segment is not enough on
    its own either, since the decoded text can reintroduce the separator, so a
    slash produced by decoding is put back as %2F. The path is only ever used
    as an identity key (facet value, filter match, display label), so keeping
    it unambiguous matters more than showing the prettiest spelling.
    """
    return "/".join(urllib.parse.unquote(part).replace("/", "%2F") for part in path.split("/"))


def page_path(url: str | None) -> str | None:
    """Landing-page path from an entry URL, or None when there is not one.

    Pathname only: no query or hash. Trailing slashes are stripped (except `/`)
    so `/p` and `/p/` share a facet. Query strings stay on attribution — putting
    them here would explode the picker with one option per UTM combo.

    None means unknown, and every consumer already handles it: row_matches_slice
    refuses it against a page constraint, cross-tabs leave it out, and the facet
    picker skips it. So anything unreadable as an http(s) page — junk, "null",
    about:blank, a data: URI — is None rather than a guess. Guessing lands it on
    `/`, where it is indistinguishable from a real homepage visit and gets
    offered to the user as a filter, counted in a measured split, and stated to
    the model as fact.
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("/") and "://" not in raw:
            # Already a path, so there is no host to check.
            path = _unquote_segments(raw.split("?", 1)[0].split("#", 1)[0])
        else:
            bare = "://" not in raw
            parsed = urllib.parse.urlparse(f"https://{raw}" if bare else raw)
            if parsed.scheme not in ("http", "https"):
                return None
            if not parsed.hostname or not _HOSTNAME_RE.match(parsed.netloc):
                return None
            if bare and "." not in parsed.hostname and not parsed.path:
                # A scheme-less single word with no path ("null", "undefined").
                # A real bare host reaching here carries either a dot or a
                # path; "localhost" alone is not an entry URL we can place.
                return None
            # A real host with an empty path is the homepage.
            path = _unquote_segments(parsed.path) or "/"
    except Exception:
        return None
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or None


def _utm_value(raw) -> str | None:
    if raw is None or raw is False:
        return None
    text = str(raw).strip().lower()
    return text or None


def attribution_classes(attribution: dict | None) -> dict:
    """Denormalised campaign columns for `sessions` (lowercased utm_* + flags)."""
    attrs = attribution if isinstance(attribution, dict) else {}
    return {
        "utm_source": _utm_value(attrs.get("utm_source")),
        "utm_medium": _utm_value(attrs.get("utm_medium")),
        "utm_campaign": _utm_value(attrs.get("utm_campaign")),
        "utm_content": _utm_value(attrs.get("utm_content")),
        "has_fbclid": bool(attrs.get("fbclid")),
        "is_meta": is_meta_attribution(attrs),
    }


def session_classes(
    *,
    url: str | None,
    device: dict | None,
    attribution: dict | None,
) -> dict:
    """Columns written with the first chunk: page, device_class, campaign fields."""
    return {
        "device_class": device_class(device),
        "page": page_path(url),
        **attribution_classes(attribution),
    }
