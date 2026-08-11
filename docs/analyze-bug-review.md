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
| 1 | 🔴 | CLI samples cohorts from a stale SQLite file | **new** |
| 2 | 🔴 | `create_cohort` blocks the event loop | **new** |
| 3 | 🔴 | Attribution parsers diverged | old bug 8, understated |
| 4 | 🟠 | Sampling shortfall is silent | old bug 3 |
| 5 | 🟠 | `_cohort_order_sql` depends on a compat shim | **new** |
| 6 | 🟠 | Group B is structurally short-sampled | **new** |
| 7 | 🟠 | `pool_facets` unbounded `fetchall` | old bug 4 |
| 8 | 🟡 | `COHORT_URL_CONTAINS` hardcodes one customer | old bug 9 |
| 9 | 🟡 | `_batch_jsonl_path` collides | old bug 2 |
| 10 | 🟡 | Extractor argument order is undefended | **new** |

---

## 1. 🔴 The CLI builds cohorts from a stale SQLite file

[`_cli_run_cohort`](../capture/analyze/__init__.py#L2874) opens `data/index.db` with
`sqlite3.connect(index)` and calls `build_cohort` against it. The application runs on
**Postgres** — `capture/db.py` is psycopg, and `store.connect()` ignores its
`data_root` argument entirely.

That file still exists and still has rows:

```
tables: [('sessions',), ('cohorts',), ('cohort_sessions',), ('cohort_cuts',)]
sessions count: (141,)
```

So the CLI **does not fail**. It samples a cohort from 141 orphaned pre-migration
sessions and writes it into Postgres via `write_cohort` — wrong session ids,
persisted to the live store, presented as a real cohort. The
`if not index.is_file()` guard at [:2875](../capture/analyze/__init__.py#L2875) makes
it worse: it is a check that the wrong thing exists.

**Fix — delete the CLI** (decided). Remove `main`, `_cli_run_cohort`, `_cli_pool`,
`_cli_data_root`, the argparse block and the `__main__` guard (~135 lines,
`:2852-2987` + `:3025`), plus the then-unused `argparse` and `sqlite3` imports.

Second payoff: the CLI is the **only** caller passing a genuine `sqlite3.Connection`
into the sampling functions. Once gone, they serve one connection type, the false
annotations become fixable, and the Postgres port loses its dual-mode requirement.

**Before deleting:** confirm nothing invokes it — `deploy.sh`, `package.json`,
systemd units, cron. Delete `data/index.db` separately, only after confirming
`store.migrate_sqlite_cohorts` has consumed it.

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

## 4. 🟠 Sampling shortfall is silent

[`_sample_matching_rows`](../capture/analyze/__init__.py#L1483) filters in Python
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
`sample_cohort_rows:1609` has the same shape.

Neither is reported. A cohort asked for 50 and given 3 is **presented as 50**, while
`_given_facts` tells the model "this group contains exactly 3 session(s)". The user
reads a confident analysis of a population that does not exist. `build_cohort` never
checks whether the requested total was reached.

**Fix:** return `{requested, matched, scanned, capped}` and surface it on the cohort.
`capped` must distinguish "the pool genuinely holds 3" from "we stopped scanning at
`COHORT_CANDIDATE_CAP`" — only the second is fixable by scanning harder.

## 5. 🟠 `_cohort_order_sql` depends on a compat shim

[:1439](../capture/analyze/__init__.py#L1439) emits
`coalesce(json_extract(interactions, '$.clicks'), 0)`. `json_extract` is **SQLite**.
It works on Postgres only because
[0002_compat.sql:66](../migrations/0002_compat.sql#L66) defines
`compat.json_extract(text, text) RETURNS numeric`.

The sort key round-trips a plain integer column (`sessions.clicks`) out to JSON text
through the view and back through a regex-stripped path expression — per row, per
comparison — defeating any index.

**Consequence:** this is a **sixth** function the "port the five" task must handle.
Deleting `0002_compat.sql` without rewriting this ORDER BY takes down cohort sampling
entirely. It is invisible from the Python side.

## 6. 🟠 Group B is structurally short-sampled

[`sample_cohort_rows:1565`](../capture/analyze/__init__.py#L1565) samples A first with
no exclusions, then re-runs the **same deterministically ordered** query for B with
`exclude_ids = A`. B's window therefore re-reads exactly the rows A consumed and
discards them at [:1520](../capture/analyze/__init__.py#L1520).

B's effective window is smaller than A's by `|A|`. The `n + len(exclude) + 200` term
partially compensates but is capped by `COHORT_CANDIDATE_CAP` and does not apply when
`n * 20` dominates. So in an A/B cohort the B arm comes up short first — a sampling
bias that reads as a finding about the population.

**Fix:** same surface as #4; either push the exclusion into SQL (`NOT IN`) or offset
B's window by `|A|`.

## 7. 🟠 `pool_facets` is an unbounded `fetchall`

[:1394](../capture/analyze/__init__.py#L1394): no `LIMIT`, no `ORDER BY`,
`.fetchall()`, then a dict per row with three `json.loads` each. It hardcodes
`url_contains=""`, so nothing narrows it. Memory grows linearly with project size.

Not a loop-blocker (its endpoint is sync `def`), but it should be bounded or
aggregated in SQL — `facets_from_rows` is a `GROUP BY` written in Python.

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

## 9. 🟡 `_batch_jsonl_path` collides

[:2211](../capture/analyze/__init__.py#L2211):
`base / f"capture-batch-{int(time.time()*1000)}.jsonl"`. Two cohorts submitting in
the same millisecond share a path, and the `finally: tmp.unlink()` has one delete the
other's payload mid-upload. Reachable from the live cohort path
([:2242](../capture/analyze/__init__.py#L2242)).

The default `Path("/var/tmp")` is also a multi-tenant exposure: the JSONL contains
session frames and metadata in a world-readable shared directory.

**Fix:** `tempfile.mkstemp` in a configured directory.

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
6. **#9, #10** — hygiene.

Verification for each: `npm test` green, prompt hashes unchanged
(`5637eecc` / `d6416d2b` / `6007448b`), and for #3 specifically, assert both parsers
agree on the four URLs in the table above.
