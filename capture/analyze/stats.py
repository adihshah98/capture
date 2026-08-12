"""Measured splits: the counts handed to the model as fact.

Asked to count across write-ups, the model invents shares. So every number that
reaches the cut prompt is computed here in Python and handed over as given --
`cross_tab_block` renders the block the prompt reads, and the prompt's job is
to interpret it, not to derive it.

Three gates decide whether a split is reportable, and each blocks a different
failure mode:

- CROSS_TAB_MIN_CELL      a share of 2 sessions is noise, not a finding;
- CROSS_TAB_MIN_COVERAGE  a dimension known for a handful of rows describes
  the handful, not the cohort;
- CROSS_TAB_MIN_LIFT_PTS  a difference smaller than this is not worth an
  action, and the model will happily write one anyway.

`graded` is the subtle one. In a single-outcome cut (every row purchased) lift
is 0 by construction, so grading on lift would suppress every real split;
those cuts grade on composition instead. Getting that wrong produces an empty
splits block and a cut that reads as though nothing differed.

Splits that fail a gate are still named, under "Examined and not reported" --
without it the model reads silence as uniformity and asserts the opposite.
"""


from .slices import OUTCOME_LABELS

# --- Measured splits -------------------------------------------------------
#
# The model is unreliable at counting across dozens of write-ups: it invents
# plausible shares and, per the note in _given_facts, has attached a
# cohort-wide total to a nine-session group. Rather than instruct against
# that, compute the splits here and hand over exact numbers to quote.
#
# Only pairings a marketing team can act on are crossed. The full product of
# five dimensions is ten pairs, and at cohort sizes of 9-50 most cells land at
# n=1 — precisely the noise the floor below exists to suppress.
CROSS_TAB_PAIRS = (
    ("utm_source", "outcome"),
    ("utm_campaign", "outcome"),
    ("page", "outcome"),
    ("device", "outcome"),
    ("utm_source", "device"),
    ("page", "utm_source"),
)

# A cell must clear all three gates. Count alone lets 3-of-200 through;
# coverage alone lets a large but unremarkable cell through; lift alone lets a
# two-session fluke through.
CROSS_TAB_MIN_CELL = 3
CROSS_TAB_MIN_COVERAGE = 0.20
CROSS_TAB_MIN_LIFT_PTS = 15.0

CROSS_TAB_DIMENSIONS = {
    "outcome": "outcome",
    "device": "device",
    "page": "landing page",
    "utm_source": "traffic source",
    "utm_campaign": "campaign",
}


def _dimension_value(row: dict, dimension: str) -> str | None:
    """One row's value on a cross-tab dimension, or None when unknown.

    Unknown is not a value: a session with no utm_source may be direct,
    organic or untagged, and bucketing those together would invent a segment.
    """
    if dimension in ("utm_source", "utm_campaign"):
        attrs = row.get("attribution")
        attrs = attrs if isinstance(attrs, dict) else {}
        value = attrs.get(dimension)
        if not value and dimension == "utm_source" and attrs.get("fbclid"):
            # An fbclid with no utm_source is still unambiguously Meta.
            return "meta (fbclid)"
        return str(value).strip().lower() or None if value else None
    if dimension == "outcome":
        return OUTCOME_LABELS.get(row.get("outcome") or "rest")
    value = row.get(dimension)
    return str(value).strip() or None if value else None


def _coverage(rows: list, dimension: str) -> tuple:
    """(known_count, total) for a dimension across the group."""
    known = sum(1 for row in rows if _dimension_value(row, dimension))
    return known, len(rows)


def _purchase_rate(rows: list) -> float | None:
    """Share of rows that purchased, as a percentage, or None for no rows."""
    if not rows:
        return None
    bought = sum(1 for row in rows if (row.get("outcome") or "rest") == "purchased")
    return 100.0 * bought / len(rows)


