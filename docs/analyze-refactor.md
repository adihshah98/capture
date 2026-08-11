# analyze.py refactor — handoff

Working notes for the Tier 2 restructuring of what was `analyze.py` and is now
the [`analyze/`](../analyze/) package. Tier 1 (correctness) is done; this file
exists so the next session does not have to re-derive the analysis.

## Where things stand

**This work is on `main`, not `cleanups`.** The `cleanups` branch was rebased
onto the Postgres move (`b3c6943`), so every SHA in the table below is the
pre-rebase one and exists only on `cleanups`; `analyze.py` is byte-identical
between the two. Work on `main`.

Branch `cleanups`, five commits past `fbbc156`:

| Commit | Fix |
|---|---|
| `5a8002e` | `enrich_cohort_status` output is a view; `persistable_cohort` keeps it off disk |
| `6948877` | `normalize_slice` no longer collapses "both devices" into no filter |
| `8e76eee` | Batch is resumed rather than re-billed; `wait_gemini_batch` is bounded |
| `f74531a` | `page_path` returns `None` for unreadable entry URLs instead of `/` |
| `14ce676` | **Tier 2.1**: cohort state into SQLite (below) |
| `084ade9` | Deleting a session drops it from the cohorts that held it |
| `60759c8` | **Tier 2.2**: the cohort state machine (below) |
| `e8b1cc4` | Per-session marks write one row, not the whole document |
| `a1b4063` | A row claiming done with no write-up is `stale`, not done |
| `b75e06d` | The cut cache key includes the prompt version |

438 tests pass (`npm test` runs build + JS + Python; `.venv/bin/python -m
unittest discover -s tests` for Python alone). The count was 374 before the
Postgres move added its own suites.

**One known-flaky test**, unrelated to any of this:
`test_durability.TestUnreadableRegistryFailsClosed.test_a_database_outage_is_retryable_not_a_500`
fails intermittently (~1 in 3) on untouched `main` too. It points the server at
a dead Postgres and expects a fast 503; sometimes the request times out
instead. Worth fixing separately — it makes every refactor look guilty.

**Not ours:** atomic writes (`os.replace` on every `write_text`) are being done
on another branch. Do not touch the write paths in `analyze.py` —
`write_cohort`, `write_analysis_result`, `write_job`, `write_cut` — beyond what
a data-layer move unavoidably rewrites, and expect a merge conflict there.

Partly landed as of `main`: `fsutil.atomic_write_{text,json}` exist and
`server.py` uses them, but `analyze/__init__.py` still has three raw
`write_text` calls (`write_job`, the legacy cohort migration,
`write_analysis_result`). Still treat those three as someone else's.

## What the module is

3,750 lines doing four jobs. Line numbers below predate `14ce676`, which added
~75 lines near the top of the file — anything past the storage block now sits
roughly that much lower.

| Concern | Roughly | What it is |
|---|---|---|
| Storage | 179–361, 2430–2452 | ~~JSON files as a database~~ → `analyze/store.py` |
| Slice algebra | 503–735, 1877–1949 | ~~the `{outcome, device, page, utm_*}` filter language~~ → `analyze/slices.py` |
| Prompt engineering | 114–170, 738–1083, 1117–1425, 1672–1763 | ~~prompt text~~ → `analyze/prompts/*.md`; ~~measured splits~~ → `analyze/stats.py` |
| Orchestration | 2455–3324 | subprocess extraction, Gemini interactive + Batch, cohort job, CLI — **still in `__init__.py`** |

Line numbers throughout this file are from the original 3,750-line `analyze.py`
and are now wrong by roughly a thousand lines: the §3 moves took ~940 lines out.
Treat them as a rough ordering, and grep for the symbol name instead. The "Known
bugs" numbers below have the same problem.

Pipeline:

```
rrweb chunks
  → run_extractor()      node src/analyze/extract.mjs (headless Chromium)
  → digest.json + JPEG frames (1fps of active time, capped at 90)
  → call_gemini()        SYSTEM_PROMPT + metadata + digest + frames
  → analysis.json        one markdown write-up per session
  → cohort               a frozen sample of ~50–100 sessions
  → cut                  a filter over that cohort
  → call_gemini_cut()    text-only over the write-ups + measured splits
  → <cohort>.cuts.json   {headline, journey, attention, intent, blockers, ...}
```

