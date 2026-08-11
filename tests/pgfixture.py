"""An ephemeral Postgres for tests.

Honours CAPTURE_TEST_DATABASE_ADMIN_URL when one is provided (CI service
container); otherwise runs initdb/pg_ctl in a temp directory, so the suite needs
neither docker nor a running daemon.

The app always connects as a NOSUPERUSER role. A superuser bypasses row-level
security outright, which would let the isolation tests pass without proving
anything.
"""

import atexit
import glob
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import psycopg

_cluster = None

APP_ROLE = "capture_app"
APP_PASSWORD = "capture_app"

UNAVAILABLE = (
    "postgres unavailable — install it, or set CAPTURE_TEST_DATABASE_ADMIN_URL"
)


def _binary(name: str) -> Optional[str]:
    """Newest Postgres on the box; keg-only formulae are not on PATH."""
    versioned = sorted(
        glob.glob(f"/usr/local/opt/postgresql@*/bin/{name}")
        + glob.glob(f"/opt/homebrew/opt/postgresql@*/bin/{name}"),
        key=lambda p: int(re.search(r"postgresql@(\d+)", p).group(1)),
        reverse=True,
    )
    return versioned[0] if versioned else shutil.which(name)


def available() -> bool:
    """Whether a usable Postgres is reachable. CAPTURE_REQUIRE_POSTGRES=1 turns a
    missing one into a hard failure, so CI can never silently skip the tenant
    isolation guard."""
    ok = bool(os.environ.get("CAPTURE_TEST_DATABASE_ADMIN_URL")) or bool(
        _binary("initdb") and _binary("pg_ctl")
    )
    if not ok and os.environ.get("CAPTURE_REQUIRE_POSTGRES") == "1":
        raise RuntimeError(UNAVAILABLE)
    return ok


class Cluster:
    def __init__(self, admin_url: str, base_dir=None, port=None):
        self.admin_url = admin_url
        self.base_dir = base_dir
        self.port = port

    def stop(self):
        if self.base_dir:
            subprocess.run(
                [_binary("pg_ctl"), "-D", str(Path(self.base_dir) / "pg"),
                 "-m", "immediate", "stop"],
                capture_output=True,
            )
            shutil.rmtree(self.base_dir, ignore_errors=True)
            self.base_dir = None


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_local_cluster() -> Cluster:
    base = tempfile.mkdtemp(prefix="capture-pg-")
    data = Path(base) / "pg"
    port = _free_port()
    subprocess.run(
        [_binary("initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            _binary("pg_ctl"), "-D", str(data),
            "-o", f"-p {port} -k {data} -c listen_addresses= -c fsync=off",
            "-l", str(Path(base) / "pg.log"), "start",
        ],
        check=True,
        capture_output=True,
    )
    admin_url = f"postgresql://postgres@/postgres?host={data}&port={port}"
    for _ in range(60):
        try:
            with psycopg.connect(admin_url, connect_timeout=2):
                break
        except Exception:
            time.sleep(0.25)
    else:
        raise RuntimeError("ephemeral postgres did not accept connections")
    return Cluster(admin_url, base_dir=base, port=port)


def cluster() -> Cluster:
    global _cluster
    if _cluster is None:
        external = os.environ.get("CAPTURE_TEST_DATABASE_ADMIN_URL")
        _cluster = Cluster(external) if external else _start_local_cluster()
        atexit.register(_cluster.stop)
    return _cluster


def fresh_database() -> tuple:
    """A new database with migrations applied. Returns (admin_url, app_url)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import db

    name = f"capture_test_{uuid.uuid4().hex[:12]}"
    root = cluster().admin_url
    with psycopg.connect(root, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')

    admin_url = _swap_database(root, name)

    # The role has to exist before migrating: on PostgreSQL 14 the compat view
    # is kept inside RLS by being owned by it.
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)
        ).fetchone()
        if not exists:
            conn.execute(
                f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}' NOSUPERUSER NOBYPASSRLS"
            )

    os.environ["CAPTURE_DB_APP_ROLE"] = APP_ROLE
    db.migrate(admin_url)

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO {APP_ROLE}')
        for schema in ("public", "compat"):
            conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {APP_ROLE}")
            conn.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA"
                f" {schema} TO {APP_ROLE}"
            )
        conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    return admin_url, _with_role(admin_url, APP_ROLE, APP_PASSWORD)


def seed_default_project(admin_url: str, name: str = "Default") -> str:
    """The project a tokenless ingest lands in — single-tenant mode's successor."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        return str(
            conn.execute(
                "INSERT INTO projects (name, storage_prefix, is_default)"
                " VALUES (%s, '', true) RETURNING id",
                (name,),
            ).fetchone()[0]
        )


