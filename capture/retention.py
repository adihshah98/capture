"""Deleting recordings that have aged out.

Called once at startup and never again — there is no scheduler. A deploy is the
sweep's trigger, which is fine for a 30-day window and would not be for an hourly one.

Refuses to run when the server is READ_ONLY: a mirror pointed at someone else's
database would otherwise try to DELETE rows it can see but does not own.
"""

import time

from . import analysis_port as ap
from . import config, projects, quota, sessionstore, storage


def sweep_expired_sessions() -> int:
    # Expiry is decided in SQL rather than by walking every session directory,
    # which is what made startup take ~23s at 9k sessions.
    if config.RETENTION_DAYS <= 0:
        return 0
    cutoff_ms = int((time.time() - config.RETENTION_DAYS * 86400) * 1000)
    removed = 0
    for project in projects.all_projects(fresh=True):
        for session_id in sessionstore.expired(project.id, cutoff_ms):
            # Through storage, not rmtree: dropping only the local directory
            # would strand the chunks in the bucket with nothing pointing at
            # them, and no later sweep could ever find them again.
            storage.delete_session(project.storage_prefix, session_id)
            sessionstore.forget(project.id, session_id)
            # Same rule as delete_session: a session purged here no longer has
            # a directory, so its cohort memberships point at write-ups that
            # are gone.
            try:
                ap.forget_session(project, session_id)
            except Exception as exc:
                print(f"cohort cleanup after expiring {session_id}: {exc}")
            removed += 1
    if removed:
        quota.clear()
    return removed
