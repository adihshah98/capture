# analyze.py refactor — handoff

Working notes for the Tier 2 restructuring of [`analyze.py`](../analyze.py).
Tier 1 (correctness) is done and on the `cleanups` branch; this file exists so
the next session does not have to re-derive the analysis.

## Where things stand

Branch `cleanups`, five commits past `fbbc156`:

| Commit | Fix |
|---|---|
| `5a8002e` | `enrich_cohort_status` output is a view; `persistable_cohort` keeps it off disk |
| `6948877` | `normalize_slice` no longer collapses "both devices" into no filter |
| `8e76eee` | Batch is resumed rather than re-billed; `wait_gemini_batch` is bounded |
| `f74531a` | `page_path` returns `None` for unreadable entry URLs instead of `/` |
| `14ce676` | **Tier 2.1**: cohort state into SQLite (below) |
| `084ade9` | Deleting a session drops it from the cohorts that held it |

327 tests pass (`npm test` runs build + JS + Python; `.venv/bin/python -m
unittest discover -s tests` for Python alone).

**Not ours:** atomic writes (`os.replace` on every `write_text`) are being done
on another branch. Do not touch the write paths in `analyze.py` —
`write_cohort`, `write_analysis_result`, `write_job`, `write_cut` — beyond what
a data-layer move unavoidably rewrites, and expect a merge conflict there.

## What the module is

3,750 lines doing four jobs. Line numbers below predate `14ce676`, which added
~75 lines near the top of the file — anything past the storage block now sits
roughly that much lower.

| Concern | Roughly | What it is |
|---|---|---|
| Storage | 179–361, 2430–2452 | ~~JSON files as a database~~ → `analyze_store.py` |
| Slice algebra | 503–735, 1877–1949 | the `{outcome, device, page, utm_*}` filter language |
| Prompt engineering | 114–170, 738–1083, 1117–1425, 1672–1763 | ~1,100 lines of prompt text + measured splits |
| Orchestration | 2455–3324 | subprocess extraction, Gemini interactive + Batch, cohort job, CLI |

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
[`analyze_store.py`](../analyze_store.py). Storage functions in `analyze.py` keep
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
analyzed. `forget_session` now drops the memberships from all four deletion
paths (`index_forget` covers three; `rebuild_index`'s stale purge is the
fourth). Cohorts shrink; cached cuts are left as the record of what the model
said when it ran. No FK is declared — SQLite ignores them unless every
connection sets `PRAGMA foreign_keys=ON`, and `server.py`'s (the one deleting)
does not.

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

### 2. An explicit state machine

`enrich_cohort_status` (364–457) is 90 lines *inferring* status by scanning the
filesystem, because status is written from ~10 places with no shared discipline.
The comments admit it — "Stay sampled-until-started unless work is somehow
already in flight", "Keep paused until the user hits Start again (even if jobs
look stale)". Those are patches for states the code can reach but cannot name.

Want: a `CohortState` enum and a declared transition table, with one function
owning transitions. `idle → queued → extracting → submitted → applying →
done | error | paused`. Then `enrich_cohort_status` is a projection, not an
inference engine, and "somehow already in flight" becomes an assertion you can
see rather than a silent status flip.

`persistable_cohort` (from `5a8002e`) is the seam: it already separates the
durable record from the response view. A state machine makes that a type
distinction instead of a field list.

Related, still open: `run_cohort_job` mutates a shared in-memory `cohort` dict
across N concurrent `extract_one` coroutines while other code re-reads it, so
there are two sources of truth that diverge under any concurrent write. The
store makes this *fixable* — `update_session_status` writes one row, so those
coroutines no longer need to share a dict at all — but the callers still pass
the dict around and were left alone in `14ce676` to keep that commit to a
storage swap. Converting them is the natural first half of this section.

### 3. Package, with prompts as files

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

**`cut_key` must include the prompt version hash.** It currently hashes only the
filter (725–729), so editing `CUT_PROMPT` leaves every cached cut stale forever
with no invalidation path — the docstring at 338 acknowledges this as
intentional. Fixing it is part of moving prompts to files.

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
4b. **`enrich_cohort_status` trusts the stored status when disk says nothing.**
   The `elif row.get("status") == "done": done += 1` fall-through means a row
   counts as analyzed with no `analysis.json` behind it. `084ade9` removed the
   way that mattered most (deleted sessions), but the fall-through is still
   there and will also fire if a write-up is removed without the session —
   `clear_analysis` does exactly that on a re-analyze. Worth an explicit
   "claims done but has no write-up" state in the §2 machine rather than a
   silent count.
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
