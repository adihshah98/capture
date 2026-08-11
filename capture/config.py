"""Behavioural settings, read from the environment once.

Its own module so the routers, the retention sweep and the ingest limits can be
read without importing the web app — server.py used to hold all of these, which
meant anything needing one of them had to import 27 routes to get it.

This module imports nothing from the package. It is the bottom of the graph, and
it used to import workos to read one API key, which meant almost everything
transitively imported an HTTP client. The WorkOS settings live here now and the
client reads them.

Paths are not here: they live in paths.py, and callers read `paths.DATA_ROOT`
rather than a copy, so a test redirecting the data root sets it in one place.

These are still module-level constants read at import, which is why tests patch
`config.RETENTION_DAYS` rather than constructing a settings object. Turning them
into one is the remaining half of that work; moving them here first is what makes
it a single-file change.
"""

import hashlib
import os

REPLAY_PASSWORD = os.environ.get("REPLAY_PASSWORD", "")
REPLAY_AUTH_DISABLED = os.environ.get("REPLAY_AUTH_DISABLED") == "1"

# Operators see every project, including those with no WorkOS org attached.
OPERATORS = {
    e.strip().lower() for e in os.environ.get("CAPTURE_OPERATORS", "").split(",") if e.strip()
}

WORKOS_API_KEY = os.environ.get("WORKOS_API_KEY", "")
WORKOS_CLIENT_ID = os.environ.get("WORKOS_CLIENT_ID", "")
WORKOS_API = os.environ.get("WORKOS_API") or "https://api.workos.com"
WORKOS_ORG_CACHE_TTL_S = float(os.environ.get("ORG_CACHE_TTL_S", "60"))

# Derived from the WorkOS key when unset so a single-box install needs no extra
# secret, at the cost of invalidating sessions if that key is rotated.
SESSION_SECRET = (
    os.environ.get("SESSION_SECRET")
    or hashlib.sha256(f"session:{WORKOS_API_KEY}".encode()).hexdigest()
)
SESSION_TTL_S = 7 * 24 * 3600
SESSION_COOKIE = "capture_session"
STATE_COOKIE = "capture_login_state"

MAX_BODY_BYTES = 50 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = int(os.environ.get("MAX_DECOMPRESSED_BYTES", 32 * 1024 * 1024))

MAX_PROJECT_BYTES = int(os.environ.get("MAX_PROJECT_BYTES", 2 * 1024**3))
MIN_FREE_DISK_BYTES = int(os.environ.get("MIN_FREE_DISK_BYTES", 1024**3))

RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))

ORDER_MAX_BODY_BYTES = 4096

# A mirror pointed at someone else's database — staging reading production.
# Recording and retention are refused outright rather than left to fail on a
# permission error: the sweep would otherwise try to DELETE rows it can see but
# does not own, which is production data one careless GRANT away from deletion.
READ_ONLY = os.environ.get("CAPTURE_READ_ONLY") == "1"

# A throwaway instance a deploy starts on a spare port to prove the release boots
# before the live one is replaced. It must not do any of the background work
# startup normally kicks off: the retention sweep would race the running
# server's, and resuming a Gemini batch alongside it would pay for it twice.
PREFLIGHT = os.environ.get("CAPTURE_PREFLIGHT") == "1"
