"""Splits the suite into a fast tier and a database tier, and enforces the split.

Google's testing taxonomy separates a test's *size* (what resources it may touch
— a machine-checkable contract) from its *scope* (how much code it covers — a
judgement call). Only size can be enforced, so that is what this does: a test
marked `unit` may not open a database connection, and finds out immediately if it
tries.

UNIT_MODULES is deliberately a short explicit list rather than a naming
convention. It is the honest measure of how much of this suite can run without
Postgres, and it should grow as configuration stops being read at import time.

  pytest -m unit          the fast tier, well under a second
  pytest                  everything, ~2.5 minutes on a warm cluster

unittest discover ignores this file, so `npm run test:server` is unaffected.
"""

import pytest

from capture import db

UNIT_MODULES = {"test_paths", "test_analysis_port", "test_patch_contract"}


def pytest_collection_modifyitems(config, items):
    for item in items:
        module = item.module.__name__.rsplit(".", 1)[-1]
        marker = "unit" if module in UNIT_MODULES else "integration"
        item.add_marker(getattr(pytest.mark, marker))


@pytest.fixture(autouse=True)
def _unit_tests_get_no_database(request, monkeypatch):
    """The size contract, enforced rather than trusted. Without this, `unit` is a
    label that drifts the first time someone adds a query to a fast test."""
    if request.node.get_closest_marker("unit") is None:
        return

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            f"{request.node.nodeid} is marked unit but opened a database connection. "
            "Drop the dependency, or take it out of UNIT_MODULES in tests/conftest.py."
        )

    monkeypatch.setattr(db, "pool", refuse)
