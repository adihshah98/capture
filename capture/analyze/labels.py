"""The words a cohort is described by: its selection rule and its display name.

Two audiences, both human. `cohort_selection_text` writes the sentence the
Insights UI shows under a cohort ("Chosen as the latest 50 purchased + 50
non-purchase sessions on /pages/x with duration >=5s..."), and
`_auto_cohort_label` writes the short name a cohort gets when the user does not
supply one.

Pure string generation over an already-normalised slice. It reads the sampling
vocabulary but never queries anything, which is what keeps the sampler free to
change how it selects without changing how the selection reads.

One naming note: `spec` here is always a slice dict, following the rest of the
package. The sampling-constants module is imported as `cohort_spec` to keep the
two apart.
"""

import time
from typing import Optional

from . import spec as cohort_spec
from .slices import OUTCOME_LABELS, normalize_slice, slice_label

def _slice_scope_text(spec: dict, url_contains: str) -> str:
    parts = []
    if spec:
        named = slice_label(spec)
        if named and named not in ("everyone else", "all sessions"):
            parts.append(named)
    needle = (url_contains or "").strip()
    if needle and not (spec or {}).get("page"):
        parts.append(needle)
    return ", ".join(parts) if parts else "any page"


def _format_cohort_clock(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    return time.strftime("%b %-d %H:%M", time.localtime(int(ms) / 1000))


def _time_window_text(start_ms: Optional[int], end_ms: Optional[int]) -> str:
    if start_ms is None and end_ms is None:
        return ""
    if start_ms is not None and end_ms is not None:
        return f" between {_format_cohort_clock(start_ms)} and {_format_cohort_clock(end_ms)}"
    if start_ms is not None:
        return f" after {_format_cohort_clock(start_ms)}"
    return f" before {_format_cohort_clock(end_ms)}"


def cohort_selection_text(
    purchased_n: int,
    all_n: int,
    *,
    any_n: int = 0,
    a_n: int = 0,
    b_n: int = 0,
    min_duration_ms: int = cohort_spec.MIN_DURATION_MS,
    url_contains: str = cohort_spec.URL_CONTAINS,
    slice_spec: Optional[dict] = None,
    slice_b: Optional[dict] = None,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    order: str = cohort_spec.ORDER_LATEST,
) -> str:
    """Human-readable sampling rule for the Insights UI."""
    spec = normalize_slice(slice_spec)
    spec_b = normalize_slice(slice_b)
    min_s = max(0, int(min_duration_ms)) / 1000
    min_label = f"{min_s:g}s" if min_s != int(min_s) else f"{int(min_s)}s"
    sort = order if order in cohort_spec.ORDERS else cohort_spec.ORDER_LATEST
    rank = "latest" if sort == cohort_spec.ORDER_LATEST else "earliest"
    window = _time_window_text(start_ms, end_ms)

    if a_n > 0 or b_n > 0:
        a_scope = _slice_scope_text(spec, url_contains)
        if b_n > 0:
            b_scope = _slice_scope_text(spec_b, url_contains)
            who = f"{rank} {a_n} for A ({a_scope}) + {b_n} for B ({b_scope})"
        else:
            who = f"{rank} {a_n} for A ({a_scope})"
        return (
            f"Chosen as the {who} sessions{window} with duration ≥{min_label} "
            f"(last_seen − first_seen), preferring sessions with more clicks when "
            f"timestamps tie."
        )

    if any_n > 0:
        who = f"{rank} {any_n} matching"
    else:
        who = f"{rank} {purchased_n} purchased + {all_n} non-purchase"
    scope = _slice_scope_text(spec, url_contains)
    return (
        f"Chosen as the {who} sessions on {scope}{window} with duration ≥{min_label} "
        f"(last_seen − first_seen), preferring sessions with more clicks when "
        f"timestamps tie."
    )


def _slice_label_bits(spec: Optional[dict], url_contains: str = "") -> list:
    """Compact filter tokens for cohort display names."""
    bits = []
    clean = normalize_slice(spec)
    outcome = clean.get("outcome") or []
    if outcome:
        keys = set(outcome)
        if keys == {"purchased", "checkout"}:
            bits.append("reached checkout")
        elif keys == {"checkout", "rest"}:
            bits.append("non-purchase")
        else:
            bits.append(" or ".join(OUTCOME_LABELS.get(o, o) for o in outcome))
    for field in ("device", "page", "utm_source", "utm_campaign"):
        value = clean.get(field)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            bits.append(" or ".join(str(v) for v in value))
        else:
            bits.append(str(value))
    if not clean.get("page"):
        needle = (url_contains or "").strip()
        if needle:
            bits.append(needle)
    return bits


def _auto_cohort_label(
    *,
    stamp: str,
    purchased_n: int,
    all_n: int,
    any_n: int,
    a_n: int,
    b_n: int,
    url_contains: str,
    spec: dict,
    spec_b: dict,
) -> str:
    """Date/time first, then whatever filters were set."""
    parts = [stamp]
    if a_n > 0 or b_n > 0:
        a_bits = _slice_label_bits(spec, url_contains)
        if b_n > 0:
            b_bits = _slice_label_bits(spec_b, url_contains)
            if a_bits or b_bits:
                parts.append(
                    f"A {' '.join(a_bits) if a_bits else 'any'} / "
                    f"B {' '.join(b_bits) if b_bits else 'any'}"
                )
        else:
            parts.extend(a_bits)
    elif any_n > 0 or purchased_n or all_n:
        parts.extend(_slice_label_bits(spec, url_contains))
    return " · ".join(parts)[:120]


def clean_cohort_label(label: Optional[str]) -> str:
    """Trim and cap a cohort display name. Empty string means “use auto label”."""
    return str(label or "").strip()[:120]

