# 06 — State, concurrency and failure

Six statuses, two semaphores, and what survives a process dying mid-batch.

---

## The constraint: the stored status goes stale

A cohort's status is written to the database. But a crashed runner leaves rows saying
`running` with nothing running, and a retried session turns an errored cohort back
into a finished one. Any design that treats the stored value as authoritative
eventually shows the user a lie.

## The design: status is a projection, not a record

[`project_cohort_status`](../../capture/analyze/__init__.py#L537) recomputes status
from the session tallies on every read:

> ```python
> A projection, not an inference: everything it needs is in `tally`, and the
> filesystem is not consulted.
> ```

The declared transition table makes the unusual part explicit:

> ```python
> # DONE and ERROR are not frozen. A terminal status is recomputed from the
> # session rows, because the stored value goes stale — a crashed runner leaves
> # a cohort marked running, a retried session turns an errored cohort back into
> # a finished one. The rows are the truth; the stored status is a cache.
> ```
> — [__init__.py:484](../../capture/analyze/__init__.py#L484)

Five rules in priority order. Two are subtle enough to be worth restating:

**Rule 3 — a queued row does not resurrect an errored cohort.**

```python
if running or (queued and current in (CohortState.RUNNING, CohortState.QUEUED)):
```

A leftover queued row revives only a cohort that was *already* running or queued.
Without the guard, one stale row silently flips an errored cohort back to `running`
and the user never retries. This behaviour was found by a characterization test that
*failed* — the expected answer was wrong, not the code.

**Rule 5 — `stale` is neither done nor error.**

[`SESSION_STALE`](../../capture/analyze/__init__.py#L534) is a row that claims `done`
with no write-up behind it — `clear_analysis` on a re-analyze, a deleted result, a
crash between the two:

> ```python
> Named so the cohort can decline to
> count it as done instead of reporting a result it cannot show.
> ```

It blocks rule 1 and rule 4, then rule 5 routes the cohort to `queued`: work
outstanding, nothing running. The alternative — counting it done — has the cohort
advertise an analysis it cannot display.

### Enforcement is tests, not exceptions

[`cohort_transition`](../../capture/analyze/__init__.py#L507) **logs** an undeclared
move rather than raising:

> ```python
> Deliberately permissive: this runs inside a live cohort run with a paid
> Gemini batch in flight, so an undeclared move must not raise and strand it.
> ```

`_undeclared_transitions` is the test hook; `test_cohort_batch.py` clears it in
`setUp` and asserts it is empty in `tearDown`, so every test that drives the pipeline
is also a transition-table conformance check. The server-level version scans the log
— and needed `PYTHONUNBUFFERED`, because `print()` is block-buffered to a file and
the assertion passed vacuously until that was fixed.

---

## Two gates, two resources

> ```python
> # Extraction and model calls contend for different resources, so they get
> # different gates. Reconstruction drives a headless Chromium and is memory
> # bound — one at a time, or a small box starts swapping. The Gemini calls are
> # pure network wait, so a few can overlap and the cohort loop stops spending
> # most of its wall clock idle.
> ```
> — [__init__.py:71](../../capture/analyze/__init__.py#L71)

`ANALYZE_EXTRACT_CONCURRENCY = 1`, `ANALYZE_MODEL_CONCURRENCY = 3`. Collapsing them
into one semaphore breaks whichever resource loses: at 1 the cohort serialises on
network wait; at 3 the box swaps on three Chromiums.
`tests/test_analyze_concurrency.py` pins both.

### Why they are created lazily

> ```python
> # Created lazily: a Semaphore binds to the running loop, and this module is
> # imported before the server's loop exists (and reused by the sync CLI).
> ```

[`_gates()`](../../capture/analyze/__init__.py#L126) rebuilds when the loop changes:

```python
if _gate_loop is not loop or _extract_gate is None or _model_gate is None:
```

A module-level `asyncio.Semaphore(...)` would bind at import to a loop that does not
exist, and every `async with` would raise or — worse, under `asyncio.run` in tests —
silently guard nothing. Tests reset `_gate_loop = None` between cases for this reason
(11 sites).

**The gates are shared across module boundaries.** `_gates()` is called from four
sites inside the package *and* from
[`server.py:1788`](../../capture/server.py#L1788), which reaches into a private
symbol. That fifth caller is the real constraint on any module split: the semaphores
must stay one shared pair, or the chat endpoint and the cohort loop each get their
own limit of 3 and the box sees 6 concurrent model calls.

---

## What survives a crash

### Per-session marks are single-row writes

> ```python
> The pipeline runs N coroutines that each own one session but share the
> cohort dict. Writing through `write_cohort` meant every mark rewrote all N
> rows from the writer's copy, so two marks landing together silently lost
> one — last writer wins, carrying its stale view of everyone else.
> ```
> — [__init__.py:426](../../capture/analyze/__init__.py#L426)

[`mark_cohort_session`](../../capture/analyze/__init__.py#L415) updates one row and
keeps the caller's `entry` dict in step. The same reasoning governs
[`_mark_prepared_still_running`](../../capture/analyze/__init__.py#L2726), which
marks row-at-a-time precisely because `apply_cohort_batch` may already have completed
some sessions before failing — pushing the whole in-memory dict back would overwrite
those completions with a stale view.

### The batch name is the recovery token

A cohort with `batch.name` persisted is resumable. `run_cohort_job` checks it first
([:2588](../../capture/analyze/__init__.py#L2588)) and routes to
`resume_cohort_batch` rather than re-extracting. On the failure paths the name is
deliberately *left* on disk; on the success path it is popped
([:2686](../../capture/analyze/__init__.py#L2686)) so a completed cohort is not
re-resumed.

[`cohorts_needing_batch_resume`](../../capture/analyze/__init__.py#L2833) is the
startup query that finds them — an index lookup across all pools, replacing a
directory scan that parsed every cohort file on the server's boot path.

### Cancellation cancels, it does not resume

`run_cohort_job`'s handler ([:2690](../../capture/analyze/__init__.py#L2690)):

```python
except asyncio.CancelledError:
    live = read_cohort(pool, cohort["id"]) or cohort
    batch_name = (live.get("batch") or {}).get("name")
    if batch_name:
        await cancel_gemini_batch(batch_name)
    mark_cohort_paused(pool, live)
    raise
```

Re-reading from the store rather than trusting the in-memory `cohort` matters: the
in-memory copy may predate marks made by the extraction coroutines.

[`mark_cohort_paused`](../../capture/analyze/__init__.py#L2452) returns in-flight rows
to `queued` and clears their job rows, so a resumed cohort re-queues them rather than
waiting forever on jobs nothing is running.

`CancelledError` is re-raised at every level and never swallowed
([:2635](../../capture/analyze/__init__.py#L2635),
[:2660](../../capture/analyze/__init__.py#L2660),
[:2711](../../capture/analyze/__init__.py#L2711)) — a task that swallows it stays
alive after `.cancel()` and the server hangs on shutdown.

### Extraction failures are per-session

`extract_one` catches per session and records the error against that row, then
`asyncio.gather(..., return_exceptions=True)`
([:2642](../../capture/analyze/__init__.py#L2642)). One unreadable session marks
itself errored; the other 49 proceed to the batch. Letting it propagate would lose
the whole cohort's extraction work to one bad recording.

---

## The leak: `_gates` is reached privately from outside

`server.py:1788` calling `analyze._gates()` is the one place the package's private
surface is used by another module. It works, and the shared-semaphore behaviour it
gets is the *correct* behaviour — but it means any refactor that moves `_gates` has to
update a file outside the package, and nothing in the type system or the tests will
say so. A public accessor would make the dependency visible.
