"""Compatibility alias for `analyze.store`.

The cohort store moved into the analyze package. Three test modules import it
under its old top-level name and hold the module object to call `close_all` in
cleanup, so this keeps that name bound to the *same module object* rather than
a copy -- `analyze_store.close_all()` and `analyze.store.close_all()` must
close the same connection cache, or a test's cleanup silently stops working.

Re-exporting names would not do that: the connection cache is module state.
"""

import sys

from analyze import store

sys.modules[__name__] = store
