"""Shaping the model's cut reply into the documented structure.

The model returns JSON that is *mostly* the requested shape. These functions
coerce it and drop the rest -- a section that came back as a string, an item with
no observation, a recommendation repeated in three sections.

Pure over dicts and strings: no I/O, no config, no model call. That is what lets
the awkward cases be tested directly, and every one of them here is a real reply
the model produced rather than a defensive hypothetical.

The subtle one is `_share_denominator_ok`. Prompting alone did not stop the model
attaching a cohort-wide total to a nine-session group, so a share whose
denominator is not the group size is dropped while the observation is kept: a
missing share renders as nothing, a wrong one renders as fact.
"""

import re
from typing import Optional

# Insight sections hold {observation, share, action} objects; recommendations
# and caveats stay plain string lists (a punch list / sample limit has no share).
CUT_INSIGHT_SECTIONS = ("journey", "attention", "intent", "blockers")
CUT_SECTIONS = CUT_INSIGHT_SECTIONS + ("caveats",)
# The prompt asks for 3-8 items; keep a little headroom so a good extra item
# is kept rather than silently dropped.
CUT_ITEMS_PER_SECTION = 10
CUT_RECOMMENDATIONS_CAP = 8


def empty_cut_analysis(error: Optional[str] = None) -> dict:
    out = {
        "headline": "",
        "recommendations": [],
        **{key: [] for key in CUT_SECTIONS},
    }
    if error:
        out["error"] = error
    return out


def _clean_text(value, limit: int) -> str:
    """Trimmed string, or "" for anything that is not usable text."""
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else ""


def _insight_item(item) -> Optional[dict]:
    """One {observation, share, action}, or None when there is no observation.

    A bare string is accepted as the observation: the model occasionally falls
    back to the old flat shape, and an item with no action still carries the
    finding. Share and action are optional and render only when present.
    """
    if isinstance(item, str):
        observation = _clean_text(item, 900)
        return {"observation": observation, "share": "", "action": ""} if observation else None
    if not isinstance(item, dict):
        return None
    # Generous limits: a good observation quotes labels and cites timestamps,
    # so it runs long. Truncating it would strip exactly the useful part.
    observation = _clean_text(item.get("observation"), 900)
    if not observation:
        return None
    return {
        "observation": observation,
        "share": _clean_text(item.get("share"), 60),
        "action": _clean_text(item.get("action"), 400),
    }


def _recommendation_line(item) -> str:
    """One punch-list line: a bare string, or the action field of an object."""
    if isinstance(item, str):
        return _clean_text(item, 400)
    if isinstance(item, dict):
        for key in ("action", "recommendation", "text"):
            line = _clean_text(item.get(key), 400)
            if line:
                return line
    return ""


def _recommendations_from_actions(out: dict) -> list:
    """Fallback punch list: unique non-empty actions from the insight items.

    Older cached cuts predate the top-level field; deriving from actions keeps
    the Recommendations block populated without a re-ask.
    """
    seen = set()
    lines = []
    for key in CUT_INSIGHT_SECTIONS:
        for item in out.get(key) or []:
            if not isinstance(item, dict):
                continue
            line = _clean_text(item.get("action"), 400)
            if not line or line.lower() in seen:
                continue
            seen.add(line.lower())
            lines.append(line)
            if len(lines) >= CUT_RECOMMENDATIONS_CAP:
                return lines
    return lines


# "4 of 9", "44% (4 of 9)", "9 of 9 sessions" — the denominator is the group
# size, and the model is told it is the only total it may use.
_SHARE_DENOM_RE = re.compile(r"\b(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)


def _share_denominator_ok(share: str, n: Optional[int]) -> bool:
    """False when a share cites a total that is not the group size.

    The model has been seen to write the cohort-wide total into a share for a
    much smaller filtered group. A share whose denominator is not `n` is not
    describing this group, so it is dropped rather than shown as fact.
    """
    if n is None or not share:
        return True
    found = _SHARE_DENOM_RE.findall(share)
    if not found:
        return True
    # A numerator above the group size is impossible here too.
    return all(int(den) == n and int(num) <= n for num, den in found)


def normalize_cut_analysis(raw, n: Optional[int] = None) -> dict:
    """Coerce the model's JSON into the documented shape, dropping anything else.

    `n` is the group size when known. Shares citing a different total are
    dropped: the observation still carries the finding, and a missing share
    renders as nothing, which beats rendering a wrong one.
    """
    data = raw if isinstance(raw, dict) else {}
    out = empty_cut_analysis()
    out["headline"] = _clean_text(data.get("headline"), 600)
    for key in CUT_SECTIONS:
        items = data.get(key)
        if not isinstance(items, list):
            continue
        clean = []
        for item in items:
            # Caveats stay plain strings; the insight sections are objects.
            entry = _clean_text(item, 900) if key == "caveats" else _insight_item(item)
            if entry:
                if isinstance(entry, dict) and not _share_denominator_ok(
                    entry["share"], n
                ):
                    entry["share"] = ""
                clean.append(entry)
            if len(clean) >= CUT_ITEMS_PER_SECTION:
                break
        out[key] = clean
    recs = data.get("recommendations")
    if isinstance(recs, list):
        clean = []
        seen = set()
        for item in recs:
            line = _recommendation_line(item)
            if not line or line.lower() in seen:
                continue
            seen.add(line.lower())
            clean.append(line)
            if len(clean) >= CUT_RECOMMENDATIONS_CAP:
                break
        out["recommendations"] = clean
    if not out["recommendations"]:
        out["recommendations"] = _recommendations_from_actions(out)
    return out
