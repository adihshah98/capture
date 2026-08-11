# analyze — cleanups

*Reviewed 2026-08-11 against `main` @ `d2f35e8`. Part 3 of 3:
[design](analyze-design-review.md) · [bugs](analyze-bug-review.md) · **cleanups**.*

Mechanical tidying: dead code, duplication, stale annotations, tooling. Nothing here
changes behaviour — anything that does is in the [bug review](analyze-bug-review.md).

Each item is independently safe. They are good "warm-up" commits before the
structural work, and good filler between the larger steps.

---

## 1. Dead code

Verified by grep across `capture/`, `tests/` and `src/`.

| Symbol | Location | Refs | Action |
| --- | --- | --- | --- |
| `submit_gemini_batch` | [:2163](../capture/analyze/__init__.py#L2163) | **definition only** | delete |
| `_estimate_inlined_bytes` | [:2155](../capture/analyze/__init__.py#L2155) | only from the above | delete |
| `ANALYZE_BATCH_INLINE_MAX_BYTES` | [:111](../capture/analyze/__init__.py#L111) | only from the above | delete |
| `status_path` | [:149](../capture/analyze/__init__.py#L149) | **definition only** | delete |

`submit_gemini_batch` is the non-cohort inline-batch branch. Deleting it removes the
inline path entirely — cohorts already always use the Files API
([deep-dive 03](deep-dive/03-model-paths-batch-and-sync.md)). **Confirm that is
intended** before deleting; if a non-cohort batch entry point is planned, keep it and
mark it as such.

`status_path` is a leftover from when job status was a file; job state moved to
Postgres in `0004_analyses.sql`.

**Also removed by [bug #1](analyze-bug-review.md)** (the CLI deletion): `main`,
`_cli_run_cohort`, `_cli_pool`, `_cli_data_root`, the argparse block, the `__main__`
guard, and the then-unused `argparse` / `sqlite3` imports — ~135 lines.

## 2. Re-exports with no consumers

The `from .slices import` / `from .stats import` blocks
([:779](../capture/analyze/__init__.py#L779),
[:942](../capture/analyze/__init__.py#L942)) exist because "server.py and the tests
reach for these off `analyze`." Nine of them do not:

| Symbol | External refs |
| --- | --- |
| `META_SOURCES`, `slice_is_empty`, `is_meta_attribution` | 0 |
| `cross_tab_cells`, `CROSS_TAB_PAIRS`, `CROSS_TAB_DIMENSIONS`, `CROSS_TAB_MIN_CELL`, `CROSS_TAB_MIN_COVERAGE`, `CROSS_TAB_MIN_LIFT_PTS` | 0 |
| `MOBILE_MAX_WIDTH` | 1 (keep) |

Narrowing the re-export lists to what is actually consumed shrinks the forwarding
surface (fewer multi-owner collisions — see
[deep-dive 07](deep-dive/07-patch-forwarding.md)) and makes the remaining exports a
deliberate public API rather than a dump.

Low risk, but do it **after** the forwarding mechanism test exists, since re-exports
are what the mechanism operates on.

## 3. `SAFE_CHUNK` is triplicated

Byte-identical `r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$"` in three places:
[storage.py:23](../capture/storage.py#L23),
[server.py:251](../capture/server.py#L251),
[__init__.py:51](../capture/analyze/__init__.py#L51).

The comment at `analyze:50` justifies the copy as *"keep local so the CLI does not
import server"* — but `storage.py` is dependency-free, and
[s3copy.py:71](../capture/s3copy.py#L71) already does it right with
`storage.SAFE_CHUNK`. The stated reason does not apply to importing `storage`, and
[bug #1](analyze-bug-review.md) removes the CLI anyway.

**Why it matters:** a chunk name accepted by ingest but rejected by
`session_chunk_names` is a session that records fine and cannot be analysed. Three
copies is three chances to drift.

**Fix:** import from `storage` in both `server.py` and `analyze`.

## 4. Duplicate prompt constants

`prompts/__init__.py` defines `SYSTEM_PROMPT`, `CUT_PROMPT`, `CHAT_PROMPT` at module
level ([:37](../capture/analyze/prompts/__init__.py#L37)). **Nothing reads them** —
`analyze/__init__.py` calls `prompts.load(...)` itself at
[:136](../capture/analyze/__init__.py#L136),
[:840](../capture/analyze/__init__.py#L840) and
[:1197](../capture/analyze/__init__.py#L1197), producing separate equal-valued
strings:

```
analyze.CUT_PROMPT is prompts.CUT_PROMPT   -> False
analyze.CUT_PROMPT == prompts.CUT_PROMPT   -> True
```

Because `prompts` is in `_FORWARD_TO` and those names are in its `vars()`, patching
`analyze.CUT_PROMPT` also rewrites a variable no one reads.

**Fix:** delete the three constants; leave `load()` as the only interface. Makes the
forwarding surface honest.

⚠️ **Do not touch the `.md` files themselves.** `prompt_version` hashes content into
`cut_key`; a whitespace change re-bills every cached cut. Hashes must stay
`5637eecc` / `d6416d2b` / `6007448b`.

## 5. `standout_line` — the dead branch, carefully

[`standout_line`](../capture/analyze/__init__.py#L733) is **live**: called at
[:767](../capture/analyze/__init__.py#L767), rendering the "What stands out" column
at [insights.js:858](../src/replay/insights.js#L858). Do **not** delete the function
— the old plan's "bug 7" is corrected on this point.

Its first branch scans for a `## What stands out` heading. No current prompt emits
that heading, so on live data the fallback always wins.

**But the branch is still exercised.** `test_cohort_batch.py:105-107`,
`test_server.py:2333` and `test_analyze_concurrency.py:74` all feed text containing
`## What stands out`. Deleting the branch changes what those tests observe.

**Recommendation: leave it alone.** It is ~12 lines of correct, tested,
defensive parsing that costs nothing and would resume working if a prompt reintroduced
the heading. Removing it means editing four test files to delete coverage. Negative
value — this is the one item on the old plan's list worth dropping entirely.

## 6. Stale type annotations

Five functions declare `conn: sqlite3.Connection`
([:1387](../capture/analyze/__init__.py#L1387),
[:1446](../capture/analyze/__init__.py#L1446),
[:1484](../capture/analyze/__init__.py#L1484),
[:1531](../capture/analyze/__init__.py#L1531),
[:1775](../capture/analyze/__init__.py#L1775)). What arrives is a
`compat.SqliteStyleConnection` wrapping psycopg. The annotation is true only on the
CLI path, which [bug #1](analyze-bug-review.md) deletes.

**Fix (after the CLI is gone):** annotate honestly — a `Protocol` with
`execute(sql, params) -> Cursor` describes what the functions actually require and
survives the Postgres port. Then drop the `import sqlite3`.

## 7. Docstring / comment drift

- **Module docstring** ([:8](../capture/analyze/__init__.py#L8)) says cohorts persist
  "under `cohorts/<id>.json`" and names "ashwamag-01, ≥5s" as defaults. Cohorts have
  been in Postgres since `0003_cohorts.sql`; the JSON path is migration-only.
- **`cohorts_dir`** ([:205](../capture/analyze/__init__.py#L205)) says "cohort state
  is in SQLite" — it is in Postgres.
- **`store.py:16`** references "a `sqlite3.Connection`'s single transaction state" in
  a module that now uses psycopg. Historical context, but flag it as such.
- **`compat.py:3`** — *"analyze.py is owned by Anant's co-founder"* — is the stated
  reason the compat layer exists. If that ownership boundary has changed, the comment
  is now the main argument for keeping a layer with no engineering justification.

Cheap, and these are the comments most likely to mislead the next reader.

## 8. Minor

- **`resolve_project(project)` called twice** in one statement,
  [server.py:1404](../capture/server.py#L1404) and
  [:1407](../capture/server.py#L1407). Hoist to a local.
- **Test fake signature drift** — `test_cohort_batch.py:95` defines
  `fake_wait(name, *, poll_s=None)` while production is
  `wait_gemini_batch(job_name, *, poll_s=None, max_wait_s=None)`. Benign (the call
  site passes one positional arg) but the fake will not fail loudly if the real
  signature grows a required parameter.
- **`data/index.db`** — stale SQLite file, 141 orphaned sessions. Delete after the
  CLI, and after confirming `store.migrate_sqlite_cohorts` has consumed it.

---

## 9. Lift the linter exclusion

The largest item, and the one that prevents this list from regenerating.

```toml
# pyproject.toml
extend-exclude = ["capture/analyze", "src/analyze", ...]

[[tool.mypy.overrides]]
module = "capture.analyze.*"
ignore_errors = true
```

CI runs `ruff check`, `ruff format --check` and `mypy capture/` on every push
([.github/workflows/test.yml](../.github/workflows/test.yml)) — and skips the largest,
most complex file in the repo. That is why false annotations (#6), dead code (#1) and
unused re-exports (#2) survive here and nowhere else.

The stated rationale is ownership, and this review exists because the file is being
worked on. **Lifting the exclusion should be the closing deliverable** of the
refactor:

1. Do it **last**, so the reformatting diff does not bury the substantive commits.
2. Split into two commits — `ruff format` alone, then real fixes — so review is
   possible.
3. Expect genuine `mypy` errors around the compat connection; #6 is the fix.
4. `ruff` and `mypy` are not installed in `.venv`, so `npm run lint` currently fails
   locally while passing in CI. Add them to the local env first.

---

## Suggested order

Safe to interleave with the other two documents' work.

**Before structural work:** #3 (`SAFE_CHUNK`), #4 (prompt constants), #7 (docstrings),
#8 (minor). All independent, all low-risk.

**During:** #1 (dead code, alongside the CLI deletion), #2 (re-exports — *after* the
forwarding test), #6 (annotations — after the CLI is gone).

**Last:** #9 (linter exclusion).

**Not doing:** #5 (`standout_line`) — leave it.

Verification: `npm test` green, prompt hashes unchanged
(`5637eecc` / `d6416d2b` / `6007448b`).
