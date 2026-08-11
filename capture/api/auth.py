"""WorkOS login, callback and logout.

The middleware that *enforces* login stays in server.py, because it runs on every
request including the static mount. These three routes only establish and clear
the session it reads.
"""

import secrets
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse, Response

from .. import config, session, snippet, workos
from ..errors import ApiError

router = APIRouter()


@router.get("/login")
def login():
    if not workos.enabled():
        return Response(
            "Per-user login is not configured; set WORKOS_API_KEY and WORKOS_CLIENT_ID.\n",
            status_code=503,
            media_type="text/plain",
        )
    state = secrets.token_urlsafe(24)
    target = workos.authorize_url(snippet.public_base_url() + "/auth/callback", state)
    response = RedirectResponse(target, status_code=302)
    response.set_cookie(
        config.STATE_COOKIE, state, max_age=600, httponly=True, secure=True, samesite="lax"
    )
    return response


@router.get("/auth/callback")
def auth_callback(
    request: Request, code: str | None = Query(None), state: str | None = Query(None)
):
    if not workos.enabled():
        raise ApiError(503, "login not configured")
    # The state cookie is what ties this callback to a /login this browser
    # started; without the comparison anyone could replay a code at it.
    if not code or not state or state != request.cookies.get(config.STATE_COOKIE):
        return RedirectResponse("/login", status_code=302)
    try:
        email, user_id = workos.authenticate(code)
    except Exception as exc:
        print(f"auth: WorkOS code exchange failed: {exc}")
        raise ApiError(401, "login failed — start again at /login") from exc
    if not user_id:
        raise ApiError(401, "login failed — start again at /login")
    token = session.issue(
        {"email": email, "user_id": user_id, "exp": int(time.time()) + config.SESSION_TTL_S}
    )
    response = RedirectResponse("/replay.html", status_code=302)
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=config.SESSION_TTL_S,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    response.delete_cookie(config.STATE_COOKIE)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(config.SESSION_COOKIE)
    return response
