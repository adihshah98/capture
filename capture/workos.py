"""WorkOS: the per-user login and the org memberships that gate project access.

Config is read here rather than in server.py because the CLI needs it too —
`invite` sends an invitation without starting a web app.

Only the API client lives here. Cookie signing and the auth middleware stay with
the app, since they are about this server's own session rather than WorkOS.
"""

import json
import os
import time
import urllib.parse
import urllib.request

from .errors import ApiError

API_KEY = os.environ.get("WORKOS_API_KEY", "")
CLIENT_ID = os.environ.get("WORKOS_CLIENT_ID", "")
API = os.environ.get("WORKOS_API") or "https://api.workos.com"
ORG_CACHE_TTL_S = float(os.environ.get("ORG_CACHE_TTL_S", "60"))


def enabled() -> bool:
    """Whether per-user login is configured. A function, not a constant: tests set
    the module attributes, and a caller holding a copy would not see it."""
    return bool(API_KEY and CLIENT_ID)


def call(path: str, payload: dict | None = None, bearer: bool = False) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {API_KEY}"} if bearer else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)


def authenticate(code: str) -> tuple:
    """Exchange an authorization code for (email, user_id)."""
    auth = call(
        "/user_management/authenticate",
        {
            "client_id": CLIENT_ID,
            "client_secret": API_KEY,
            "grant_type": "authorization_code",
            "code": code,
        },
    )
    user = auth.get("user") or {}
    return (user.get("email") or "").lower(), user.get("id") or ""


def authorize_url(redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "provider": "authkit",
            "state": state,
        }
    )
    return f"{API}/user_management/authorize?{query}"


_org_cache: dict = {}


def user_orgs(user_id: str) -> set:
    """Which orgs this user belongs to, cached briefly.

    On a lookup failure a warm cache is served rather than locking the user out;
    with no cache it raises, because guessing here would widen project access.
    """
    now = time.time()
    cached = _org_cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        memberships = call(
            "/user_management/organization_memberships"
            f"?user_id={urllib.parse.quote(user_id)}&statuses=active&limit=100",
            bearer=True,
        )
        orgs = {
            m["organization_id"] for m in memberships.get("data", []) if m.get("organization_id")
        }
    except Exception as exc:
        print(f"auth: membership lookup failed for {user_id}: {exc}")
        if cached:
            return cached[1]
        raise ApiError(503, "could not verify project access — try again shortly") from exc
    _org_cache[user_id] = (now + ORG_CACHE_TTL_S, orgs)
    return orgs