def cross_tab_cells(rows: list, left: str, right: str) -> dict:
    """Measured split of `rows` by `left`, reported against `right`.

    Returns {"baseline", "kept", "suppressed", "coverage", "graded"}. `kept`
    holds only cells clearing every gate; `suppressed` counts what the gates
    removed, so the prompt can say a dimension was examined rather than leave
    the model to read silence as uniformity.

    When every session shares one outcome — a "purchased" cut, say — purchase
    rate is constant and lift is 0 for every cell by construction. Grading on
    it there would suppress the whole block, including splits that are real
    and visible. Such a group is graded on size alone and `graded` says so,
    so the prompt can describe what the numbers mean.
    """
    known, total = _coverage(rows, left)
    outcomes = {(row.get("outcome") or "rest") for row in rows}
    graded = "lift" if len(outcomes) > 1 else "composition"
    baseline = _purchase_rate(rows)
    # Accumulated as locals rather than in `out`: the result mixes strings,
    # tuples, lists and counters, so reading a counter back out of it gives the
    # union of all of them and `+= 1` stops meaning anything checkable.
    kept: list[dict] = []
    suppressed = 0

    def result() -> dict:
        return {
            "left": left,
            "right": right,
            "coverage": (known, total),
            "baseline": baseline,
            "graded": graded,
            "kept": kept,
            "suppressed": suppressed,
        }

    if not rows or not known:
        return result()

    buckets: dict[str, list] = {}
    for row in rows:
        value = _dimension_value(row, left)
        if value:
            buckets.setdefault(value, []).append(row)

    for value, bucket in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(bucket)
        coverage = n / total if total else 0.0
        rate = _purchase_rate(bucket)
        lift = None if rate is None or baseline is None else rate - baseline
        too_small = n < CROSS_TAB_MIN_CELL or coverage < CROSS_TAB_MIN_COVERAGE
        # Only grade on lift where outcome actually varies; see the docstring.
        too_flat = graded == "lift" and (
            lift is None or abs(lift) < CROSS_TAB_MIN_LIFT_PTS
        )
        if too_small or too_flat:
            suppressed += 1
            continue
        breakdown: dict[str, int] = {}
        for member in bucket:
            key = _dimension_value(member, right) or "unknown"
            breakdown[key] = breakdown.get(key, 0) + 1
        kept.append(
            {
                "value": value,
                "n": n,
                "share": 100.0 * coverage,
                "rate": rate,
                "lift": lift,
                "breakdown": breakdown,
            }
        )
    return result()


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def cross_tab_block(rows: list, spec: dict | None = None) -> str:
    """Exact, pre-computed splits for the group — empty when nothing clears.

    Dimensions pinned by the filter are skipped: they are constant across the
    group and cannot separate anything, which is the same reasoning
    _given_facts applies to the premises.
    """
    spec = spec or {}
    if len(rows) < CROSS_TAB_MIN_CELL:
        return ""

    pinned = {key for key in CROSS_TAB_DIMENSIONS if spec.get(key)}
    if spec.get("fbclid") is not None:
        pinned.add("utm_source")

    lines: list = []
    notes: list = []
    for left, right in CROSS_TAB_PAIRS:
        if left in pinned or right in pinned:
            continue
        result = cross_tab_cells(rows, left, right)
        known, total = result["coverage"]
        title = f"{CROSS_TAB_DIMENSIONS[left]} × {CROSS_TAB_DIMENSIONS[right]}"
        if not known:
            notes.append(f"- {title}: no session in this group carries a {left}.")
            continue
        if known < total:
            coverage_note = f" ({known} of {total} sessions carry a {left})"
        else:
            coverage_note = ""
        if not result["kept"]:
            notes.append(
                f"- {title}{coverage_note}: examined, nothing clears the "
                f"reporting floor."
            )
            continue
        baseline = result["baseline"]
        if result["graded"] == "lift":
            lines.append(
                f"\n{title}{coverage_note} — group baseline "
                f"{_pct(baseline)} purchased ({len(rows)} sessions)"
            )
        else:
            # Outcome is fixed by the filter, so a purchase rate here would
            # just restate it. These rows say how the group is made up.
            lines.append(
                f"\n{title}{coverage_note} — how the group breaks down "
                f"({len(rows)} sessions, all the same outcome)"
            )
        for cell in result["kept"]:
            parts = ", ".join(
                f"{k} {v}" for k, v in sorted(cell["breakdown"].items(), key=lambda kv: -kv[1])
            )
            head = (
                f"  {cell['value']}: {cell['n']} sessions "
                f"({_pct(cell['share'])} of the group)"
            )
            if result["graded"] == "lift":
                sign = "+" if cell["lift"] >= 0 else "−"
                head += (
                    f" · {_pct(cell['rate'])} purchased · "
                    f"{sign}{abs(cell['lift']):.0f}pts vs baseline"
                )
            lines.append(f"{head} · {parts}")
        if result["suppressed"]:
            lines.append(
                f"  (below the floor, not shown: {result['suppressed']} more)"
            )

    if not lines and not notes:
        return ""

    outcomes = {(row.get("outcome") or "rest") for row in rows}
    if len(outcomes) > 1:
        criterion = (
            f"Only splits meeting all of: at least {CROSS_TAB_MIN_CELL} "
            f"sessions, at least {_pct(CROSS_TAB_MIN_COVERAGE * 100)} of the "
            f"group, and at least {CROSS_TAB_MIN_LIFT_PTS:.0f} points away "
            "from the group's purchase rate."
        )
    else:
        criterion = (
            "Every session here shares one outcome, so there is no purchase "
            "rate to compare against. These rows show how the group is made "
            f"up: only splits of at least {CROSS_TAB_MIN_CELL} sessions and "
            f"at least {_pct(CROSS_TAB_MIN_COVERAGE * 100)} of the group."
        )
    header = (
        "## Measured splits — computed, not inferred\n\n"
        "These counts were calculated directly from the group's session "
        "labels. They are EXACT. Quote them as given; never recompute, round "
        "differently, or contradict them. Where your reading of the write-ups "
        "disagrees with a number here, the number is right.\n\n"
        f"{criterion} A split absent here is one the data does not support — "
        "do not assert it from the write-ups alone.\n\n"
        "A row below tells you WHAT differs, never WHY. The reason lives in "
        "the write-ups: pair each number with what those sessions actually "
        "did on the page, and never restate a row as a finding on its own."
    )
    body = "\n".join(lines)
    if notes:
        # Named either way: silence would let the model read "no split shown"
        # as "the group is uniform on that dimension".
        body += "\n\nExamined and not reported:\n" + "\n".join(notes)
    return f"{header}\n{body}".strip()
