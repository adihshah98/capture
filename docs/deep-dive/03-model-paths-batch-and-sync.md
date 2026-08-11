# 03 — The two model paths

One session write-up can be produced two ways: an interactive `generate_content`
call, or a Gemini Batch job. The rules about *when* each is used, and what may fall
back to what, are the most expensive invariants in the codebase to get wrong —
because getting them wrong bills twice.

---

## The constraint: Batch is 50% cheaper and has no latency SLA

A cohort is 50–100 sessions, each carrying up to 90 JPEG frames. At interactive
prices that is the dominant cost of the product. Batch halves it.

What Batch buys in price it removes in predictability:

> ```python
> # Batch is slow by design (the 50% discount buys a queue, not a latency SLA),
> # so this is a stuck-job backstop rather than a deadline anyone should hit.
> ```
> — [__init__.py:100](../../capture/analyze/__init__.py#L100)

So the split is by *use case*, not by preference: cohorts go through Batch, the
single-session **Analyze** button stays interactive because a human is waiting.
`ANALYZE_BATCH=0` forces sync ([:93](../../capture/analyze/__init__.py#L93)).

---

## The rule: only submission may fall back

This is the single most important invariant in the module.

> ```python
> # Only submission falls back to sync. Once a job exists it is
> # billable, so a later failure must keep its name and be resumed
> # rather than re-running every session at interactive prices.
> ```
> — [__init__.py:2650](../../capture/analyze/__init__.py#L2650)

The boundary is exact. **Before** `batches.create` returns a name, nothing has been
paid for, so a Batch outage falls back to sync and the cohort still completes
([:2662](../../capture/analyze/__init__.py#L2662)). **After** it returns, the job
exists, the money is committed, and every subsequent failure must preserve the job
name so a resume can collect results already being generated.

[`_collect_cohort_batch`](../../capture/analyze/__init__.py#L2699) enforces this by
converting *every* post-submission failure into "leave it to resume":

```python
except BatchStillRunning as exc:      # deadline passed / polling gave up
    _mark_prepared_still_running(...)
    return False
except Exception as exc:              # reading or writing results failed
    # Reading or writing the results failed, not the job.
    _mark_prepared_still_running(...)
    return False
```

Note what is *not* caught: `asyncio.CancelledError` is re-raised
([:2711](../../capture/analyze/__init__.py#L2711)). A user pressing Pause should
cancel the job, not resume it — and `run_cohort_job`'s own handler does exactly that
([:2690](../../capture/analyze/__init__.py#L2690)): cancel the batch, mark paused,
re-raise.

### `BatchStillRunning` is a distinct type for a reason

> ```python
> """Gave up watching a batch that is, as far as we know, still alive.
>
> Distinct from a batch that failed: the job name remains valid and its
> results are still coming, so the caller must keep the name on disk and let
> a resume collect them. Re-running the sessions instead would pay for the
> same work twice.
> """
> ```
> — [__init__.py:2369](../../capture/analyze/__init__.py#L2369)

Collapsing this into `RuntimeError` would make "we stopped watching" indistinguishable
from "the job failed," and the natural handling of the latter — mark sessions errored,
let the user retry — re-bills the entire cohort.

### Polling tolerates blips

[`wait_gemini_batch`](../../capture/analyze/__init__.py#L2379) counts *consecutive*
failures and resets on success ([:2409](../../capture/analyze/__init__.py#L2409)):

> ```python
> # A failed poll says nothing about the job, so treat it as a blip
> # until it stops looking like one.
> ```

Five in a row (`ANALYZE_BATCH_POLL_MAX_ERRORS`) raises `BatchStillRunning` — still
resumable. A single transient 503 escalating would cost a cohort re-run.

The terminal-state check is defensive in a way that looks redundant:

```python
# Compare by name first: _batch_state_name handles every shape the
# SDK returns, while `state in terminal` raises on anything
# unhashable and would defeat that fallback by going first.
if _batch_state_name(job) in terminal_names:
    return job
try:
    if getattr(job, "state", None) in terminal:
        return job
except TypeError:
    pass
```

Reordering these two blocks reintroduces the bug: an unhashable `state` raises
`TypeError` inside the `in` test, and the name-based fallback never runs.

---

## Streaming submission: the OOM that shaped the API

There are two submit functions.
[`submit_gemini_batch`](../../capture/analyze/__init__.py#L2163) takes fully-built
`InlinedRequest` objects — and its own docstring warns you off:

> ```python
> Prefer submit_prepared_gemini_batch for cohorts: building every request up
> front keeps all frame bytes in RAM and OOM'd a 2GB box at n=100.
> ```

[`submit_prepared_gemini_batch`](../../capture/analyze/__init__.py#L2218) takes
`(entry, digest)` pairs and builds each request *inside* the write loop:

```python
req = build_inlined_session_request(sid, digest, digest.get("frames") or [], meta)
payload = req.model_dump(mode="json", exclude_none=True)
del req
...
out.write(json.dumps({"key": key, "request": payload}, ...))
del payload
```

The `del`s are load-bearing — they are the difference between peak RAM of *one
session's frames* and *the whole cohort's*. They look like the kind of line a linter
flags as pointless.

It also **always** uses the Files API, never the inline path:

> ```python
> Always uses the Files API — at cohort scale the inline path
> is both oversized and an OOM hazard while estimating size.
> ```

`_estimate_inlined_bytes` would have to serialise every request to measure it —
reintroducing the exact allocation the streaming path exists to avoid.

**As of today `submit_gemini_batch` and `_estimate_inlined_bytes` have zero
callers.** They are the non-cohort inline branch, kept but unreachable. See the
cleanup review.

### `pool` is what carries campaign metadata

```python
meta = session_metadata(pool / sid) if pool is not None else None
```

> ```python
> `pool` is what lets each request carry its session's campaign metadata; a
> batch submitted without it still works, minus that context.
> ```
> — [__init__.py:2231](../../capture/analyze/__init__.py#L2231)

A batch submitted with `pool=None` succeeds, costs the same, and produces write-ups
that cannot connect behaviour to the campaign that bought the click. `test_cohort_batch.py:92`
asserts `pool is not None` for exactly this reason — it is a silent-degradation guard,
not a type check.

---

## Result matching is by key, never by order

[`parse_batch_results`](../../capture/analyze/__init__.py#L2297) handles both
response shapes — inlined (`metadata.session_id`) and file-output (`row["key"]`) —
and both produce a `{session_id: {...}}` map. `apply_cohort_batch` then joins on
that map:

```python
by_id = {entry["session_id"]: (entry, digest) for entry, digest in prepared}
...
for sid, (entry, digest) in by_id.items():
    hit = results.get(sid)
```

Nothing is read positionally. A batch that returns results out of order, or omits
one, cannot misattribute a write-up to the wrong session — a missing key becomes
`"missing from batch results"` for that session alone
([:2769](../../capture/analyze/__init__.py#L2769)).

This is worth stating because it is the reason a "sort `prepared` for determinism"
change is cosmetic rather than a fix.

---

## The digest reuse rule

Inside `run_cohort_job`'s `extract_one`:

> ```python
> # Reuse a prior reconstruction unless the caller asked to redo.
> # Without this, a retry after a Batch OOM would wipe digests and
> # spend another hour in Playwright before talking to Gemini.
> ```
> — [__init__.py:2620](../../capture/analyze/__init__.py#L2620)

Guarded by `_usable_frames(digest.get("frames"))`, not merely by the digest
existing — a digest whose JPEGs were swept is not reusable, and reusing it would
submit a batch of text-only requests describing images that were never attached.

---

## What each path writes

Both converge on
[`write_analysis_result`](../../capture/analyze/__init__.py#L2116), which writes the
analysis **and** clears the job row. Ordering matters: a result stored with a
lingering `running` job would have `enrich_cohort_status` read the row as in-flight
forever. The sync fallback
([`_cohort_sync_model`](../../capture/analyze/__init__.py#L2787)) and
`apply_cohort_batch` both go through it, which is why the two paths cannot drift in
what a completed session looks like.
