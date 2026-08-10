# analyze.py refactor — handoff

Working notes for the Tier 2 restructuring of [`analyze.py`](../analyze.py).
Tier 1 (correctness) is done and on the `cleanups` branch; this file exists so
the next session does not have to re-derive the analysis.

## Where things stand

Branch `cleanups`, four commits past `fbbc156`:

| Commit | Fix |
|---|---|
| `5a8002e` | `enrich_cohort_status` output is a view; `persistable_cohort` keeps it off disk |
| `6948877` | `normalize_slice` no longer collapses "both devices" into no filter |
| `8e76eee` | Batch is resumed rather than re-billed; `wait_gemini_batch` is bounded |
| `f74531a` | `page_path` returns `None` for unreadable entry URLs instead of `/` |

304 tests pass (`npm test` runs build + JS + Python; `.venv/bin/python -m
unittest discover -s tests` for Python alone).

**Not ours:** atomic writes (`os.replace` on every `write_text`) are being done
on another branch. Do not touch the write paths in `analyze.py` —
`write_cohort`, `write_analysis_result`, `write_job`, `write_cut` — beyond what
a data-layer move unavoidably rewrites, and expect a merge conflict there.

## What the module is

3,500 lines doing four jobs:

| Concern | Roughly | What it is |
|---|---|---|
| Storage | 179–361, 2430–2452 | JSON files as a database |
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

### 1. Cohort state into SQLite

The problem behind most remaining bugs: cohort state is a mutable JSON document
rewritten in full by concurrent writers with no locking, atomicity, or
transactions.

`write_cohort` is called **inside loops** — once per session in
`apply_cohort_batch` (3263) and per extraction start in `extract_one` (3143).
For 100 sessions that is hundreds of full-document rewrites. `_read_cohort_file`
swallows a parse error and returns `None`, so a truncated write makes the cohort
**vanish from the UI** along with its in-flight batch job name.

`write_cut` (337–345) is a textbook lost update: `read_cuts()` → mutate →
`write_text()`, and `_cut_tasks` is keyed per-cut, so two cuts of one cohort
finishing together drop one.

SQLite is already in-process (`data/index.db`, WAL on). Cohort membership,
per-session status, batch job records and the cut cache are small, relational and
read-modify-write heavy.

- Move those four into tables; leave write-up markdown and JPEG frames on disk,
  referenced by path.
- `enrich_cohort_status` becomes a query. Today it is O(sessions) `stat` + `read`
  **on every poll** — see `server.py:1352` `get_cohort`.
- `cohorts_needing_batch_resume` becomes an index lookup, not a directory scan.
- Needs a migration reading existing `cohorts/*.json` + `*.cuts.json`. Keep
  `migrate_legacy_cohort`'s precedent: migrate once, then delete.

If files must stay: temp file + `os.replace`, `fcntl.flock` around
read-modify-write, and a monotonic `version` for optimistic concurrency. Note the
atomic-write branch above.

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
across N concurrent `extract_one` coroutines while other code re-reads the file,
so there are two sources of truth that diverge under any concurrent write.

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
  store.py      # SQLite repository
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
5. **Silent `except Exception`** in `_read_cohort_file`, `read_analysis`,
   `read_job`, `load_session_digest`, `_parse_index_json`, `page_path`,
   `_session_attribution`. Disk error, permissions error and absent file are
   indistinguishable, with no logging.
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
