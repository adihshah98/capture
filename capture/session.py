"""The signed cookie this server issues after a WorkOS login.

Separate from capture/workos.py: that talks to WorkOS, this is our own session
token. Separate from the routers because the auth middleware reads a cookie on
every request while only /auth/callback writes one.

The payload is signed, not encrypted — it carries an email, a user id and an
expiry, all of which the user already knows. The signature is what stops them
editing it.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import config


def _sign(raw: str) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def issue(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{raw}.{_sign(raw)}"


def read(cookie: str | None) -> dict | None:
    """The payload, or None for anything not currently valid — missing, malformed,
    wrongly signed or expired. Callers treat None as "not logged in" and must not
    need to tell those cases apart.

    compare_digest, not ==, so the comparison does not leak the signature a byte
    at a time.
    """
    if not cookie or "." not in cookie:
        return None
    raw, sig = cookie.rsplit(".", 1)
    if not secrets.compare_digest(_sign(raw), sig):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload
