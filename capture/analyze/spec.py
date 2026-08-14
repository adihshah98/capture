"""The cohort sampling vocabulary: what to select, and how much of it.

These constants were threaded through the sampler as bare keyword arguments --
`build_cohort` took fifteen of them, `sample_cohort_rows` fourteen, and the same
twelve values were re-typed at every layer down to the sampler. The web
layer declared them a third time as FastAPI `Query()` objects. Adding one
sampling dimension meant touching seven signatures.

They live here so there is one definition to import and, in time, one object to
pass. `store.py` already persists exactly this shape in the cohort's `fields`
JSON column, so the grouping is not invented -- it is the shape the data has.

Nothing here imports the rest of the package: this is a leaf, and the sampler,
the label writer and the port can all depend on it without a cycle.
"""

import os
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


class Connection(Protocol):
    """What the sampling functions actually require of a connection.

    Production passes a psycopg connection from `db.connection(project_id)`.
    Tests may pass a thin sqlite stub that accepts the same `%s` SQL.
    `execute` is the entire surface these functions use.
    """

    def execute(self, sql: str, params: Any = ..., /) -> Any: ...


# The ceiling on one cohort. Every session in a cohort is a headless Chromium
# reconstruction and a set of frames sent to the model, so this is a cost and
# memory bound rather than a schema one.
MAX_SESSIONS = 500

# Below this a recording is a bounce with nothing to read: a page load, maybe a
# scroll, no decision. Measured as last_seen - first_seen.
MIN_DURATION_MS = 5000

# Empty means "any page". Stored on old cohorts that were sampled with a
# substring gate; new samples use the `page` slice instead. Not an API default.
URL_CONTAINS = os.environ.get("COHORT_URL_CONTAINS", "")

# How many of the newest index rows to scan for pool facets. Membership
# sampling filters in SQL, so this is not the bound on a rare slice filling a
# cohort — only the facet picker stops here.
CANDIDATE_CAP = 5000

ORDER_LATEST = "latest"
ORDER_EARLIEST = "earliest"
ORDERS = (ORDER_LATEST, ORDER_EARLIEST)


def clean_order(order: str) -> str:
    """The sort, or the default when it is not one we know.

    Callers accept `order` from a query string, and every one of them was
    repeating `order if order in ORDERS else ORDER_LATEST` inline.
    """
    return order if order in ORDERS else ORDER_LATEST


@dataclass(frozen=True)
class CohortSpec:
    """How a cohort was selected: everything except which sessions came back.

    One object instead of the twelve keyword arguments that were re-typed at
    every layer -- build_cohort took fifteen parameters, sample_cohort_rows
    fourteen, and the web layer declared them a third time as Query() objects.
    Adding a sampling dimension meant editing seven signatures and getting the
    defaults right in each.

    Frozen because a cohort's selection rule is a fact about a cohort that has
    already been sampled: `build_cohort` records it, and every later read --
    the selection sentence, the auto label, a resample -- is describing what was
    already decided. Use `replace(spec, ...)` for a variant.

    Sampling is one mode: **groups** -- `a_n` (+ optional `b_n`) with
    independent slices. `slice_b` is what makes an A/B cut possible.

    The field names match the keys `store.py` persists in the cohort's `fields`
    JSON column, so `asdict()` round-trips through storage without a mapping.
    """

    a_n: int = 0
    b_n: int = 0

    # Base gates, applied in SQL.
    min_duration_ms: int = MIN_DURATION_MS
    url_contains: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    order: str = ORDER_LATEST

    # Cut-style slices, already normalised by slices.normalize_slice.
    slice_spec: dict = field(default_factory=dict)
    slice_b: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        """How many sessions this spec asks for."""
        return self.a_n + self.b_n

    def normalised(self) -> "CohortSpec":
        """A copy with the sort coerced to a known value.

        `order` arrives from a query string, so every consumer was repeating the
        same fallback. Done once, here, rather than at each layer.
        """
        return replace(self, order=clean_order(self.order))

    @classmethod
    def from_cohort(cls, cohort: dict) -> "CohortSpec":
        """Recover the selection rule from a stored cohort.

        `build_cohort` writes each field as its own key, so this is the inverse.
        A cohort written before a field existed simply gets its default, which is
        why every one of them has one.
        """
        min_duration = cohort.get("min_duration_ms")
        return cls(
            a_n=int(cohort.get("a_n") or 0),
            b_n=int(cohort.get("b_n") or 0),
            min_duration_ms=int(min_duration if min_duration is not None else MIN_DURATION_MS),
            url_contains=str(cohort.get("url_contains") or ""),
            start_ms=cohort.get("start_ms"),
            end_ms=cohort.get("end_ms"),
            order=str(cohort.get("order") or ORDER_LATEST),
            slice_spec=cohort.get("slice") or {},
            slice_b=cohort.get("slice_b") or {},
        )
