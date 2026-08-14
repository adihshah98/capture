"""The words a cohort is described by: its selection rule and its display name.

Two audiences, both human. `cohort_selection_text` writes the sentence the
Insights UI shows under a cohort ("Chosen as the latest 50 purchased + 50
non-purchase sessions on /pages/x with duration >=5s..."), and
`_auto_cohort_label` writes the short name a cohort gets when the user does not
supply one.

Pure string generation over an already-normalised slice. Slice tokens come from
`slice_label_parts` so a cut chip and a cohort name cannot disagree. This
module only adds what a slice does not know: how many, A vs B, duration, the
time window, and `url_contains` when it is not already a `page` constraint.

The sampling-constants module is imported as `cohort_spec` so it is not
confused with a slice dict (`slice_spec`).
"""

import time

from . import spec as cohort_spec
from .slices import normalize_slice, slice_label_parts
from .spec import CohortSpec


def _slice_scope_text(slice_spec: dict, url_contains: str) -> str:
    named = " · ".join(slice_label_parts(slice_spec))
    needle = (url_contains or "").strip()
    extra = needle if needle and not (slice_spec or {}).get("page") else ""
    if named and extra:
        return f"{named}, {extra}"
    return named or extra or "any page"


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
    slice_spec = normalize_slice(selection.slice_spec)
    slice_b = normalize_slice(selection.slice_b)
    min_s = max(0, int(selection.min_duration_ms)) / 1000
    min_label = f"{min_s:g}s" if min_s != int(min_s) else f"{int(min_s)}s"
    rank = "latest" if selection.order == cohort_spec.ORDER_LATEST else "earliest"
    window = _time_window_text(selection.start_ms, selection.end_ms)

    if a_n > 0 or b_n > 0:
        a_scope = _slice_scope_text(slice_spec, url_contains)
        if b_n > 0:
            b_scope = _slice_scope_text(slice_b, url_contains)
            who = f"{rank} {a_n} for A ({a_scope}) + {b_n} for B ({b_scope})"
        else:
            who = f"{rank} {a_n} for A ({a_scope})"
        return (
            f"Chosen as the {who} sessions{window} with duration ≥{min_label} "
            f"(last_seen − first_seen), preferring sessions with more clicks when "
            f"timestamps tie."
        )

    # Stored cohorts sampled before groups existed. New samples never land here.
    if any_n > 0:
        who = f"{rank} {any_n} matching"
    else:
        who = f"{rank} {purchased_n} purchased + {all_n} non-purchase"
    scope = _slice_scope_text(slice_spec, url_contains)
    return (
        f"Chosen as the {who} sessions on {scope}{window} with duration ≥{min_label} "
        f"(last_seen − first_seen), preferring sessions with more clicks when "
        f"timestamps tie."
    )


def _slice_label_bits(slice_spec: dict | None, url_contains: str = "") -> list:
    """Compact filter tokens for cohort display names."""
    clean = normalize_slice(slice_spec)
    bits = list(slice_label_parts(clean))
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
    slice_spec = normalize_slice(selection.slice_spec)
    slice_b = normalize_slice(selection.slice_b)
    parts = [stamp]
    if a_n > 0 or b_n > 0:
        a_bits = _slice_label_bits(slice_spec, url_contains)
        if b_n > 0:
            b_bits = _slice_label_bits(slice_b, url_contains)
            if a_bits or b_bits:
                parts.append(
                    f"A {' '.join(a_bits) if a_bits else 'any'} / "
                    f"B {' '.join(b_bits) if b_bits else 'any'}"
                )
        else:
            parts.extend(a_bits)
    elif any_n > 0 or purchased_n or all_n:
        # Display shim: old any / purchased+all cohorts still have a name.
        parts.extend(_slice_label_bits(slice_spec, url_contains))
    return " · ".join(parts)[:120]


def clean_cohort_label(label: str | None) -> str:
    """Trim and cap a cohort display name. Empty string means “use auto label”."""
    return str(label or "").strip()[:120]
