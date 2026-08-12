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

from . import spec as cohort_spec
from .slices import OUTCOME_LABELS, normalize_slice, slice_label
from .spec import CohortSpec


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


def _format_cohort_clock(ms: int | None) -> str:
    if ms is None:
        return ""
    return time.strftime("%b %-d %H:%M", time.localtime(int(ms) / 1000))


def _time_window_text(start_ms: int | None, end_ms: int | None) -> str:
    if start_ms is None and end_ms is None:
        return ""
    if start_ms is not None and end_ms is not None:
        return f" between {_format_cohort_clock(start_ms)} and {_format_cohort_clock(end_ms)}"
    if start_ms is not None:
        return f" after {_format_cohort_clock(start_ms)}"
    return f" before {_format_cohort_clock(end_ms)}"


def cohort_selection_text(selection: CohortSpec) -> str:
    """Human-readable sampling rule for the Insights UI."""
    selection = selection.normalised()
    purchased_n, all_n = selection.purchased_n, selection.all_n
    any_n, a_n, b_n = selection.any_n, selection.a_n, selection.b_n
    url_contains = selection.url_contains
    spec = normalize_slice(selection.slice_spec)
    spec_b = normalize_slice(selection.slice_b)
    min_s = max(0, int(selection.min_duration_ms)) / 1000
    min_label = f"{min_s:g}s" if min_s != int(min_s) else f"{int(min_s)}s"
    rank = "latest" if selection.order == cohort_spec.ORDER_LATEST else "earliest"
    window = _time_window_text(selection.start_ms, selection.end_ms)

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


def _slice_label_bits(spec: dict | None, url_contains: str = "") -> list:
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


def _auto_cohort_label(selection: CohortSpec, *, stamp: str) -> str:
    """Date/time first, then whatever filters were set."""
    purchased_n, all_n = selection.purchased_n, selection.all_n
    any_n, a_n, b_n = selection.any_n, selection.a_n, selection.b_n
    url_contains = selection.url_contains
    spec = normalize_slice(selection.slice_spec)
    spec_b = normalize_slice(selection.slice_b)
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


def clean_cohort_label(label: str | None) -> str:
    """Trim and cap a cohort display name. Empty string means “use auto label”."""
    return str(label or "").strip()[:120]
