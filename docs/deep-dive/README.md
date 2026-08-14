# analyze — Architecture Deep Dive

A companion series to [analyze-design-review.md](../analyze-design-review.md), one
aspect per document.

**Read the design review first** (it is a point-in-time map; `__init__.py` is now a
19-line docstring, sampling is SQL, there is no compat layer). These documents are
the terrain underneath it — for each subsystem, the constraint that forced the
design, the failure mode the design prevents, and the invariant you would break by
"cleaning it up."

05 (compat sampling) and 07 (patch forwarding) describe mechanisms that have been
deleted; their banners say so. The invariant in 07 still holds: patch at the
definition site, never `from .owner import patched_name`.

The split in one line: **the design review says what the design is; a deep dive says
what happens when you get it wrong.**

## Why this exists

The load-bearing facts in this pipeline are not visible in its structure. A
`sqlite3.Connection` annotation names a type that never arrives. Attribution is
frozen by a `coalesce` at ingest and can never be recomputed. `analyze` reads
sessions as directories on disk, but the chunks live in S3 and exist locally only
inside one `async with`. A prompt file's trailing whitespace is in a cache key that
bills real money.

Each of those silently dictates a piece of the design, and none is inferable from
reading the code that obeys it. Someone "simplifying" the staging context manager
gets an empty digest and no error. These documents are the record of those
constraints.

## The series

| # | Document | Covers |
| --- | --- | --- |
| 01 | [The ingest → analyze seam](01-ingest-analyze-seam.md) | Where recording ends and analysis begins; why `analyze` speaks `Path` while storage speaks S3; `chunks_on_disk`; what is computed once at ingest and frozen forever |
| 02 | [The extraction subprocess](02-extraction-subprocess.md) | Why a headless Chromium and not a parser; the active-time model; why frames are labelled before the image; the warnings contract |
| 03 | [The two model paths](03-model-paths-batch-and-sync.md) | Interactive vs Batch; why only submission may fall back; `BatchStillRunning`; the streaming JSONL and its `del`s |
| 04 | [Cohorts, cuts and the caching model](04-cohorts-cuts-caching.md) | Why a cohort is frozen; what `cut_key` hashes and why the prompt is in it; the measured-splits doctrine |
| 05 | [Sampling and the compat layer](05-sampling-and-compat.md) | Why five functions still speak SQLite; the RLS trap in the view; where the silent shortfall comes from |
| 06 | [State, concurrency and failure](06-state-concurrency-failure.md) | The six statuses and why terminal ones are recomputed; the two gates; what survives a crash mid-batch |
| 07 | [The patch-forwarding mechanism](07-patch-forwarding.md) | Why `analyze.run_extractor = fake` must reach the real call site; how `_ForwardingModule` works; what it does *not* protect |

## Conventions

- **Cite code as links with line numbers** — `[__init__.py:2618](../../capture/analyze/__init__.py#L2618)`.
  Deep dives make specific claims; a reader must be able to check them.
- **Lead each section with the constraint**, then the design that satisfies it.
  Not the reverse — the design is only interesting once you know what it is up against.
- **Prefer the failure mode to the feature.** "Move this line and every write-up
  loses its campaign metadata" teaches more than "the pool argument is passed down."
- **Record the leaks too.** Where the boundary does not hold, say so. A deep dive
  that only describes the intended design is marketing.
- Line numbers drift. Section names and code quotes are the durable anchors; treat a
  stale line number as a nudge to re-grep, not a broken doc.

## A note on scope

"Ingest" in this repo means [`POST /ingest`](../../capture/server.py#L333) — the
rrweb endpoint the recorder posts session chunks to. It is **not** related to the
separate `lp_ingest` repository, which crawls brand sites and writes WARCs. The two
share a word and nothing else: no imports, no database, no wire format.
