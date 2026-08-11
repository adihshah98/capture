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
