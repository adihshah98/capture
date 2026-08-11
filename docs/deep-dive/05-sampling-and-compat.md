# 05 — Sampling and the compat layer

How a cohort's members are chosen, why five functions still emit SQLite SQL against
a Postgres database, and where the silent wrong answers come from.

---

## The constraint: a file was written against a database that no longer exists

The cohort sampler was written when the session index was a local SQLite file
(`data/index.db`) with a flat `sessions` table, JSON-text columns, and `?`
placeholders. The application has since moved to Postgres with a normalised schema,
row-level security, and `%s` placeholders.

Rewriting the sampler was deferred. The reason is recorded in
[compat.py:1](../../capture/compat.py#L1):

> ```python
> """Lets analyze.py's SQLite-era cohort queries run against Postgres unmodified.
>
> analyze.py is owned by Anant's co-founder. Its five conn-taking functions
> (pool_facets, build_cohort, sample_cohort_rows and two helpers) expect `?`
> placeholders and the old flat `sessions` table. The compat schema from
> migrations/0002_compat.sql supplies the shape; this supplies the placeholder
> style. Both go away when those functions are ported.
> """
> ```

So the compat layer is **organisational, not technical** — it exists to avoid editing
someone else's file. That is worth stating plainly, because it means the layer has no
engineering reason to survive the moment that constraint lifts.

## The design: two halves

**Shape** — [`migrations/0002_compat.sql`](../../migrations/0002_compat.sql) defines
`compat.sessions`, a view that flattens the normalised schema back into the old row:
`project_id::text AS pool`, the `orders` LEFT JOIN re-collapsed into a JSON-text
`"order"` column, `jsonb_build_object('clicks', …)::text AS interactions`, and
`device`/`attribution` cast `::text`.

**Placeholders** — [`compat.py:15`](../../capture/compat.py#L15) rewrites `?` → `%s`:

```python
if not params:
    return sql
return sql.replace("%", "%%").replace("?", "%s")
```

Order matters and is tested: existing `%` must be doubled *first* or psycopg reads it
as a placeholder. And it only fires when params are passed, because psycopg skips
interpolation entirely without them
([test_compat.py:152](../../tests/test_compat.py#L152)).

`cohort_connection` sets `search_path = compat, public` so an unqualified
`FROM sessions` resolves to the view.

---

## The two casts that are contracts

### `::text` — because `json.loads` is on the other side

> ```sql
> -- SQLite handed back JSON columns as TEXT, and _parse_index_json calls
> -- json.loads on them. Returning jsonb would make psycopg produce dicts, which
> -- that helper silently turns into None — so every column is cast back to text.
> ```

[`_parse_index_json`](../../capture/analyze/__init__.py#L1311) returns `None` for a
non-string input. Remove the casts and psycopg hands back `dict`s, `json.loads`
is never reached, and **every session reads as though it never bought anything** —
silently. `test_compat.py:96-105` pins this with `assertIsInstance(order, str)`.

### `security_invoker` — the cross-tenant leak

The most dangerous comment block in the repo:

> ```sql
> -- A view executes as its owner, so one owned by a superuser bypasses row level
> -- security on the tables underneath and quietly hands one customer another's
> -- sessions. Two ways to stay inside RLS: security_invoker (PostgreSQL 15+), or
> -- an owner that is itself subject to RLS. Refuse to build the view if neither
> -- is available rather than build a leaky one.
> ```

The migration **refuses to build** rather than build a leaky view
([0002_compat.sql:24](../../migrations/0002_compat.sql#L24)). `pgfixture.py:137`
creates the app role `NOSUPERUSER NOBYPASSRLS` specifically so the isolation tests
are not vacuous — a superuser role would pass them all while proving nothing.

What this buys is checked directly: `sample_cohort_rows` called with **another
tenant's pool id** returns `[]`
([test_compat.py:136](../../tests/test_compat.py#L136)). Even though the sampler
interpolates a caller-supplied `pool` into `WHERE pool = ?`, RLS makes it
unexploitable. Any replacement for this view must reproduce that property.

### `compat.json_extract` — the shim nobody expects

[`_cohort_order_sql`](../../capture/analyze/__init__.py#L1438) emits:

```python
activity = "coalesce(json_extract(interactions, '$.clicks'), 0)"
```

`json_extract` is a **SQLite** function. It works on Postgres only because
[0002_compat.sql:66](../../migrations/0002_compat.sql#L66) defines
`compat.json_extract(text, text) RETURNS numeric`.

So the sort key round-trips a plain integer column (`sessions.clicks`) into JSON text
via the view, then back out through a regex-stripped path expression, **per row, per
comparison** — and defeats any index on it. This is a sixth function the "port the
five" task must handle; deleting the migration without rewriting this ORDER BY takes
down cohort sampling.

---

## Where the wrong answers come from

The sampler pushes **cheap** predicates into SQL and evaluates **selective** ones in
Python — which is exactly backwards for correctness.

In SQL ([`_cohort_where`](../../capture/analyze/__init__.py#L1406)): `pool`,
`chunk_count > 0`, duration, `url LIKE`, `last_seen` bounds, `ORDER BY`, `LIMIT`.

In Python ([:1522](../../capture/analyze/__init__.py#L1522)): the entire slice —
outcome, device, page, and every `utm_*` — via `row_matches_slice`.

### Shortfall 1: the window is a guess

```python
candidate_limit = min(
    COHORT_CANDIDATE_CAP,
    max(n * 20, n + len(exclude) + 200, 500),
)
```

`n * 20` assumes the slice matches at least 5% of rows. For a rare campaign or an
uncommon landing page it does not, the SQL `LIMIT` truncates before enough matches
appear, and the function returns short. `COHORT_CANDIDATE_CAP = 5000` hard-caps it
regardless of `n`.

### Shortfall 2: the loop breaks and never refills

```python
out.append({"session_id": sid, "filter": tag, "status": "queued"})
if len(out) >= n:
    break
```

There is no second query, no cursor advance, no pagination. If the first window
under-delivers, that is the final answer.

**Neither shortfall is reported.** A cut asked for 50 sessions and given 3 is
presented as a cut over 50 — and `_given_facts` will dutifully tell the model
*"This group contains exactly 3 session(s)"* while the UI label says the cohort was
sampled at 50. The user sees a confident analysis of a population that does not exist.

`build_cohort` never checks whether `len(sessions)` reached the requested total; the
only guard is `server.py`'s "no sessions matched" for the fully-empty case.

### Shortfall 3: group B is structurally disadvantaged

[`sample_cohort_rows:1565`](../../capture/analyze/__init__.py#L1565) samples A first
with no exclusions, then re-runs the **same** ordered query for B with
`exclude_ids = A`. Because the ordering is deterministic, B's window re-reads exactly
the rows A already consumed and discards them. B's effective window is smaller than
A's by `|A|`, so in an A/B cohort the B arm comes up short first — a sampling bias
that looks like a finding about the population.

### `pool_facets` is unbounded

[:1394](../../capture/analyze/__init__.py#L1394) has no `LIMIT` and no `ORDER BY`,
calls `.fetchall()`, and builds a dict per row with three `json.loads` each. It also
hardcodes `url_contains=""`, so nothing narrows it. It is at least called from a sync
`def` endpoint, so Starlette runs it in the threadpool — unlike `build_cohort`, which
is `await`ed from an `async def` and blocks the event loop.

---

## The annotations are false

All five functions declare `conn: sqlite3.Connection`
([:1387](../../capture/analyze/__init__.py#L1387),
[:1446](../../capture/analyze/__init__.py#L1446),
[:1484](../../capture/analyze/__init__.py#L1484),
[:1531](../../capture/analyze/__init__.py#L1531),
[:1775](../../capture/analyze/__init__.py#L1775)).

What arrives from the server is a `compat.SqliteStyleConnection` wrapping psycopg.
The annotation is accurate on exactly one path — the CLI at
[:2893](../../capture/analyze/__init__.py#L2893), which opens a real SQLite file that
is no longer the live database. That path is a bug in its own right (see the bug
review); removing it makes these annotations honestly fixable.

---

## The invariant you would break

1. **Do not drop the `::text` casts** to "return proper JSON." Every order becomes
   `None` and every session reads as a non-purchase.
2. **Do not replace the view without reproducing `security_invoker`** or an
   RLS-subject owner. This is a cross-tenant data leak, and the sampler's
   caller-supplied `pool` is what RLS is defending.
3. **Do not delete `0002_compat.sql` before rewriting `_cohort_order_sql`** — the
   `json_extract` shim is invisible from the Python side.
4. **Do not push `device_class` or `page_path` into SQL** as part of a port.
   Reimplementing them in SQL forks the semantics, which is the attribution-twins
   failure at higher stakes — and `page_path` returning `/` instead of `None` was
   already a fixed bug (`f74531a`).
