"""Project identity, resolved from Postgres.

Replaces data/projects.json. The external handle stayed the storage prefix (the
token a project was created with, "" for the default pool) so existing replay
URLs and the UI keep working, while ingest credentials moved to project_tokens
and can now rotate without touching data.
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import db

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")
DATA_DIR = DATA_ROOT / "sessions"
PROJECTS_DIR = DATA_ROOT / "projects"

CACHE_TTL_S = float(os.environ.get("CAPTURE_PROJECT_CACHE_TTL_S", "5"))

_lock = threading.Lock()
_cache: Optional[list] = None
_cache_at = 0.0


class ProjectsUnavailable(Exception):
    """The project registry cannot be read. Callers must fail closed rather than
    fall back to a shared pool — that fallback was a cross-tenant auth bypass."""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    org_id: Optional[str]
    domains: tuple
    storage_prefix: str
    is_default: bool
    config: Optional[dict] = None
    tokens: tuple = field(default_factory=tuple)

    @property
    def handle(self) -> str:
        """What `?project=` carries. Stable across token rotation."""
        return "" if self.is_default else self.storage_prefix

    @property
    def pool(self) -> Path:
        return DATA_DIR if self.is_default else PROJECTS_DIR / self.storage_prefix / "sessions"


def _row_to_project(row) -> Project:
    pid, name, org_id, domains, prefix, is_default, config, tokens = row
    if isinstance(domains, str):
        domains = json.loads(domains)
    return Project(
        id=str(pid),
        name=name,
        org_id=org_id,
        domains=tuple(domains or ()),
        storage_prefix=prefix or "",
        is_default=bool(is_default),
        config=config,
        tokens=tuple(tokens or ()),
    )


def _load() -> list:
    with db.connection(admin=True) as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.org_id, p.domains, p.storage_prefix, p.is_default, p.config,
                   array_remove(array_agg(t.token) FILTER (WHERE t.revoked_at IS NULL), NULL)
            FROM projects p
            LEFT JOIN project_tokens t ON t.project_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at
            """
        ).fetchall()
    return [_row_to_project(r) for r in rows]


def all_projects(fresh: bool = False) -> list:
    global _cache, _cache_at
    with _lock:
        stale = _cache is None or (time.monotonic() - _cache_at) > CACHE_TTL_S
        if fresh or stale:
            try:
                _cache = _load()
            except Exception as exc:
                raise ProjectsUnavailable(str(exc)) from exc
            _cache_at = time.monotonic()
        return list(_cache)


def invalidate() -> None:
    global _cache, _cache_at
    with _lock:
        _cache = None
        _cache_at = 0.0


def by_token(token: Optional[str]) -> Optional[Project]:
    """The project a recording belongs to. A tokenless batch only resolves when
    a default project exists, which is what single-tenant installs use."""
    if not token:
        return default()
    for project in all_projects():
        if token in project.tokens:
            return project
    for project in all_projects(fresh=True):
        if token in project.tokens:
            return project
    return None


def by_handle(handle: Optional[str]) -> Optional[Project]:
    for project in all_projects():
        if project.handle == (handle or ""):
            return project
    return None


def by_id(project_id: str) -> Optional[Project]:
    for project in all_projects():
        if project.id == str(project_id):
            return project
    return None


def default() -> Optional[Project]:
    for project in all_projects():
        if project.is_default:
            return project
    return None


def for_orgs(orgs: set) -> list:
    return [p for p in all_projects() if p.org_id and p.org_id in orgs]


def create(name: str, token: str, *, is_default: bool = False) -> Project:
    with db.connection(admin=True) as conn:
        pid = conn.execute(
            "INSERT INTO projects (name, storage_prefix, is_default)"
            " VALUES (%s, %s, %s) RETURNING id",
            (name, "" if is_default else token, is_default),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tokens (token, project_id) VALUES (%s, %s)", (token, pid)
        )
    invalidate()
    return by_id(str(pid))


def set_org(project: Project, org_id: str) -> None:
    with db.connection(admin=True) as conn:
        conn.execute("UPDATE projects SET org_id = %s WHERE id = %s", (org_id, project.id))
    invalidate()


def set_domains(project: Project, domains: list) -> None:
    with db.connection(admin=True) as conn:
        conn.execute(
            "UPDATE projects SET domains = %s WHERE id = %s",
            (json.dumps(domains), project.id),
        )
    invalidate()


def revoke_token(token: str) -> None:
    with db.connection(admin=True) as conn:
        conn.execute(
            "UPDATE project_tokens SET revoked_at = now() WHERE token = %s AND revoked_at IS NULL",
            (token,),
        )
    invalidate()


def delete(project: Project) -> None:
    with db.connection(admin=True) as conn:
        conn.execute("DELETE FROM projects WHERE id = %s", (project.id,))
    invalidate()


def usage(project: Project) -> tuple:
    with db.connection(project.id) as conn:
        row = conn.execute(
            "SELECT count(*), coalesce(sum(bytes), 0) FROM sessions"
        ).fetchone()
    return int(row[0]), int(row[1])