## Worth preserving

Load-bearing decisions that look incidental. Do not lose these in a rewrite.

- **Measured splits** (1182–1425). The model invents shares when asked to count
  across write-ups, so the counts are computed in Python and handed over as
  exact. Three gates (min cell 3, min coverage 20%, min lift 15pts) each block a
  different failure. `graded: "lift" | "composition"` matters: in a single-outcome
  cut, lift is 0 by construction and grading on it would suppress every real split.
- **"Examined and not reported"** (1421–1424). Without it the model reads silence
  as uniformity.
- **Citation numbering returned as data** (1086–1114). `{"1": session_id}` is
  built in the loop that numbers the write-ups and stored. Rebuilding it from row
  order later would silently repoint every deep link.
- **`_share_denominator_ok`** (1517–1530). Prompting alone did not stop the model
  attaching cohort-wide totals to a nine-session group. Drops the share, keeps
  the observation.
- **Streaming batch submission** (2795–2850). Building every `InlinedRequest` up
  front OOM'd a 2GB box at n=100. The `del req` / `del payload` are load-bearing.
- **The two concurrency gates** (64–65). Extraction is memory-bound (one
  Chromium); model calls are network-bound (a few overlap). Different resources,
  different limits — `tests/test_analyze_concurrency.py` pins this.

## Tier 2

### 1. Cohort state into SQLite — **done** (`14ce676`)

Cohorts, membership and the cut cache are three tables in `data/index.db`, in
[`analyze/store.py`](../analyze/store.py). Storage functions in `analyze.py` keep
their `pool: Path` signatures and dict shapes, so `server.py` was not touched.

What landed:

- A cohort is one `cohorts` row plus one `cohort_sessions` row per member, so a
  per-session mark is an UPDATE of one row instead of a full-document rewrite
  (**24x** faster over 100 sessions). Each cut is its own `cohort_cuts` row, so
  the `write_cut` lost update is gone.
- Writes run in `BEGIN IMMEDIATE`, **serialized per connection by an RLock**. A
  `sqlite3.Connection` has one transaction state, so `check_same_thread=False`
  lets threads share it but does not make it concurrent — concurrent `BEGIN`s
  raise "cannot start a transaction within a transaction" and cursors interleave
  into `InterfaceError`. First cut of this dropped 21 of 40 concurrent writes;
  `server.py` already guards its own connection with `_index_lock` for the same
  reason. Reads take the lock too, so a read straddling a membership rewrite
  gets a consistent snapshot.
- `cohorts_needing_batch_resume` is an index lookup over all pools at once.
- Migration imports `cohorts/*.json` + `*.cuts.json` on first touch, then
  deletes them (including the directory, so the latch can tell "done" from
  "never ran"). An unreadable file is **kept**, not consumed. A cohort already
  in the table wins, so a stale file cannot roll back newer state. Verified
  against the real `data/`: 6 cohorts, 85 member rows, 3 cuts, order and
  per-session extras (`qa_status`) intact; persisted `progress` dropped on the
  way in.

**Referential integrity** (`084ade9`). `cohort_sessions` is a join table — one
row per (cohort, session) edge, so a session in six cohorts has six rows and
exactly one `analysis.json` on disk that all six point at. Deleting a session
used to strand those rows, and because `enrich_cohort_status` trusts the stored
status when it finds nothing on disk, the cohort kept counting the session as
analyzed. `forget_session` now drops the memberships from both deletion paths
that survived the Postgres move (`b3c6943`): the `DELETE /api/sessions/{id}`
endpoint and `sweep_expired_sessions`, which replaced `rebuild_index`'s stale
purge. Cohorts shrink; cached cuts are left as the record of what the model
said when it ran. No FK is declared — SQLite ignores them unless every
connection sets `PRAGMA foreign_keys=ON`, and `analyze_store`'s (the one
deleting) does not.

Cohort rows carry columns for what is queried or updated alone (`id`, `label`,
`status`, `created_at`, `updated_at`, `batch`); the sampling vocabulary
(`purchased_n`, `slice`, `selection`, …) rides in a `fields` JSON column so it
can change without a migration. Per-session extras ride in the same shape.