def seed_project(admin_url: str, name: str, token: str, domains=None) -> str:
    import json as _json

    with psycopg.connect(admin_url, autocommit=True) as conn:
        pid = conn.execute(
            "INSERT INTO projects (name, storage_prefix, domains)"
            " VALUES (%s, %s, %s) RETURNING id",
            (name, token, _json.dumps(domains or [])),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO project_tokens (token, project_id) VALUES (%s, %s)", (token, pid)
        )
    return str(pid)


def set_project_org(admin_url: str, token: str, org_id: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE projects SET org_id = %s WHERE id ="
            " (SELECT project_id FROM project_tokens WHERE token = %s)",
            (org_id, token),
        )


def seed_session(admin_url: str, token: str, session_id: str, **cols) -> None:
    """An indexed session for a project, without going through /ingest."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO sessions (project_id, session_id, first_seen, last_seen,"
            " chunk_count, bytes)"
            " VALUES ((SELECT project_id FROM project_tokens WHERE token = %s),"
            " %s, %s, %s, 1, 100)",
            (token, session_id, cols.get("first_seen", 1000), cols.get("last_seen", 2000)),
        )


READONLY_ROLE = "capture_readonly"


def seed_readonly_role(admin_url: str) -> str:
    """SELECT-only role for a mirror. Mirrors the production grant exactly."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (READONLY_ROLE,)
        ).fetchone():
            conn.execute(
                f"CREATE ROLE {READONLY_ROLE} LOGIN PASSWORD '{READONLY_ROLE}'"
                " NOSUPERUSER NOBYPASSRLS"
            )
        db_name = admin_url.partition("?")[0].rsplit("/", 1)[1]
        conn.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO {READONLY_ROLE}')
        for schema in ("public", "compat"):
            conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {READONLY_ROLE}")
            conn.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {READONLY_ROLE}")
    return _with_role(admin_url, READONLY_ROLE, READONLY_ROLE)


def _swap_database(url: str, name: str) -> str:
    head, _, query = url.partition("?")
    base = head.rsplit("/", 1)[0]
    return f"{base}/{name}" + (f"?{query}" if query else "")


def _with_role(url: str, role: str, password: str) -> str:
    scheme, _, rest = url.partition("://")
    _, _, host_and_path = rest.partition("@")
    if not host_and_path:
        host_and_path = rest
    return f"{scheme}://{role}:{password}@{host_and_path}"


class CohortStoreMixin:
    """A Postgres-backed cohort store for tests that used to get SQLite.

    One database per class; cohorts are cleared between tests. The pool the
    tests build resolves to pool_key "" — the default project — so that is what
    gets seeded.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import db
        import projects

        cls._pg_admin, cls._pg_app = fresh_database()
        cls._pg_project = seed_default_project(cls._pg_admin)
        # The pool-isolation tests build a project pool named "acme".
        seed_project(cls._pg_admin, "Acme", "acme")
        cls._pg_saved = os.environ.get("CAPTURE_DATABASE_URL")
        os.environ["CAPTURE_DATABASE_URL"] = cls._pg_app
        cls._pg_db, cls._pg_projects = db, projects
        db.close_pool()
        projects.invalidate()

    @classmethod
    def tearDownClass(cls):
        cls._pg_db.close_pool()
        if cls._pg_saved is None:
            os.environ.pop("CAPTURE_DATABASE_URL", None)
        else:
            os.environ["CAPTURE_DATABASE_URL"] = cls._pg_saved
        cls._pg_projects.invalidate()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        with psycopg.connect(self._pg_admin, autocommit=True) as conn:
            # Cuts have no foreign key to cohorts, so they do not cascade.
            conn.execute("DELETE FROM cohort_cuts")
            conn.execute("DELETE FROM cohorts")
        self._pg_projects.invalidate()
