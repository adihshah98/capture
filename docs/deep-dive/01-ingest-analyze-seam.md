# 01 — The ingest → analyze seam

Where recording ends and analysis begins. This is the seam most likely to be broken
by a well-intentioned refactor, because the two sides disagree about what a session
*is* and a context manager reconciles them for the duration of one call.

---

## The two vocabularies

**Ingest speaks rows.** [`POST /ingest`](../../capture/server.py#L333) receives a
batch of rrweb events, writes the chunk to object storage, and folds the derived
facts into one Postgres row via
[`sessionstore.record_chunk`](../../capture/sessionstore.py#L124). Nothing about a
session exists as a directory.

> ```python
> # No directory is created here any more: chunks go to storage and the last
> # two sidecars are columns. Analysis makes one when it stages frames.
> ```
> — [server.py:373](../../capture/server.py#L373)

**Analyze speaks paths.** Every public function in `capture/analyze/` takes a
`session_dir: Path` or a `pool: Path`:
[`analysis_paths`](../../capture/analyze/__init__.py#L145),
[`read_analysis`](../../capture/analyze/__init__.py#L159),
[`session_chunk_names`](../../capture/analyze/__init__.py#L2037),
[`run_cohort_job`](../../capture/analyze/__init__.py#L2581). It reads chunks with
`session_dir.iterdir()` and writes frames to `session_dir/.analysis/`.

These vocabularies are incompatible. The reconciliation is one context manager.

---

## The constraint: the extractor is a subprocess that only knows the filesystem

`run_extractor` spawns `node src/analyze/extract.mjs` with a directory argument
([__init__.py:1891](../../capture/analyze/__init__.py#L1891)). The extractor calls
`readFileSync(join(sessionDir, name))`
([extract.mjs:67](../../src/analyze/extract.mjs#L67)). It cannot be handed bytes, an
S3 client, or a database cursor — it is a separate process in a different language
whose entire input contract is "a directory with chunk files in it."

So for the duration of an analysis, the chunks *must* exist on local disk, even
though their durable home is S3.

## The design: `chunks_on_disk`

[`chunks_on_disk`](../../capture/server.py#L1055) is an async context manager that
materialises exactly what is missing and removes exactly what it added:

> ```python
> staged = await asyncio.to_thread(_stage_chunks, project, list(session_ids))
> try:
>     yield
> finally:
>     await asyncio.to_thread(_unstage_chunks, staged)
> ```
> — [server.py:1065](../../capture/server.py#L1065)

Two details in [`_stage_chunks`](../../capture/server.py#L1031) carry the whole
design:

```python
path = directory / chunk.name
if path.exists():
    continue          # ← never adopt a file we did not create
...
staged.append(path)   # ← only these are removed later
```

`_unstage_chunks` deletes **only the paths this call staged**. A session whose
chunks were already local — a pre-S3 recording, or a concurrent analysis — is left
alone. Without that check, two overlapping cohort runs over the same session would
have the first to finish delete the second's input mid-extraction.

The whole thing is a no-op when storage is local
([server.py:1062](../../capture/server.py#L1062)), which is why the test suite never
exercises it and why a bug here would reach production unseen.

### Where it is applied

Three call sites, and they are the complete set:

| Site | Path |
| --- | --- |
| [server.py:1076](../../capture/server.py#L1076) | single-session `Analyze` |
| [server.py:1155](../../capture/server.py#L1155) | cohort run |
| [server.py:1176](../../capture/server.py#L1176) | cohort batch resume |

**Failure mode:** call `analyze.run_cohort_job` without wrapping it and
`session_chunk_names` returns `[]`, which
[`extract_one`](../../capture/analyze/__init__.py#L2628) turns into
`RuntimeError("no chunks to analyze")` — one error per session, cohort marked
errored, no indication that the cause was a missing context manager. The pipeline
does not distinguish "this session was never recorded" from "its chunks are in S3
and nobody fetched them."

### The leak: resume does not need chunks, but stages them anyway

[`_resume_cohort_batch`](../../capture/server.py#L1169) wraps
`analyze.resume_cohort_batch` in `chunks_on_disk`. But the resume path never
re-extracts — it reads a digest that a prior run already wrote to disk:

> ```python
> digest = load_session_digest(session_dir) or {"frames": [], "warnings": []}
> ```
> — [__init__.py:2561](../../capture/analyze/__init__.py#L2561)

So on resume the staging is pure cost: a full S3 fetch of every chunk in the cohort,
on process startup, to satisfy a code path that reads none of them. For a 100-session
cohort that is 100 sessions × N chunks of needless download before the server is
usefully up.

It is *harmless* — which is why it has survived — but it is the kind of thing that
looks load-bearing to the next reader. It is not: `resume_cohort_batch` reads
`.analysis/digest.json`, which `_unstage_chunks` never touches.

---

## What ingest freezes forever

The seam is not only about files. Three facts are computed **once, at ingest**, and
analyze can only read them back.

### Attribution — frozen by `coalesce`, first write wins

[`record_chunk`](../../capture/sessionstore.py#L149) upserts with:

```sql
url         = coalesce(sessions.url, excluded.url),
device      = coalesce(sessions.device, excluded.device),
attribution = coalesce(sessions.attribution, excluded.attribution)
```

`coalesce(existing, new)` keeps the **existing** value. So the first chunk that
carries a URL sets the session's attribution permanently; every later chunk's is
discarded. This is correct — the entry URL is the one that carries the campaign, and
a later navigation must not overwrite it — but it has a consequence that is easy to
miss:

**Re-parsing attribution later cannot fix a session that was indexed wrong.** The
raw query string is not retained anywhere except the `url` column (capped at 2048
chars, [server.py:381](../../capture/server.py#L381)). Whatever
[`parse_attribution`](../../capture/server.py#L646) extracted at ingest time is what
that session will report for the rest of its life.

This is what makes the divergence between `server.parse_attribution` and
`analyze._session_attribution` more than cosmetic. A session that arrived with
`?utm_term=running+shoes` has that term in the *prompt* metadata (analyze re-parses
`url` at read time, [__init__.py:1995](../../capture/analyze/__init__.py#L1995)) but
**not** in the indexed `attribution` column that slicing and cut-matching read. The
two views of one session genuinely disagree, and no backfill of the parser fixes
already-ingested rows without re-reading `url` and rewriting the column.

### Device — one type-4 event, then never again

[`read_device`](../../capture/server.py#L357) scans for the first rrweb meta event
with a positive width. That becomes `sessions.device`, `coalesce`d like the rest.
[`device_class`](../../capture/analyze/slices.py#L34) later thresholds it at
`MOBILE_MAX_WIDTH = 768`.

A session whose first chunk carried no type-4 event has `device = NULL` forever.
`device_class` returns `None`, and `row_matches_slice` **refuses `None` against any
device constraint** ([slices.py:203](../../capture/analyze/slices.py#L203)). That is
why `normalize_slice` will not collapse "mobile or desktop" into no filter:

> ```python
> # `total` says whether every session has a value here. Selecting every
> # value is the same as no constraint only when it is: outcome_class()
> # always returns one of its three, but device_class() returns None for an
> # unindexed viewport…
> ```
> — [slices.py:150](../../capture/analyze/slices.py#L150)

Delete that `total=False` and every unknown-device session silently joins every
device-filtered cohort.

### Clicks — accumulated in SQL, never recounted

[`count_clicks`](../../capture/server.py#L369) counts **this batch only**, and the
upsert does `clicks = sessions.clicks + excluded.clicks`. The comment states the
reason plainly:

> ```python
> """This batch's clicks only. The running totals accumulate in SQL, where an
> increment cannot be lost between a read and a write."""
> ```

This is the counter that `_cohort_order_sql` sorts on as a tiebreaker
([__init__.py:1439](../../capture/analyze/__init__.py#L1439)) — via a JSON round-trip
through the compat view, which document 05 covers.

---

## The direction of dependency

```
recorder (browser)
    │  POST /ingest
    ▼
server.py ──▶ storage.put_chunk ──▶ S3
    │      └▶ sessionstore.record_chunk ──▶ Postgres (sessions row)
    │
    │  later, on demand
    ▼
chunks_on_disk ──▶ stages S3 → local disk
    │
    ▼
analyze.run_cohort_job ──▶ node extract.mjs ──▶ digest + frames
    │
    ▼
Gemini ──▶ write-up ──▶ Postgres (analyses)
```

`analyze` imports **nothing** from `server`. The dependency runs one way —
[`server.py:41`](../../capture/server.py#L41) does `from . import analyze` — and the
comment at [__init__.py:1931](../../capture/analyze/__init__.py#L1931) records why
the attribution parser was duplicated rather than shared:

> ```python
> # Kept in step with server.parse_attribution, which does the same job for the
> # index. analyze.py cannot import server.py (server imports analyze), so the
> # two are deliberate twins — change both together.
> ```

The stated constraint is real; the chosen remedy is not a mechanism. Both functions
are pure and depend on nothing but `urllib` and `re`, so the correct home is
`slices.py`, which both sides already import. See the bug review.

---

## The invariant you would break

1. **Never make `analyze` fetch from S3 directly.** It would need a `project` to
   resolve a `storage_prefix`, which means importing `projects`, which means the
   package stops being drivable from a plain directory — and the extractor
   subprocess still could not use it.
2. **Never widen `_unstage_chunks` to "delete all chunks in the directory."** It is
   the difference between a cleanup and a data-loss bug for local-storage
   deployments.
3. **Never move attribution parsing later.** It looks like it could be derived at
   read time from `url`, and for the *prompt* it already is — but the indexed column
   is what slicing reads, and it is written once.
4. **`SAFE_CHUNK` must stay in step across all three copies**
   ([storage.py:23](../../capture/storage.py#L23),
   [server.py:251](../../capture/server.py#L251),
   [__init__.py:51](../../capture/analyze/__init__.py#L51)). A chunk name accepted by
   ingest but rejected by `session_chunk_names` is a session that records fine and
   cannot be analysed.