**Not moved:** `write_job` / `read_job` (`analysis.status.json`). They are
per-session-directory and used by the single-session analysis path in
`server.py`, which is where the atomic-write branch is working. Folding them in
is what would make `enrich_cohort_status` a pure query with no `stat` at all —
see §2, which now depends on it.

**Toward an external store.** The seam is reusable: nothing outside
`analyze_store.py` knows SQLite exists, and the composite keys (`(pool, id)`,
`(pool, cohort_id, key)`) map onto a partition/sort key or a Postgres table
as-is. What gets rewritten is `_Tx` (`BEGIN IMMEDIATE` is a local single-writer
concept; remotely this becomes a conditional write on a `version` column,
deliberately not added yet), the connection cache (needs a pool with retries),
and `write_cohort`'s DELETE-then-INSERT membership replacement — cheap locally,
expensive over a network, so the per-row `update_session_status` path should
become the only hot-loop write.

### 2. An explicit state machine — **done** (`60759c8`, `e8b1cc4`, `a1b4063`)

`CohortState` declares the six statuses that are actually persisted;
`COHORT_TRANSITIONS` declares the legitimate moves; `project_cohort_status`
replaced the 20-line if/elif chain with four rules (now five) in priority
order. It is a projection, not an inference: everything it needs is in the
tally, and it does not touch the filesystem.

**The wider vocabulary was not adopted.** `extracting` / `submitted` /
`applying` are phases of `run_cohort_job`'s control flow that nothing writes
down; declaring them would have invented states no code can produce or observe.
If a stuck cohort ever needs to be legible ("submitted 40min ago"), that starts
by persisting the phase, not by naming it here.

**Refactored against a characterization baseline, not by reading.** The whole
input space was enumerated and frozen into
`tests/fixtures/cohort_status_baseline.json` *before* any change. This paid for
itself immediately: a test asserting an errored cohort with queued work reports
`running` failed — it reports `error`, because only `RUNNING` and `QUEUED` are
revivable and a leftover queued row must not silently resurrect an errored
cohort. Two other behaviors that look like bugs are deliberate and now
documented: `DONE` and `ERROR` are recomputed rather than frozen, because the
stored status goes stale and the session rows are the truth.

**Enforcement is the tests, not an exception.** `cohort_transition` logs an
undeclared move rather than raising — it runs inside a live run with a paid
batch in flight, and an unforeseen move must not strand it. Every test in
`test_cohort_batch.py` asserts the pipeline made only declared moves, and a
server-level test drives create/start/pause/start over HTTP and scans the
server log. That last one needs `PYTHONUNBUFFERED` in the test env: `print()`
is block-buffered to a file, so the assertion passed vacuously until fixed.

**The shared dict is gone** (`e8b1cc4`). `mark_cohort_session` writes the one
row that changed instead of handing the whole document to `write_cohort`.
Before: two concurrent marks lost one, and under 30 threads 29 were lost.
Converted the four concurrent paths (`extract_one`, `apply_cohort_batch`'s
result loop, `_cohort_sync_model`, and `_collect_cohort_batch`'s
reset-to-running — that last one had been pushing a stale copy back over
completions `apply_cohort_batch` had already written). Full-document writes in
the pipeline: 16 → 8. `mark_cohort_paused` and the pre-scan keep theirs; both
are sequential loops that set cohort status and every row together.

**Known bug 4b is fixed** (`a1b4063`). `SESSION_STALE` names a row that claims
done with no `analysis.json` behind it. It counts as outstanding work, so the
cohort stops reporting a result it cannot show, and since the row is no longer
`done`, `attach_cohort_insights` skips it and it cannot become cut evidence.
The baseline was doubled to 320 cases covering both the with- and
without-write-up halves, since the original only exercised the latter — which
is why the bug survived the first pass.

`persistable_cohort` (from `5a8002e`) remains the seam between the durable
record and the response view. Making that a type distinction rather than a
field list is still open, and is the natural companion to §3.

### 3. Package, with prompts as files — **in progress**

Landed so far (`933bbe5`, `f4c01b4`, `ab6ef7d`, `da3a9ca`). `analyze.py` 3,967 →
3,030 lines, and it is now `analyze/__init__.py`:

```
analyze/
  __init__.py   # everything not yet moved, + patch forwarding
  prompts/
    __init__.py  session.md  cut.md  chat.md
  slices.py     # the filter language — pure, 292 lines
  stats.py      # measured splits — 273 lines
  store.py      # was analyze_store.py, moved unchanged
```

`server.py` and the tests were not touched; all 52 `analyze.*` call sites still
resolve through `__init__.py`. `analyze_store.py` remains as a shim that rebinds
`sys.modules` to `analyze.store` — not a re-export, because three test modules
call `analyze_store.close_all` in cleanup and the connection cache is module
state, so it has to be the same module object.

**Prompt hashes are unchanged** — `session 5637eecc`, `cut d6416d2b`,
`chat 6007448b`. The files were written out of the loaded literals and verified
to round-trip, because `cut_key` bakes that hash into every cached cut key and
one changed byte re-bills every cut. `load()` therefore does not strip:
`session.md` genuinely ends in a trailing space. It was ~495 lines of literal,
not the ~1,100 estimated below — the rest is prompt-adjacent Python that stays
code.

**`cut_key` and `prompt_version` stayed in `__init__.py`.** `cut_key` reads the
`CUT_PROMPT` global and the cache tests reassign `analyze.CUT_PROMPT` to check
invalidation; moving it into `prompts/` would bind the value at import and break
that seam with the tests still green.

**Patch forwarding** (`da3a9ca`) is the constraint the rest of this split has to
respect. The tests swap the expensive edges by assignment
(`analyze.run_extractor = fake`, `analyze.wait_gemini_batch = fake`) and then
drive the real pipeline — which works only while caller and callee share one
namespace. Once `pipeline.py` does `from .models import call_gemini`, the name
is bound at import and `analyze.call_gemini = fake` rebinds only the re-export:
a real network call from a suite that still passes, because those tests assert
on cohort state rather than on which function ran. `__init__.py` therefore
forwards attribute assignment to whichever submodule defines the name, with
owners discovered from `vars()` rather than a hand-written map. **Submodules
must call through the module object** (`models.call_gemini(...)`), not import
the name.

Still to move: `extract.py`, `models.py`, `pipeline.py`, `cli.py` — the
orchestration half, which is also where the three remaining raw `write_text`
calls live (see the atomic-write note above; `fsutil` landed in `server.py` but
not here).

**`deploy.sh` names the package now.** It listed `analyze.py` in both the
tarball and the rollback copy, and never listed `analyze_store.py` at all — so
the store had been shipping stale since `14ce676`. Both now name `analyze`,
which recurses. The tarball must carry `prompts/*.md`: the loader reads them at
import, so a missing `cut.md` is a startup crash rather than a degraded mode.

#### Original plan

~1,100 of 3,500 lines are prompt literals interleaved with the code that uses
them. The git log is almost entirely prompt work, yet a prompt cannot be
versioned, diffed cleanly, or A/B'd.

```
analyze/
  slices.py     # normalize/match/label/facets — pure, ~350 lines
  stats.py      # cross_tab_cells, purchase rate, reporting floor
  prompts/
    session.md  cut.md  chat.md
    __init__.py # loader + version hash
  store.py      # SQLite repository — exists as analyze_store.py, move it here
  extract.py    # subprocess driver for extract.mjs
  models.py     # Gemini: interactive, batch, chat
  pipeline.py   # the cohort state machine
  cli.py
```

**`cut_key` includes the prompt version hash** — **done** (`b75e06d`), ahead of
the package move rather than as part of it. The key is now
`{"filter": …, "prompt": <8 hex>}`, where the hash is `prompt_version(CUT_PROMPT)`
— content-addressed, because a version constant someone must remember to bump
is the mechanism that rots. Reverting a prompt restores the old key, so a
revert reuses its cache rather than paying again.

The model is deliberately *not* in the key: switching `GEMINI_MODEL` is rare
and intentional, and each cut already records the `model` that made it, so it
stays traceable without invalidating every cohort at once on an env-var change.

When prompts become files, `prompt_version` should hash the file rather than
the literal — the function is the seam, and nothing else needs to move.

