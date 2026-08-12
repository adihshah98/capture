# analyze — bug review

*Reviewed 2026-08-11, re-verified against `main` @ `70f5748`. Part 2 of 3:
[design](analyze-design-review.md) · **bugs** · [cleanups](analyze-cleanups.md).*

> **Re-verification note.** `capture/analyze/` is byte-identical since the first
> draft, so every defect below still stands as written. Line references into
> `server.py` were updated — it shrank from 2,242 to 1,731 lines — and the web layer
> now reaches the package through `capture/analysis_port.py`, so bug #2's call site
> reads `ap.build_cohort(...)` rather than `analyze.build_cohort(...)`. The blocking
> behaviour is unchanged.

Defects that produce wrong output, corrupt data, or cost money. Ordered by severity.
Structural concerns are in the [design review](analyze-design-review.md); mechanical
tidying is in [cleanups](analyze-cleanups.md).

**Numbering note:** these are independent of the "bug 1–10" numbering in the
superseded `docs/analyze-refactor.md` (deleted in `1a913be`). The mapping is given
where it applies.

| # | Severity | Defect | Status vs. old plan |
| --- | --- | --- | --- |
| 1 | 🔴 | ~~CLI samples cohorts from a stale SQLite file~~ — **fixed** | **new** |
| 2 | 🔴 | `create_cohort` blocks the event loop | **new** |
| 3 | 🔴 | Attribution parsers diverged | old bug 8, understated |
| 4 | 🟠 | ~~Sampling shortfall is silent~~ — **fixed** | old bug 3 |
| 5 | 🟠 | ~~`_cohort_order_sql` depends on a compat shim~~ — **fixed** | **new** |
| 6 | 🟠 | ~~Group B is structurally short-sampled~~ — **fixed** | **new** |
| 7 | 🟠 | ~~`pool_facets` unbounded `fetchall`~~ — **fixed** | old bug 4 |
| 8 | 🟡 | `COHORT_URL_CONTAINS` hardcodes one customer | old bug 9 |
| 9 | 🟡 | `_batch_jsonl_path` collides | old bug 2 |
| 10 | 🟡 | Extractor argument order is undefended | **new** |

---

## 1. 🔴 The CLI builds cohorts from a stale SQLite file — **fixed (deleted)**

`_cli_run_cohort` opened `data/index.db` with
`sqlite3.connect(index)` and calls `build_cohort` against it. The application runs on
**Postgres** — `capture/db.py` is psycopg, and `store.connect()` ignores its
`data_root` argument entirely.

That file still exists and still has rows:

```
tables: [('sessions',), ('cohorts',), ('cohort_sessions',), ('cohort_cuts',)]
sessions count: (141,)
```

So the CLI **did not fail**. It sampled a cohort from 141 orphaned pre-migration
sessions and wrote it into Postgres via `write_cohort` — wrong session ids,
persisted to the live store, presented as a real cohort. The `if not
index.is_file()` guard made it worse: it was a check that the wrong thing exists.

