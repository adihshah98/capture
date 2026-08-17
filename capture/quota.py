"""The per-project storage quota, cached in memory.

One dict, deliberately: ingest reads it on every batch, deleting a session credits
it back, and the retention sweep does too. A second copy in another module would
silently double-count and let a project past its cap.

Process-local, which is why this server runs a single worker. A second worker
would keep its own counter and each would see roughly half the true usage. Moving
the check into SQL is what would lift that.
"""

from . import sessionstore

_bytes_used: dict = {}


async def used(project) -> int:
    """Cached so the quota check is not a query per ingest; the running total is
    maintained in SQL and re-read only on a cold cache."""
    if project.id not in _bytes_used:
        _bytes_used[project.id] = await sessionstore.project_bytes(project.id)
    return _bytes_used[project.id]


def add(project_id, delta: int) -> None:
    _bytes_used[project_id] = _bytes_used.get(project_id, 0) + delta


def clear() -> None:
    """Drop every project's cache. The retention sweep uses this: it may have
    freed bytes across many projects, and recomputing on next read is cheaper
    than tracking each credit."""
    _bytes_used.clear()


def forget(project_id) -> None:
    """Drop the cache for one project, so the next read comes from SQL. Used after
    a delete or a sweep, where recomputing is cheaper than tracking the credit."""
    _bytes_used.pop(project_id, None)
