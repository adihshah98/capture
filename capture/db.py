import os
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def database_url() -> str:
    url = os.environ.get("CAPTURE_DATABASE_URL", "")
    if not url:
        raise RuntimeError("CAPTURE_DATABASE_URL is not set")
    return url


def admin_url() -> str:
    return os.environ.get("CAPTURE_DATABASE_ADMIN_URL") or database_url()


def configured() -> bool:
    return bool(os.environ.get("CAPTURE_DATABASE_URL"))


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    database_url(),
                    min_size=1,
                    max_size=int(os.environ.get("CAPTURE_DB_POOL_MAX", "10")),
                    # Fail fast when the database is down. The default 30s wait
                    # would queue ingests behind an outage until they time out
                    # client-side; a prompt 503 lets the recorder retry instead.
                    timeout=float(os.environ.get("CAPTURE_DB_WAIT_S", "5")),
                    open=True,
                )
    return _pool


def close_pool(timeout: float = 3.0) -> None:
    """Bounded on purpose: with the database unreachable the pool's workers are
    mid-retry, and an unbounded close makes the process impossible to stop
    cleanly — which is how a deploy turns into a SIGKILL."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close(timeout=timeout)
            except Exception as exc:
                print(f"db: pool did not close cleanly: {exc}")
            _pool = None


@contextmanager
def connection(project_id=None, admin: bool = False):
    """A transaction scoped to one project. Every RLS-protected table is invisible
    unless project_id matches or admin is set, so a query that forgets its WHERE
    clause returns nothing instead of another tenant's rows."""
    if not admin and project_id is None:
        raise ValueError("connection() needs a project_id, or admin=True")
    with pool().connection() as conn:
        with conn.cursor() as cur:
            if admin:
                cur.execute("SELECT set_config('app.admin', 'on', true)")
            else:
                cur.execute("SELECT set_config('app.project_id', %s, true)", (str(project_id),))
        yield conn


def applied_versions(conn) -> set:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(url: str | None = None) -> list:
    """Apply pending migrations in filename order. Each runs in its own
    transaction so a failure leaves the ones before it committed."""
    target = url or admin_url()
    applied = []
    with psycopg.connect(target) as conn:
        conn.autocommit = True
        done = applied_versions(conn)
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem
        if version in done:
            continue
        with psycopg.connect(target) as conn:
            with conn.transaction():
                role = os.environ.get("CAPTURE_DB_APP_ROLE")
                if role:
                    conn.execute("SELECT set_config('capture.app_role', %s, true)", (role,))
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


if __name__ == "__main__":
    for version in migrate():
        print(f"applied {version}")
