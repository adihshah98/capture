"""The slice language: the {outcome, device, page, utm_*} filter over sessions.

One vocabulary shared by three callers that must agree exactly -- the cohort
sampler picks rows with it (via SQL on columns materialised at ingest), the cut
cache keys on its normal form, and the UI labels chips from it. When they
disagree the symptom is silent: a cut whose label says one thing and whose
evidence is another.

So normalisation is the contract, not a convenience. `normalize_slice` maps
every accepted spelling onto one canonical form, which is what makes
`cut_key` stable across value order and letter case, and what lets
`row_matches_slice` / `_slice_where` be plain predicates over an
already-canonical spec. `slice_label_parts` is the one namer: cuts join it,
cohort auto-names list it, and the Insights picker mirrors it.

Classifiers live in `capture.classify` so ingest can write the same labels
without importing the analyze package. This module re-exports them and owns
the match/normalize/label surface.
"""

from typing import Any

from capture.classify import (
    DEVICE_VALUES,
    META_SOURCES,
    MOBILE_MAX_WIDTH,
    OUTCOME_VALUES,
    device_class,
    is_meta_attribution,
    outcome_class,
    page_path,
)

__all__ = [
    "DEVICE_VALUES",
    "META_SOURCES",
    "MOBILE_MAX_WIDTH",
    "OUTCOME_LABELS",
    "OUTCOME_VALUES",
    "device_class",
    "is_meta_attribution",
    "normalize_slice",
    "outcome_class",
    "page_path",
    "row_matches_slice",
    "slice_is_empty",
    "slice_label",
    "slice_label_parts",
]


def normalize_slice(spec: dict | None) -> dict:
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
    # Mixed by design: list[str] for the multi-valued keys, str for page and
    # the utm_* fields, bool for fbclid.
    out: dict[str, Any] = {}

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
    """Python predicate over already-classified row facts.

    Sampling filters in SQL on the same columns; this stays for cuts, tests,
    and any caller holding in-memory rows.
    """
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
    raw_attrs = row.get("attribution")
    attrs: dict = raw_attrs if isinstance(raw_attrs, dict) else {}
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
        elif want_source == "non_meta":
            if is_meta_attribution(attrs):
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


def slice_label_parts(spec: dict) -> list:
    """Human tokens for a normalised slice. Empty means nothing to name.

    Cuts, cohort auto-names, and the Insights picker all join or list these.
    Changing a wording here is the one place that changes every surface.
    """
    if not spec:
        return []
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
    return parts


def slice_label(spec: dict) -> str:
    if not spec:
        return "everyone else"
    parts = slice_label_parts(spec)
    return " · ".join(parts) if parts else "all sessions"
