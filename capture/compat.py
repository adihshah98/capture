"""Lets analyze.py's SQLite-era cohort queries run against Postgres unmodified.

analyze.py is owned by Anant's co-founder. Its five conn-taking functions
(pool_facets, build_cohort, sample_cohort_rows and two helpers) expect `?`
placeholders and the old flat `sessions` table. The compat schema from
migrations/0002_compat.sql supplies the shape; this supplies the placeholder
style. Both go away when those functions are ported.
"""

from contextlib import contextmanager

from . import db


def translate(sql: str, params) -> str:
    """`?` -> `%s`. Existing `%` must be doubled first or psycopg reads it as a
    placeholder, and only when params are passed, since psycopg skips
    interpolation entirely without them."""
    if not params:
        return sql
    return sql.replace("%", "%%").replace("?", "%s")


class SqliteStyleConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        return self._conn.execute(translate(sql, params), tuple(params))

    def __getattr__(self, name):
        return getattr(self._conn, name)


@contextmanager
def cohort_connection(project_id):
    """A connection analyze.py can query as though the SQLite index still existed."""
    with db.connection(project_id) as conn:
        conn.execute("SET LOCAL search_path = compat, public")
        yield SqliteStyleConnection(conn)
