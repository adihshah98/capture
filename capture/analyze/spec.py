"""The cohort sampling vocabulary: what to select, and how much of it.

These constants were threaded through the sampler as bare keyword arguments --
`build_cohort` took fifteen of them, `sample_cohort_rows` fourteen, and the same
twelve values were re-typed at every layer down to `_sample_candidates`. The web
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

    They were annotated `sqlite3.Connection`, which was true only on the CLI
    path that read the stale `data/index.db`. What production passes is
    `compat.SqliteStyleConnection`, a psycopg wrapper that translates `?`
    placeholders -- structurally unrelated to `sqlite3.Connection`, so the
    annotation was false everywhere it mattered.

    `execute` is the entire surface these functions use, and both types have
    it. Stating that keeps the annotation honest through the Postgres port,
    which removes the wrapper but not the method.
    """

    def execute(self, sql: str, params: Any = ..., /) -> Any: ...


# The insights cohort's defaults. A cohort sampled without arguments is the
# latest 50 purchased plus 50 non-purchase sessions, at least five seconds long.
DEFAULT_PURCHASED_N = 50
DEFAULT_ALL_N = 50

# The ceiling on one cohort. Every session in a cohort is a headless Chromium
# reconstruction and a set of frames sent to the model, so this is a cost and
# memory bound rather than a schema one.
MAX_SESSIONS = 500

# Below this a recording is a bounce with nothing to read: a page load, maybe a
# scroll, no decision. Measured as last_seen - first_seen.
MIN_DURATION_MS = 5000

# A substring required in the entry URL, applied as `url LIKE '%<value>%'`.
#
# Empty means "any page", which is the correct default for a multi-tenant
# product. It is read from the environment because it was previously hardcoded
# to one customer's landing page ("/pages/ashwamag-01"), which silently scoped
# every cohort sampled without an explicit override -- including every cohort
# created through the API, since this is the Query() default. Any other tenant
# got an empty cohort and a successful response.
URL_CONTAINS = os.environ.get("COHORT_URL_CONTAINS", "")

# How many of the newest index rows to scan when a cut-style slice is applied.
# The slice itself is evaluated in Python after the query returns, so this is
# the real bound on how rare a slice can be and still fill a cohort.
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

    Three sampling modes, and they are mutually exclusive:

      * **groups** -- `a_n` (+ optional `b_n`) with independent slices. The one
        to prefer; `slice_b` is what makes an A/B cut possible.
      * **any** -- `any_n` sessions from one mixed pool.
      * **legacy split** -- `purchased_n` + `all_n`, a purchase vs non-purchase
        split that predates the slice language. Still tagged purchased/all, and
        the arms own the outcome split, so a slice's `outcome` is dropped.

    The field names match the keys `store.py` persists in the cohort's `fields`
    JSON column, so `asdict()` round-trips through storage without a mapping.
    """

    # Legacy purchase-split arms.
    purchased_n: int = 0
    all_n: int = 0
    # One mixed pool.
    any_n: int = 0
    # Independent groups.
    a_n: int = 0
    b_n: int = 0

    # Base gates, applied in SQL for every mode.
    min_duration_ms: int = MIN_DURATION_MS
    url_contains: str = URL_CONTAINS
    start_ms: int | None = None
    end_ms: int | None = None
    order: str = ORDER_LATEST

    # Cut-style slices, already normalised by slices.normalize_slice.
    slice_spec: dict = field(default_factory=dict)
    slice_b: dict = field(default_factory=dict)

    @property
    def uses_groups(self) -> bool:
        return self.a_n > 0 or self.b_n > 0

    @property
    def total(self) -> int:
        """How many sessions this spec asks for, whichever mode it is in."""
        if self.uses_groups:
            return self.a_n + self.b_n
        if self.any_n > 0:
            return self.any_n
        return self.purchased_n + self.all_n

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
            purchased_n=int(cohort.get("purchased_n") or 0),
            all_n=int(cohort.get("all_n") or 0),
            any_n=int(cohort.get("any_n") or 0),
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