**Dead data found:** one of the three cached cuts is keyed
`{"a":{"outcome":"purchased"},"b":{"outcome":"all"}}`, a shape no current code
can produce or read — a leftover from a retired A/B compare feature. It was
already unreachable before `b75e06d`. Worth a sweep of cut rows whose key does
not parse to the current shape.

`server.py` imports `analyze` as a module and uses ~60 attributes off it. Keep
`analyze/__init__.py` re-exporting the current surface so the split lands without
touching `server.py`, then narrow it separately.

## Known bugs still open

From the audit, not yet fixed. Numbers are `analyze.py` lines.

1. **`_estimate_inlined_bytes` (2732)** `json.dumps` every request to decide if
   the payload is too big — materialising the base64 frames it is trying to
   avoid. Only reachable from the non-cohort path now.
2. **`_batch_jsonl_path` (2788)** hardcoded `/var/tmp`, millisecond-resolution
   filename (collides), no cleanup if the process dies mid-write. Use
   `tempfile.NamedTemporaryFile` in a configured dir.
3. **Sampling shortfalls are silent (2068–2093, 2187–2197)** `_sample_matching_rows`
   scans at most `COHORT_CANDIDATE_CAP` (5000) newest rows and filters in Python.
   A rare slice asked for 50 can quietly return 3. The purchased/non-purchase
   split has the same hole. Push the predicates into SQL (`json_extract` covers
   outcome/device/page) or report the shortfall.
4. **`pool_facets` (1952)** `fetchall()` over the whole pool with no LIMIT, in the
   event-loop thread.
4b. ~~**`enrich_cohort_status` trusts the stored status when disk says
   nothing.**~~ Fixed in `a1b4063` — see §2. The row now reports
   `SESSION_STALE` and counts as outstanding work.
5. **Silent `except Exception`** in `read_analysis`, `read_job`,
   `load_session_digest`, `_parse_index_json`, `page_path`,
   `_session_attribution`. Disk error, permissions error and absent file are
   indistinguishable, with no logging. (`_read_cohort_file` is off the cohort
   read path since `14ce676` — only the legacy migration still calls it, and
   that path now logs and keeps the file rather than swallowing.)
6. **`_dimension_value` (1230)** `str(value).strip().lower() or None if value else None`
   parses correctly but is one edit from wrong.
7. **`standout_line` (460)** looks for `## What stands out`, which `SYSTEM_PROMPT`
   no longer asks for. Always falls through to the first non-heading line. Dead
   branch from a prompt rewrite.
8. **`ATTRIBUTION_PARAMS` (2504)** deliberate duplicate of
   `server.parse_attribution` with a "change both together" comment. A comment is
   not a mechanism.
9. **`COHORT_URL_CONTAINS = "/pages/ashwamag-01"` (72)** one customer's page as a
   global default in a multi-tenant product.
10. **`prepared.append` from concurrent coroutines (3151, 3159)** atomic under the
    GIL, but the order is nondeterministic. Latent trap if anything ever assumes
    `prepared[i]` matches `sessions[i]`.
11. ~~**`deploy.sh` never shipped `analyze_store.py`.**~~ Fixed in `da3a9ca`. It
    listed files by name in both the tarball and the rollback copy, and the
    store was added to neither when it was introduced in `14ce676` — so the
    deployed box kept running the pre-SQLite module. Now names the `analyze`
    directory, which recurses. Worth auditing the rest of that list against a
    fresh checkout: it is a hand-maintained enumeration of every deployable
    file, which is the same rot mechanism as bug 8.

## Tier 3, for later

Retries with backoff on every model call (there are none — one 429 fails a
session permanently). A cost/token ledger (`usage_metadata` is returned and
discarded). Structured logging (three bare `print()`s, no correlation IDs).
Idempotency on batch submission across restarts. A dead-letter path
distinguishing retryable from permanent. Prompt-injection review: page text
reaches `CUT_PROMPT` and `CHAT_PROMPT` as trusted evidence.

## Environment

- `.venv/bin/python`, Python 3.14, macOS.
- Tests: `npm test` (build + JS + Python) or
  `.venv/bin/python -m unittest discover -s tests`.
- Real index at `data/index.db`: 141 sessions, 123 distinct paths.
  **139 of 141 (98.6%) have no indexed device** — worth investigating
  separately; it makes any device-based cut nearly empty.
