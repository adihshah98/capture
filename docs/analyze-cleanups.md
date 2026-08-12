# analyze — cleanups

*Reviewed 2026-08-11, re-verified against `main` @ `70f5748`. Part 3 of 3:
[design](analyze-design-review.md) · [bugs](analyze-bug-review.md) · **cleanups**.*

> **Worked through 2026-08-11.** Eight of the ten items are done; see the
> [status table](#status--2026-08-11) at the foot of this document. Each section
> below has been rewritten to describe what was actually changed rather than what was
> proposed, including the two items that turned out to be already fixed (#2, #10) and
> the one that was already correct (#7's `store.py` entry).
>
> What is left is deliberate: #5 (not doing), the `compat.py` ownership comment and
> the `data/index.db` deletion (both decisions for a human), and #9, which is scoped
> and measured but wants a clean tree and two commits of its own.

Mechanical tidying: dead code, duplication, stale annotations, tooling. Nothing here
changes behaviour — anything that does is in the [bug review](analyze-bug-review.md).

Each item is independently safe. They are good "warm-up" commits before the
structural work, and good filler between the larger steps.

---

## 1. Dead code — **done**

Verified by grep across `capture/`, `tests/` and `src/`.

| Symbol | Refs | Action |
| --- | --- | --- |
| `submit_gemini_batch` | **definition only** | deleted |
| `_estimate_inlined_bytes` | only from the above | deleted |
| `ANALYZE_BATCH_INLINE_MAX_BYTES` | only from the above | deleted |
| `status_path` | **definition only** | deleted |
| `ANALYSIS_STATUS_FILE` | only from `status_path` | deleted |

`submit_gemini_batch` was the non-cohort inline-batch branch; removing it removes the
inline path entirely, which is intended — cohorts already always use the Files API
([deep-dive 03](deep-dive/03-model-paths-batch-and-sync.md), updated to match). If a
non-cohort batch entry point is wanted later it is a fresh design, not a revival.

`status_path` was a leftover from when job status was a file; job state moved to
Postgres in `0004_analyses.sql`. It took `ANALYSIS_STATUS_FILE`, its only remaining
reader, with it.

**The CLI went in the same pass** (listed under [bug #1](analyze-bug-review.md)):
`main`, `_cli_run_cohort`, `_cli_pool`, `_cli_data_root`, the argparse block, the
`__main__` guard, and the then-unused `argparse` import. `sqlite3` stays for now —
its only remaining uses are the stale annotations in #6, which is the item that
removes it. 208 lines total; `npm run test:server` green at 525 tests.

## 2. Re-exports with no consumers — **done**

**Done during the extraction commits.** All ten zero-consumer names —
`META_SOURCES`, `slice_is_empty`, `is_meta_attribution`, `cross_tab_cells`, the five
`CROSS_TAB_*` constants and `MOBILE_MAX_WIDTH` — are gone from the re-export blocks
and have no references anywhere in `capture/` or `tests/`.

The comment now at [:751](../capture/analyze/__init__.py#L751) states the rule the
narrowing established: the blocks are imports *for use in this module*, not a public
surface. Outside callers go through `capture/analysis_port.py`; anything wanting the
slice vocabulary imports `capture.analyze.slices` directly.

Re-measured 2026-08-11: every name still in a re-export block is live. Six
(`DEVICE_VALUES`, `OUTCOME_VALUES`, `device_class`, `CUT_SECTIONS`,
`facets_from_rows`, `pool_facets`) are *unused inside* `__init__.py` and look
droppable — they are not. `analysis_port.py` and the suite reach all six off the
package, which is precisely what the re-export is for. Deleting them breaks
production; the import-line-only grep is the trap here.

This was the precondition for #10, which has also landed (`d2401ee`).

## 3. `SAFE_CHUNK` — **done**

The canonical definition is [paths.py:30](../capture/paths.py#L30); `server.py` and
`storage.py` imported it as of `37d10ff`, and `analyze/__init__.py` now does too. The
local regex and the comment that justified it ("keep local so the CLI does not import
server" — a CLI that no longer exists) are gone. Verified the two patterns were
byte-identical before collapsing them, so no behaviour changed.

**Why it mattered:** a chunk name accepted by ingest but rejected by
`session_chunk_names` is a session that records fine and cannot be analysed.

## 4. Duplicate prompt constants — **done**

`prompts/__init__.py` defined `SYSTEM_PROMPT`, `CUT_PROMPT`, `CHAT_PROMPT` at module
level with **no readers** — `analyze/__init__.py` calls `prompts.load(...)` itself,
producing separate equal-valued strings. The three constants are deleted; `load()` is
the only interface.

Note the loader docstring still refers to `analyze.CUT_PROMPT`. That is the
*package-level* name, which is live and is what the suite patches to test cache
invalidation — a different object from the deleted `prompts.CUT_PROMPT`.

⚠️ **Do not touch the `.md` files themselves.** `prompt_version` hashes content into
`cut_key`; a whitespace change re-bills every cached cut. Hashes verified unchanged
after this edit: `5637eecc` / `d6416d2b` / `6007448b`.

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

## 6. Stale type annotations — **done**

Five functions declared `conn: sqlite3.Connection` (one in `__init__.py`, four in
`sampling.py`). What arrives is `compat.SqliteStyleConnection` wrapping psycopg; the
annotation was true only on the now-deleted CLI path.

They now take [`spec.Connection`](../capture/analyze/spec.py#L23), a `Protocol`
declaring the single method these functions call — `execute` is the entire surface
they use, confirmed by grep. It sits in `spec.py` because that is already the
dependency-free leaf the sampler, the label writer and the port all import.

With the last annotation gone, `import sqlite3` was dead in `__init__.py` and is
removed. `sampling.py`'s module docstring, which explained the false annotation, was
updated to describe the Protocol instead.

## 7. Docstring / comment drift — **done, except one judgement call**

- **Module docstring** — rewritten. Dropped the "ashwamag-01, ≥5s" defaults (they
  live in `spec.py` and are no longer hardcoded to one customer) and the claim that
  cohorts persist as `cohorts/<id>.json`; it now states that state is in Postgres and
  the JSON layout is migration-only.
- **`cohorts_dir`** — "cohort state is in SQLite" → "in Postgres".
- **`store.py:16`** — **left as-is.** Re-read in full: the file opens *"Cohort state,
  in Postgres"* and the `sqlite3.Connection` mention sits inside an explicit list of
  "three things the move simplifies". It is already flagged as historical.
- **`compat.py:3`** — **left for you.** *"analyze.py is owned by Anant's co-founder"*
  is a statement about a team boundary, not about code. If that boundary has changed,
  it is the main argument for keeping a layer with no engineering justification — but
  whether it has changed is not something the tree can tell me.

## 8. Minor — **done, except the data file**

- **`resolve_project(project)` called twice** — **already fixed.** All 19 call sites
  in `server.py` now bind `found = resolve_project(project)` first; no double call
  remains.
- **Test fake signature drift** — fixed. `fake_wait` in `test_cohort_batch.py` now
  mirrors `wait_gemini_batch(job_name, *, poll_s=None, max_wait_s=None)`, with a
  comment saying why the signatures must track.
- **`data/index.db`** — **not done, needs your call.** Still present (110KB, 141
  orphaned sessions). Deleting a data file is irreversible and the doc's own
  precondition is confirming `store.migrate_sqlite_cohorts` has consumed it, which I
  have not verified.

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
4. Install the dev tooling locally — `pip install -r requirements-dev.txt`. The file
   now exists (`fb82347`); without it `npm run lint` fails locally while passing in CI.

`capture/analyze` is now the **only** entry left in `extend-exclude` that is ours
(`src/analyze` is the Node extractor, `.venv`/`node_modules` are vendor). The rest of
`capture/` was formatted and gated in `067fe83` / `d177c4c`, so this is the last file
outside the net.

### Measured 2026-08-11 — not yet lifted

Tooling installed and run against the package with the exclusion still in place. Note
that a global `user = true` in `~/.config/pip/pip.conf` breaks `pip install` into the
venv; `PIP_USER=0` in front of it is the workaround.

`ruff format --check`: **8 of 9 files** would be reformatted. `mypy capture/` is
already clean. `ruff check`: **99 errors**, of which 80 are autofixable —

| Rule | Count | Character |
| --- | --- | --- |
| `UP007` / `UP006` / `UP035` | 78 | `Optional[x]` → `x \| None`. Mechanical. |
| `F401` unused-import | 10 | **See the hazard below.** |
| `I001` unsorted-imports | 5 | Mechanical, but see the `E402` note. |
| `B904` raise-without-from | 2 | Real: loses the causal traceback. Judgement. |
| `C408`, `UP041`, `RUF005`, `RUF013` | 4 | Mechanical. |

> ⚠️ **Do not run `ruff check --fix` on this package.** Eight of the ten `F401`s are
> the re-export blocks from #2, and six of those names — `DEVICE_VALUES`,
> `OUTCOME_VALUES`, `device_class`, `CUT_SECTIONS`, `facets_from_rows`, `pool_facets`
> — are *unused inside the module* but reached off the package by
> `analysis_port.py` and the suite. Autofixing deletes them and breaks production.
> Ruff's own alternative is the right one: declare an `__all__`, which also gives #2
> the explicit public surface it was arguing for.
>
> The other two `F401`s (`typing.List`, `typing.Tuple`) were genuinely dead and are
> already removed.

The re-export blocks also carry `# noqa: E402` because they sit mid-file; the
`I001` fixes and any `__all__` work have to preserve that placement, which is
deliberate — see the comment at [:750](../capture/analyze/__init__.py#L750).

## 10. Delete `_ForwardingModule` — **done in `d2401ee`**

Added on re-verification; see [design §0.3](analyze-design-review.md) for the full
argument.

`_ForwardingModule` swapped the live module's `__class__` so assignment to the package
was pushed into whichever submodule owned the name. Its costs were all concrete:
invisible to type checkers, IDEs and `__all__` tooling; fired on every module-global
write; `OUTCOME_LABELS` was in `vars()` of both `slices` and `stats` and the loop had
no `break`, so one assignment wrote three bindings; and the class swap was the last
statement of a 3,000-line module, so any import-time exception disabled it silently.

It was also propping up a testing style rather than being required by one: the suite
used **54 raw assignments** against **one** `unittest.mock.patch`.

**Replaced by a checked import rule**, as proposed: `_ForwardingModule` and
`_FORWARD_TO` are gone, and [tests/test_patch_contract.py](../tests/test_patch_contract.py)
enforces the *rule* by walking the AST — no module in the package may bind a patchable
name via `from . import X`.

One coupling worth knowing: that file's `PATCHABLE` set has a companion test asserting
every listed name still exists on the package. Deleting a patchable function (as #1
did with `submit_gemini_batch`) fails CI until the name is removed from the set too.
That is the guard working — it catches a list going stale — but it is a second place
to edit.

---

## Status — 2026-08-11

| # | Item | State |
| --- | --- | --- |
| 1 | Dead code (+ the CLI) | **done** |
| 2 | Narrow the re-exports | **done** (extraction commits) |
| 3 | `SAFE_CHUNK` single definition | **done** |
| 4 | Duplicate prompt constants | **done** |
| 5 | `standout_line` | **not doing** — deliberate, see above |
| 6 | Stale `sqlite3` annotations | **done** — `spec.Connection` Protocol |
| 7 | Docstring / comment drift | **done**, except `compat.py:3` (your call) |
| 8 | Minor | **done**, except deleting `data/index.db` (your call) |
| 9 | Lift the linter exclusion | **measured, not lifted** |
| 10 | Delete `_ForwardingModule` | **done** (`d2401ee`) |

Two items are left deliberately, because both are decisions rather than edits: the
`compat.py` ownership comment (#7) and the `data/index.db` deletion (#8). #9 is
scoped and measured above but is the closing deliverable — it wants its own two
commits, on a clean tree.

Verification for everything marked done: `npm run test:server` green at **525 tests**,
`npm run test:client` green at **172**, prompt hashes unchanged
(`5637eecc` / `d6416d2b` / `6007448b`), `mypy capture/` clean.

Note: `ruff check .` reports 19 pre-existing errors and 4 files needing reformat, all
under `tests/`. Confirmed identical on a stashed clean tree — not introduced by this
work, and out of scope for the analyze cleanup.
