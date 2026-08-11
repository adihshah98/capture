"""Request-level helpers every router needs: who is asking, and what they may see.

These are the reason a router split needs a shared module at all —
require_read_access alone has call sites in the sessions, cohorts and auth groups,
so it cannot live with any one of them.

Each raises ApiError rather than returning a status, so a handler that forgets to
check fails closed at the first thing it tries to resolve.
"""

from pathlib import Path

from fastapi import Request

from .. import config, paths, projects, workos
from ..errors import ApiError


def allowed_projects(request: Request) -> set | None:
    """Handles this caller may read, or None meaning "no restriction".

    None is deliberately the permissive answer: it covers the operator list and
    the single-tenant install where auth is off entirely. Callers must test
    `is not None` rather than truthiness — an empty set means a logged-in user who
    may see nothing, which is the opposite of None.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None or (auth.get("email") or "").lower() in config.OPERATORS:
        return None
    orgs = workos.user_orgs(auth.get("user_id") or "")
    return {p.handle for p in projects.for_orgs(orgs)}


def require_read_access(request: Request, project: str | None) -> None:
    allowed = allowed_projects(request)
    if allowed is not None and (not project or project not in allowed):
        raise ApiError(403, "not your project")


def resolve_project(handle: str | None) -> projects.Project:
    found = projects.by_handle(handle)
    if found is None:
        raise ApiError(404, "unknown project")
    return found


def resolve_pool(project: str | None) -> Path:
    return resolve_project(project).pool


def resolve_session(session_id: str, project: str | None) -> Path:
    """The session's directory, 404ing when it is not there.

    Still a directory because the analyser is file-based and the legacy sidecar
    reader needs one; see capture/analysis_port.py. Several callers invoke this
    purely for the 404.
    """
    if not paths.SAFE_ID.match(session_id):
        raise ApiError(400, "invalid session_id")
    path = resolve_pool(project) / session_id
    if not path.is_dir():
        raise ApiError(404, "not found")
    return path
