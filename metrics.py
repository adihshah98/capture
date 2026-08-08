"""Tier 1 cohort metrics: computed rates over session facts.

These numbers are counted, not inferred. The cut analyses in analyze.py ask a
model to estimate shares from prose write-ups, which is why the prompt has to
insist on percentages; everything here is arithmetic over facts.json and can be
checked by hand.

Only geometry-and-time facts are aggregated. Nothing is keyed on a customer's
markup, so a CSS refactor on the recorded site cannot silently split a series in
two or swap two metrics' identities.

Every rate carries its own coverage: the number of sessions that could have
produced the fact at all. A session missing the underlying measurement is
excluded from the denominator rather than counted as a zero, and the caller is
told how much of the cohort was excluded. Without that, a metric quietly becomes
a statement about a shrinking, self-selected subset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from analyze import ANALYSIS_DIR, row_matches_slice, normalize_slice

FACTS_FILE = "facts.json"

# Below this many eligible sessions a percentage overstates precision; the same
# threshold the cut prompt uses when it asks for raw counts instead.
MIN_SAMPLE_FOR_PCT = 5


def facts_path(session_dir: Path) -> Path:
    return session_dir / ANALYSIS_DIR / FACTS_FILE


def read_facts(session_dir: Path) -> Optional[dict]:
    """Tier 1 facts for one session, or None if it has not been extracted."""
    path = facts_path(session_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class Metric:
    """One aggregate over session facts.

    `eligible` decides whether a session can answer this metric at all —
    separating "did not do it" from "could not be measured". `value` is read
    only from eligible sessions.
    """

    def __init__(
        self,
        key: str,
        label: str,
        kind: str,
        eligible: Callable[[dict], bool],
        value: Callable[[dict], Optional[float]],
    ):
        self.key = key
        self.label = label
        self.kind = kind  # "rate" | "distribution"
        self.eligible = eligible
        self.value = value


def _has_geometry(f: dict) -> bool:
    return bool(f.get("has_geometry"))


def _timed(f: dict) -> bool:
    # A zero-length recording cannot speak to how long anyone stayed.
    return (f.get("duration_s") or 0) > 0


def _band(f: dict, key: str) -> Optional[float]:
    bands = f.get("dwell_ms_by_band")
    if not isinstance(bands, dict):
        return None
    return (bands.get(key) or 0) / 1000


def _depth_at_least(pct: int) -> Callable[[dict], Optional[float]]:
    def read(f: dict) -> Optional[float]:
        depth = f.get("max_depth_pct")
        return None if depth is None else float(depth >= pct)

    return read


def _dwell_over(band: str, seconds: int) -> Callable[[dict], Optional[float]]:
    def read(f: dict) -> Optional[float]:
        secs = _band(f, band)
        return None if secs is None else float(secs > seconds)

    return read


METRICS: list = [
    Metric(
        "scrolled_past_50",
        "Scrolled past halfway",
        "rate",
        _has_geometry,
        _depth_at_least(50),
    ),
    Metric(
        "scrolled_past_75",
        "Scrolled past 75%",
        "rate",
        _has_geometry,
        _depth_at_least(75),
    ),
    Metric(
        "reached_bottom",
        "Reached the bottom",
        "rate",
        _has_geometry,
        _depth_at_least(100),
    ),
    Metric(
        "dwell_over_10s_lower_half",
        "Spent >10s in the lower half of the page",
        "rate",
        _has_geometry,
        lambda f: (
            None
            if _band(f, "50-75") is None
            else float((_band(f, "50-75") + _band(f, "75-100")) > 10)
        ),
    ),
    Metric(
        "dwell_over_10s_deep",
        "Spent >10s past 75%",
        "rate",
        _has_geometry,
        _dwell_over("75-100", 10),
    ),
    Metric(
        "active_over_30s",
        "Active for more than 30s",
        "rate",
        _timed,
        lambda f: float((f.get("active_s") or 0) > 30),
    ),
    Metric(
        "bounced_under_10s",
        "Left within 10s",
        "rate",
        _timed,
        lambda f: float((f.get("duration_s") or 0) < 10),
    ),
    Metric(
        "any_click",
        "Clicked anything",
        "rate",
        lambda f: True,
        lambda f: float((f.get("click_count") or 0) > 0),
    ),
    Metric(
        "rage_clicked",
        "Rage-clicked",
        "rate",
        lambda f: True,
        lambda f: float((f.get("rage_click_count") or 0) > 0),
    ),
    Metric(
        "multi_page",
        "Viewed more than one page",
        "rate",
        lambda f: True,
        lambda f: float((f.get("page_count") or 0) > 1),
    ),
    Metric(
        "median_active_s",
        "Median active seconds",
        "distribution",
        _timed,
        lambda f: float(f.get("active_s") or 0),
    ),
    Metric(
        "median_depth_pct",
        "Median scroll depth",
        "distribution",
        _has_geometry,
        lambda f: None if f.get("max_depth_pct") is None else float(f["max_depth_pct"]),
    ),
]

METRICS_BY_KEY = {m.key: m for m in METRICS}


def _median(values: list) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def compute_metric(metric: Metric, facts: list, cohort_size: int) -> dict:
    """One metric over a list of session facts, with its coverage."""
    values = []
    for f in facts:
        if not metric.eligible(f):
            continue
        value = metric.value(f)
        if value is not None:
            values.append(value)

    eligible = len(values)
    # Share of the selected cohort this metric could actually speak for. A low
    # number means the metric describes a subset, not the cohort.
    coverage_pct = round((eligible / cohort_size) * 100) if cohort_size else 0

    out = {
        "key": metric.key,
        "label": metric.label,
        "kind": metric.kind,
        "eligible": eligible,
        "cohort_size": cohort_size,
        "coverage_pct": coverage_pct,
        # Too few sessions for a percentage to mean anything; the caller should
        # show the raw count instead of a rate.
        "directional": eligible < MIN_SAMPLE_FOR_PCT,
    }

    if metric.kind == "rate":
        matched = int(sum(values))
        out["count"] = matched
        out["pct"] = round((matched / eligible) * 100) if eligible else None
    else:
        out["median"] = _median(values)
        out["count"] = eligible
        out["pct"] = None
    return out


def cohort_metrics(
    rows: list,
    facts_for: Callable[[dict], Optional[dict]],
    slice_spec: Optional[dict] = None,
    keys: Optional[list] = None,
) -> dict:
    """Every Tier 1 metric for the rows matching a slice.

    `rows` are cohort rows as analyze.py builds them, so the same slice spec that
    drives a cut selects the same sessions here. `facts_for` maps a row to its
    facts.json, keeping this function free of any filesystem layout.
    """
    spec = normalize_slice(slice_spec)
    selected = [r for r in rows if row_matches_slice(r, spec)]

    facts = []
    missing = 0
    for row in selected:
        f = facts_for(row)
        if f is None:
            # Not yet extracted. Counted separately so a half-processed cohort
            # is visible as such rather than looking like a small one.
            missing += 1
            continue
        facts.append(f)

    wanted = METRICS
    if keys:
        wanted = [METRICS_BY_KEY[k] for k in keys if k in METRICS_BY_KEY]

    cohort_size = len(selected)
    return {
        "filter": spec,
        "cohort_size": cohort_size,
        "with_facts": len(facts),
        "missing_facts": missing,
        "metrics": [compute_metric(m, facts, cohort_size) for m in wanted],
    }
