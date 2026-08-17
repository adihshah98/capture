"""The database is async. These are the mistakes that quietly undo that.

`capture/db.py` runs two pools. `connection()` is async and serves the server;
`sync_connection()` blocks and exists for callers with no event loop — the CLI,
migrations, the project registry that both share. Mixing them up does not fail
loudly: a blocking call from a coroutine returns the right answer and simply
stops the event loop while it runs, so every other tenant's request waits on it.
That is invisible in tests and shows up in production as unexplained latency.

Two static guards, for the two ways the split gets broken:

  * a coroutine reaching `sync_connection` (or the raw sync `pool`), which
    blocks the loop;
  * `await f(...).fetchone()` / `await f(...)[0]`, where `await` binds to the
    whole postfix expression and the attribute is read off a coroutine object
    rather than a cursor. This one *does* fail at runtime, but only on the code
    path that runs it, which in a migration or an error branch can be much
    later than the change that introduced it. Every one of these was a real bug
    caught during the async port.
"""

import ast
import unittest
from pathlib import Path

CAPTURE = Path(__file__).resolve().parent.parent.parent / "capture"

# Modules with no event loop of their own. They are the reason `sync_connection`
# exists, so reaching for it there is correct rather than a mistake.
SYNC_BY_DESIGN = {
    "cli.py",
    "db.py",
    "projects.py",
    "backfill.py",
    "s3copy.py",
}

# Attributes that only exist on a cursor, never on a coroutine.
CURSOR_ONLY = {"fetchall", "fetchone", "fetchmany", "rowcount"}


def _sources():
    return sorted(p for p in CAPTURE.rglob("*.py") if "__pycache__" not in p.parts)


def _parents(tree):
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _enclosing_async(node, parents):
    """The `async def` this node sits in, or None.

    A plain `def` is a barrier: a synchronous helper nested inside a coroutine
    does not itself run on the loop unless something awaits it, and the ones in
    this tree are handed to `to_thread` deliberately.
    """
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.AsyncFunctionDef):
            return parent.name
        if isinstance(parent, ast.FunctionDef):
            return None
        parent = parents.get(parent)
    return None


class TestCoroutinesUseTheAsyncPool(unittest.TestCase):
    def test_no_coroutine_reaches_the_blocking_pool(self):
        offenders = []
        for source in _sources():
            if source.name in SYNC_BY_DESIGN:
                continue
            tree = ast.parse(source.read_text(), filename=str(source))
            parents = _parents(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in ("sync_connection", "pool"):
                    continue
                if not (isinstance(func.value, ast.Name) and func.value.id == "db"):
                    continue
                holder = _enclosing_async(node, parents)
                if holder is None:
                    continue
                offenders.append(
                    f"{source.relative_to(CAPTURE)}:{node.lineno} "
                    f"{holder}() calls db.{func.attr}() — use the async db.connection()"
                )
        self.assertEqual(
            offenders,
            [],
            "blocking database access on the event loop:\n" + "\n".join(offenders),
        )

    def test_the_sync_exemption_list_is_not_stale(self):
        """A file that no longer exists would silently widen the exemption."""
        missing = [name for name in SYNC_BY_DESIGN if not (CAPTURE / name).exists()]
        self.assertEqual(missing, [], f"SYNC_BY_DESIGN names files that are gone: {missing}")


class TestAwaitBindsWhereItLooksLikeItDoes(unittest.TestCase):
    """`await f(...).fetchone()` reads `.fetchone` off the coroutine, not the row."""

    def test_no_cursor_attribute_is_read_off_a_coroutine(self):
        offenders = []
        for source in _sources():
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Await):
                    continue
                value = node.value
                target = value.func if isinstance(value, ast.Call) else value
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Call)
                    and target.attr in CURSOR_ONLY
                ):
                    offenders.append(
                        f"{source.relative_to(CAPTURE)}:{node.lineno} "
                        f"await <call>.{target.attr}() — bind the cursor first, "
                        f"or use db.fetchone/db.fetchall"
                    )
                if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Call):
                    offenders.append(
                        f"{source.relative_to(CAPTURE)}:{node.lineno} "
                        f"await <call>[...] — indexes the coroutine; wrap it as "
                        f"(await call)[...]"
                    )
        self.assertEqual(offenders, [], "await-binding bugs:\n" + "\n".join(offenders))


class TestTheAsyncPoolIsBuiltOnce(unittest.TestCase):
    """A cold start must produce exactly one pool, however many callers race.

    `apool()` awaits `open()` between checking the cache and filling it. Without
    a lock every coroutine that arrives during that window gets past the check
    and builds its own pool; all but the last are then dropped on the floor
    still holding backends, which is a connection leak that only shows up under
    the concurrency of a real cold start.

    No database is needed: the pool class is replaced with a stub that records
    construction and yields at `open()`, which is the whole race.
    """

    def test_a_racing_cold_start_creates_one_pool(self):
        import asyncio
        from unittest import mock

        from capture import db

        built = []

        class FakePool:
            def __init__(self, *args, **kwargs):
                built.append(self)

            async def open(self):
                # A real open does I/O. Yielding here is what lets the other
                # waiters run, which is exactly the window being guarded.
                await asyncio.sleep(0)

        async def main():
            with (
                mock.patch.object(db, "AsyncConnectionPool", FakePool),
                mock.patch.object(db, "database_url", lambda: "postgresql:///unused"),
            ):
                await asyncio.gather(*(db.apool() for _ in range(8)))

        previous, previous_loop = db._apool, db._apool_loop
        try:
            db.forget_apool()
            asyncio.run(main())
            self.assertEqual(len(built), 1, f"cold start built {len(built)} pools, expected 1")
        finally:
            db._apool, db._apool_loop = previous, previous_loop

    def test_a_stale_pool_can_be_dropped_without_a_loop(self):
        """Teardown paths have no loop to await a close on."""
        from capture import db

        self.assertTrue(callable(db.forget_apool))


if __name__ == "__main__":
    unittest.main()
