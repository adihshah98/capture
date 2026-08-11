"""The slice language: the {outcome, device, page, utm_*} filter over sessions.

One vocabulary shared by three callers that must agree exactly -- the cohort
sampler picks rows with it, the cut cache keys on its normal form, and the UI
labels chips from it. When they disagree the symptom is silent: a cut whose
label says one thing and whose evidence is another.

So normalisation is the contract, not a convenience. `normalize_slice` maps
every accepted spelling onto one canonical form, which is what makes
`cut_key` stable across value order and letter case, and what lets
`row_matches_slice` be a plain predicate over an already-canonical spec.

Pure functions over dicts: no I/O, no database, no module state. That is what
makes the sampler's predicates testable without a pool and, eventually,
translatable into SQL (see the sampling-shortfall note in the refactor doc).
"""

import re
import urllib.parse
from typing import Optional

META_SOURCES = frozenset({"facebook", "meta", "fb", "instagram", "ig"})

# Same threshold the session detail panel uses, so one session is never called
# mobile in one place and desktop in another.
MOBILE_MAX_WIDTH = 768
DEVICE_VALUES = ("mobile", "desktop")

# purchased ⊃ checkout: a purchase implies checkout was reached, so "checkout"
# here means reached checkout and did NOT purchase — the three are disjoint.
OUTCOME_VALUES = ("purchased", "checkout", "rest")


def device_class(device: Optional[dict]) -> Optional[str]:
    """mobile/desktop from the indexed viewport, or None when unknown."""
    if not isinstance(device, dict):
        return None
    width = device.get("width")
    if not isinstance(width, int) or width <= 0:
        return None
    return "mobile" if width < MOBILE_MAX_WIDTH else "desktop"


def outcome_class(order: Optional[dict]) -> str:
    """purchased | checkout (reached, did not buy) | rest."""
    if not isinstance(order, dict):
        return "rest"
    if order.get("purchased_at"):
        return "purchased"
    if order.get("checkout_started_at"):
        return "checkout"
    return "rest"


def is_meta_attribution(attribution: Optional[dict]) -> bool:
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
    return "/".join(
        urllib.parse.unquote(part).replace("/", "%2F") for part in path.split("/")
    )


def page_path(url: Optional[str]) -> Optional[str]:
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


def normalize_slice(spec: Optional[dict]) -> dict:
    """Normalize a slice: any combination of outcome, device, page and attribution.

    Keys: outcome (list of purchased|checkout|rest), device (list of
    mobile|desktop), page (landing path), utm_source, utm_medium, utm_campaign,
    utm_content, fbclid. utm_source="meta" matches Meta traffic (fbclid or a
    known Meta source).

    Constraints are ANDed across keys and ORed within a key, so
    {"outcome": ["purchased", "checkout"], "device": ["mobile"]} reads as
    "reached checkout on mobile, bought or not".

    Selecting every value of a dimension drops the constraint only where the
    dimension is total — see `_multi`.
    """
    if not isinstance(spec, dict):
        return {}
    out = {}

    def _multi(key, allowed, *, total: bool):
        """Clean one multi-valued key.

        `total` says whether every session has a value here. Selecting every
        value is the same as no constraint only when it is: outcome_class()
        always returns one of its three, but device_class() returns None for an
        unindexed viewport, and row_matches_slice refuses those unknowns
        against any device constraint. Collapsing "mobile or desktop" to no
        filter would therefore widen the selection to include them.
        """
        raw = spec.get(key)
        if raw is None or raw == "":
            return
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        clean = []
        for value in values:
            text = str(value or "").strip().lower()
            # "all" is the legacy spelling of "everyone who did not purchase".
            if key == "outcome" and text == "all":
                for legacy in ("checkout", "rest"):
                    if legacy not in clean:
                        clean.append(legacy)
                continue
            if text in allowed and text not in clean:
                clean.append(text)
        if clean and (not total or len(clean) < len(allowed)):
            out[key] = sorted(clean)

    _multi("outcome", OUTCOME_VALUES, total=True)
    _multi("device", DEVICE_VALUES, total=False)

    page = page_path(spec.get("page")) if spec.get("page") not in (None, "") else None
    if page:
        out["page"] = page

    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip().lower()
    if "fbclid" in spec and spec["fbclid"] is not None:
        out["fbclid"] = bool(spec["fbclid"])
    return out


def slice_is_empty(spec: dict) -> bool:
    return not spec


def row_matches_slice(row: dict, spec: dict) -> bool:
    if not spec:
        return True
    want_outcome = spec.get("outcome")
    if want_outcome and (row.get("outcome") or "rest") not in want_outcome:
        return False
    want_device = spec.get("device")
    if want_device:
        # Unknown device can't satisfy a device constraint.
        if (row.get("device") or "") not in want_device:
            return False
    want_page = spec.get("page")
    if want_page:
        # Unknown / missing landing page can't satisfy a page constraint.
        if (row.get("page") or "") != want_page:
            return False
    attrs = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
    if "fbclid" in spec:
        has = bool(attrs.get("fbclid"))
        if spec["fbclid"] and not has:
            return False
        if not spec["fbclid"] and has:
            return False
    for key in ("utm_medium", "utm_campaign", "utm_content"):
        want = spec.get(key)
        if not want:
            continue
        got = str(attrs.get(key) or "").strip().lower()
        if got != want:
            return False
    want_source = spec.get("utm_source")
    if want_source:
        if want_source == "meta":
            if not is_meta_attribution(attrs):
                return False
        elif want_source in ("google", "non_meta"):
            if want_source == "non_meta":
                if is_meta_attribution(attrs):
                    return False
            else:
                got = str(attrs.get("utm_source") or "").strip().lower()
                if got != want_source:
                    return False
        else:
            got = str(attrs.get("utm_source") or "").strip().lower()
            if got != want_source:
                return False
    return True


OUTCOME_LABELS = {
    "purchased": "purchased",
    "checkout": "reached checkout",
    "rest": "did not reach checkout",
}


def slice_label(spec: dict) -> str:
    if not spec:
        return "everyone else"
    parts = []

    outcome = spec.get("outcome") or []
    if outcome:
        # The common "bought or not, but got to checkout" pair reads badly as
        # a raw join, so name the union directly.
        if set(outcome) == {"purchased", "checkout"}:
            parts.append("reached checkout")
        elif set(outcome) == {"checkout", "rest"}:
            parts.append("did not purchase")
        else:
            parts.append(" or ".join(OUTCOME_LABELS[o] for o in outcome))

    device = spec.get("device") or []
    if device:
        parts.append(" or ".join(device))

    if spec.get("page"):
        parts.append(spec["page"])

    if spec.get("utm_source") == "meta" or spec.get("fbclid") is True:
        parts.append("Meta")
    elif spec.get("utm_source") == "non_meta":
        parts.append("non-Meta")
    elif spec.get("utm_source"):
        parts.append(spec["utm_source"])
    if spec.get("fbclid") is False and spec.get("utm_source") != "non_meta":
        parts.append("no fbclid")
    for key, label in (
        ("utm_medium", "medium"),
        ("utm_campaign", "campaign"),
        ("utm_content", "content"),
    ):
        if spec.get(key):
            parts.append(f"{label}={spec[key]}")
    if not parts:
        return "all sessions"
    return " · ".join(parts)