**Fixed by deleting the CLI.** `main`, `_cli_run_cohort`, `_cli_pool`,
`_cli_data_root`, the argparse block, the `__main__` guard and the `argparse` import
are gone, alongside [cleanup #1](analyze-cleanups.md)'s dead symbols. Nothing invoked
it — verified against `deploy.sh`, `deploy-staging.sh`, `setup.sh`, `package.json`
and the CI workflow.

Second payoff, now available: the CLI was the **only** caller passing a genuine
`sqlite3.Connection` into the sampling functions. They now serve one connection type,
so the false annotations ([cleanup #6](analyze-cleanups.md)) are fixable and the
Postgres port loses its dual-mode requirement. The `sqlite3` import stays until that
item lands, since those annotations still reference it.

**Still outstanding:** delete `data/index.db`, only after confirming
`store.migrate_sqlite_cohorts` has consumed it ([cleanup #8](analyze-cleanups.md)).

## 2. 🔴 `create_cohort` blocks the event loop

[`create_cohort`](../capture/server.py#L1045) is `async def`, and at
[server.py:1153](../capture/server.py#L1153) runs blocking psycopg I/O inline:

```python
with compat.cohort_connection(resolve_project(project).id) as conn:
    cohort = analyze.build_cohort(conn, ...)
```

On the event loop thread: a pool checkout that waits up to `CAPTURE_DB_WAIT_S`
(**default 5s**, [db.py:43](../capture/db.py#L43)), a `SET LOCAL search_path` round
trip, and one or two sorted queries over up to 5,000 rows — each sorting via
`compat.json_extract` (see #5). Under pool contention this stalls **every** request
the worker serves, including `/healthz` and `/ingest`.

The same file uses the correct idiom seven times (`asyncio.to_thread` at
`server.py:80, 113, 295, 304, 311, 764, 768`), and `/ingest` itself uses it three
times. This call site simply missed it.

**Fix:** wrap in `asyncio.to_thread`. One line, no refactor, do it first.

By contrast [`get_pool_facets`](../capture/server.py#L1032) is plain `def`, so
Starlette already runs it in the threadpool — that one is correct.

## 3. 🔴 The attribution parsers have diverged

Two parsers, one job: [`analyze._session_attribution`](../capture/analyze/__init__.py#L1937)
and [`server.parse_attribution`](../capture/server.py#L646). Measured on the current
tree:

| URL | `analyze` | `server` |
| --- | --- | --- |
| `?utm_source=google&utm_term=shoes&fbclid=abc` | `{'utm_source':'google','utm_term':'shoes','fbclid':True}` | `{'utm_source':'google','fbclid':True}` |
| `?utm_term=only` | `{'utm_term':'only'}` | `None` |
| `?fbclid=` | `{}` | `None` |
| `/p` | `{}` | `None` |

Four divergences: `utm_term` (analyze only); `fbclid` handled inside vs. outside the
key tuple; **return type** `{}` vs `None`; and empty-value semantics —
analyze uses `if params.get(key)` (drops `utm_source=0`), server uses
`if key not in params` (keeps it).

The `# change both together` comment at
[:1931](../capture/analyze/__init__.py#L1931) failed exactly as the forwarding
module's comment predicts a comment would.

**This is worse than a duplication smell.** Per
[deep-dive 01](deep-dive/01-ingest-analyze-seam.md), `sessions.attribution` is
written **once at ingest** under `coalesce(sessions.attribution, excluded.attribution)`
— first write wins, permanently. Meanwhile analyze re-parses `url` at read time for
prompt metadata. So one session genuinely has two attribution views that disagree:
`utm_term` reaches the model but never the index that slicing and cut-matching read.

**Fix:** one parser in a new `attribution.py` (pure, importable by both), returning
the **union** of keys — `utm_term` (prompt metadata) and `fbclid` (slicing) — with a
single consistent return type. Adapt the server's `or None` at its call site, not in
the shared function.

**Fixing the parser does not fix existing rows.** Sessions already indexed keep their
stored attribution. A backfill re-reading `sessions.url` and rewriting the column is
required, and is a separate task — `capture/backfill.py` is the natural home.

## 4. 🟠 ~~Sampling shortfall is silent~~ — **fixed**

[`_sample_matching_rows`](../capture/analyze/sampling.py) filters in Python
after SQL has already truncated:

```python
candidate_limit = min(COHORT_CANDIDATE_CAP, max(n * 20, n + len(exclude) + 200, 500))
...
for row in candidates:
    if not row_matches_slice(row, spec): continue
    out.append(...)
    if len(out) >= n: break
```

Two independent shortfalls. **(a)** `n * 20` assumes the slice matches ≥5% of rows;
a rare campaign or landing page falls outside the window and the `LIMIT` cuts it off.
**(b)** the loop breaks at `n` and never refills — no second query, no pagination.
`sample_cohort_rows` had the same shape.

Neither was reported. A cohort asked for 50 and given 3 was **presented as 50**, while
`_given_facts` tells the model "this group contains exactly 3 session(s)". The user
read a confident analysis of a population that does not exist. `build_cohort` never
checked whether the requested total was reached.

**Fix (landed):** sampling paginates up to `CANDIDATE_CAP`, and every cohort stores
`sample: {requested, matched, scanned, capped}`. `capped` is true only when the
scan hit the hard cap while still short — that is the case still fixable by
scanning harder or pushing the slice into SQL. The Insights progress line and
cohort settings view surface the shortfall.

## 5. 🟠 ~~`_cohort_order_sql` depends on a compat shim~~ — **fixed**

Sampling used to emit `coalesce(json_extract(interactions, '$.clicks'), 0)`.
`json_extract` is **SQLite**. It worked on Postgres only because
[0002_compat.sql](../migrations/0002_compat.sql) defined
`compat.json_extract(text, text) RETURNS numeric`.

The sort key round-tripped a plain integer column (`sessions.clicks`) out to JSON text
through the view and back through a regex-stripped path expression — per row, per
comparison — defeating any index.

**Fix (landed):** expose `clicks` (and `rage`) on `compat.sessions`, order by
`coalesce(clicks, 0)`, and drop `compat.json_extract` (`0005_compat_clicks.sql`).

## 6. 🟠 ~~Group B is structurally short-sampled~~ — **fixed**

[`sample_cohort`](../capture/analyze/sampling.py) samples A first with
no exclusions, then re-runs the **same deterministically ordered** query for B with
`exclude_ids = A`. B's window used to re-read exactly the rows A consumed and
discard them in Python after `LIMIT`.

B's effective window was smaller than A's by `|A|`. The `n + len(exclude) + 200` term
partially compensated but was capped by `COHORT_CANDIDATE_CAP` and did not apply when
`n * 20` dominated. So in an A/B cohort the B arm came up short first — a sampling
bias that read as a finding about the population.

**Fix (landed):** push `exclude_ids` into SQL as `session_id NOT IN (...)`, so B's
`LIMIT`/`OFFSET` window never spends slots on A's members. B's scan budget matches
A's.

## 7. 🟠 ~~`pool_facets` is an unbounded `fetchall`~~ — **fixed**

`pool_facets` had no `LIMIT`, no `ORDER BY`, called `.fetchall()`, then built a
dict per row with three `json.loads` each. It hardcoded `url_contains=""`, so
nothing narrowed it. Memory grew linearly with project size.

Not a loop-blocker (its endpoint is sync `def`), but it needed a bound —
`facets_from_rows` is a `GROUP BY` written in Python, and pushing
`device_class` / `page_path` into SQL would fork sampler semantics.

**Fix (landed):** scan at most `CANDIDATE_CAP` newest sessions (same window and
order as sampling), then classify in Python as before.

## 8. 🟡 `COHORT_URL_CONTAINS` hardcodes one customer

`COHORT_URL_CONTAINS = "/pages/ashwamag-01"`
([:84](../capture/analyze/__init__.py#L84)) — one customer's landing page as a global
default in a multi-tenant product. It becomes SQL as `url LIKE '%…%'`.

Seven textual occurrences but only **two behavioral**: the `Query` default at
[server.py:1054](../capture/server.py#L1054) — so any `POST /api/cohorts` omitting
`url_contains` is silently scoped to that page — and the CLI default (removed by #1).
The rest are signature defaults always overridden by callers, or label text.

**Fix:** env var, default `""` (= any). Note `test_server.py:1897`
(`test_sample_requires_ashwamag_path_and_min_duration`) pins the current behaviour by
name and must be updated. Most other tests already route around the default via
`create_cohort_url` ([test_server.py:884](../tests/test_server.py#L884), *"disables
production ashwamag/≥5s filters"*) — which is itself the signature of a bad default.

## 9. 🟡 `_batch_jsonl_path` collides — **fixed**

`base / f"capture-batch-{int(time.time()*1000)}.jsonl"`. Two cohorts submitting in
the same millisecond share a path, and the `finally: tmp.unlink()` has one delete the
other's payload mid-upload. Reachable from the live cohort path.

The default `Path("/var/tmp")` is also a multi-tenant exposure: the JSONL contains
session frames and metadata in a world-readable shared directory.

**Fixed** with `tempfile.mkstemp`, and `chmod(0o600)` stated explicitly rather than
left to `mkstemp`'s default, since the mode is the property being relied on. `mkstemp`
creates the file where the old name only described one, so the regression test also
covers the submit path's `open("w")` truncating rather than appending.

Covered by `tests/server/test_batch_tempfile.py` (fast tier — no database). Verified
against the old implementation: reverting the name turns three of its four red.

## 10. 🟡 Extractor argument order is undefended

[`run_extractor`](../capture/analyze/__init__.py#L1897) passes positionally:

```python
str(ANALYZE_FPS),
str(ANALYZE_MAX_FRAMES),
# Positional, and ahead of the variadic chunk names.
*chunk_names,
```

destructured at [extract.mjs:471](../src/analyze/extract.mjs#L471). Insert an argument
on one side only and the first chunk name is read as `maxFrames`. Verified:

```
Number('1700000000000-abcd1234-abcdef.json') -> NaN ;  NaN || 0 -> 0
```

So the frame cap silently disables **and** that chunk drops out of the analysis. No
error on either side. The comment acknowledges the coupling; nothing enforces it.

**Fix:** pass `--fps` / `--max-frames` as named flags, or have the extractor validate
`Number.isFinite` and fail loudly.

---

## Confirmed *not* bugs

- **`standout_line`** (old bug 7) is **live** — called at
  [:767](../capture/analyze/__init__.py#L767), renders a UI column. Only its
  `## What stands out` branch is dead. Deleting the function blanks a column. See
  [cleanups](analyze-cleanups.md).
- **`apply_cohort_batch` ordering** (old bug 10) is inert — `by_id` is keyed on
  `session_id` ([:2752](../capture/analyze/__init__.py#L2752)) and never read
  positionally. Sorting `prepared` is cosmetic.
- **The `write_text` "not ours" warning** in the superseded plan was stale. No
  atomic-write branch exists; `write_job`, `write_analysis_result` and the legacy
  migration are ours to change.

## Suggested order

1. **#2** — one line, whole-process stall.
2. **#1** — data corruption; unblocks later work.
3. **#3** — parser unification, then the backfill as a separate task.
4. **#8** — env var (small, and #1 removes one of its two sites).
5. **#4 + #6 + #7 + #5** — one project inside `sampling.py`, alongside the Postgres
   port. See [design review §4](analyze-design-review.md).
6. ~~**#9, #10** — hygiene.~~ **#9 done.** #10 remains: it needs a change in
   `src/analyze/extract.mjs`, which is the Node side and its own review surface.

Verification for each: `npm test` green, prompt hashes unchanged
(`5637eecc` / `d6416d2b` / `6007448b`), and for #3 specifically, assert both parsers
agree on the four URLs in the table above.
