# 02 — The extraction subprocess

Turning a list of rrweb events into images a vision model can reason about. This is
the only non-Python stage in the pipeline, and the only one that runs a browser.

---

## The constraint: rrweb events are not a description of the page

A recording is a DOM mutation log — a full snapshot followed by thousands of deltas.
Nothing in it says what the page *looked like*. There is no screenshot, no rendered
text, no layout. Answering "did the visitor see the price before they scrolled past
it" requires actually building the DOM, applying the mutations in order, resolving
the stylesheets, loading the images, and looking at the result.

A parser cannot do this. Only a browser can.

## The design: drive the same player the replay UI uses

[`startPlayer`](../../src/analyze/extract.mjs#L240) instantiates
`window.captureAnalyze.CapturePlayer` — the *same* class the replay UI runs — inside
a headless Chromium, then holds it still:

> ```js
> // The constructor starts playing; stop it before the rAF loop can advance
> // past a seek target or skip ahead on its own.
> player.pause(0);
> ```
> — [extract.mjs:248](../../src/analyze/extract.mjs#L248)

The module docstring in `analyze/__init__.py` states why this matters:

> ```
> The extractor drives the same CapturePlayer the replay UI uses, so animations
> are frozen and recorded scroll offsets are re-applied exactly as they are on
> screen.
> ```

**The failure mode this prevents:** a bespoke rrweb renderer would drift from the
replay UI. A frame the model analyses would show something no human reviewer could
reproduce by scrubbing the same session — and the write-up would cite it as fact.
Sharing the player makes "what the model saw" and "what you see when you open the
replay" the same artifact by construction.

The trade is that extraction is **memory-bound**: one Chromium per extraction. That
is the entire reason the two concurrency gates exist (document 06).

---

## Active time, not wall-clock time

A 20-minute session with 40 seconds of interaction is 40 seconds of content. Sampling
at 1fps across wall-clock would produce 1,200 near-identical frames of a stationary
page, blow the token budget, and bury the four seconds that mattered.

[`activeSpans`](../../src/analyze/extract.mjs#L158) reduces the event stream to
intervals where a human was doing something. The definition of "something" is
deliberately narrow:

```js
const active =
  e.type === 2 ||                                        // full snapshot
  e.type === 4 ||                                        // meta
  (e.type === 3 && e.data && ACTIVE_SOURCES.has(e.data.source));
```

`ACTIVE_SOURCES` is `{1,2,3,4,5,6,7,12}` — and the comment pins it to its twin:

> ```js
> // Mirrors ACTIVE_SOURCES in src/player.js: the incremental sources that mean a
> // human was doing something, as opposed to the page mutating on its own.
> ```
> — [extract.mjs:9](../../src/analyze/extract.mjs#L9)

A carousel auto-advancing is a mutation, not activity. Including its source would
make every session with a hero slider read as continuously active — the exact case
`ANALYZE_MAX_FRAMES` was later added to cap.

Gaps longer than `ACTIVITY_THRESHOLD_MS` (5s) split a span. That threshold is
*also* shared with the replay UI's skip-ahead, so "active" means one thing across
the product.

### Frame selection is pure

[`pickFrameTimes`](../../src/analyze/extract.mjs#L204) takes events and returns
timestamps. No browser, no I/O:

> ```js
>  * Pure and deterministic so the choice of frames is unit-testable
>  * without a browser.
> ```

Three subtleties worth not "simplifying":

- **The first frame leads the first snapshot** by `FIRST_FRAME_LEAD_MS`
  ([:214](../../src/analyze/extract.mjs#L214)). Seeking before the first full
  snapshot renders a blank page.
- **Every span contributes its tail** ([:226](../../src/analyze/extract.mjs#L226)):
  "the tail of a span is where a scroll or click came to rest." A span shorter than
  one step would otherwise vanish entirely — and short spans are clicks.
- **`fps` is clamped, not trusted** ([:212](../../src/analyze/extract.mjs#L212)) to
  `[MIN_FPS, MAX_FPS]` = `[0.2, 4]`. The comment gives the number: *"50fps yields
  ~10k frames for a 200s recording."* A mistyped env var degrades sampling density
  rather than turning one session into thousands of billed images.

### The cap thins, it does not truncate

When `maxFrames` is exceeded, [:230](../../src/analyze/extract.mjs#L230) strides
across the whole sorted list:

```js
const stride = sorted.length / maxFrames;
for (let i = 0; i < maxFrames; i++) thinned.push(sorted[Math.floor(i * stride)]);
```

Taking `sorted.slice(0, maxFrames)` instead would analyse the first 90 seconds of
every long session and silently discard the checkout. Thinning keeps coverage of the
whole session at lower density — the right degradation for "what happened here."

---

## Frame labelling: text before image

[`_session_model_parts`](../../capture/analyze/__init__.py#L2076) emits a timestamp
part, then the JPEG:

> ```python
> # Label before the image: the model attributes a caption to what follows it
> # far more reliably than to what precedes it.
> ```
> — [__init__.py:2074](../../capture/analyze/__init__.py#L2074)

Swap the order and the model starts attributing timestamps to the previous frame.
Every temporal claim in every write-up shifts by one sample, and nothing fails.

Only frames that actually exist on disk are sent —
[`_usable_frames`](../../capture/analyze/__init__.py#L2045) filters on `is_file()`,
because a screenshot can fail without failing the run.

---

## The warnings contract

The extractor's most important output may be its `warnings` array. A partial
recording still produces a *plausible* digest, and the model will confidently
describe a session it only half saw.

[`readChunks`](../../src/analyze/extract.mjs#L63) documents the trap:

> ```js
>  * A dropped chunk does not leave a visible hole: collectEvents sorts whatever
>  * parsed, so a missing middle chunk reads as continuous, and activeSpans then
>  * treats the gap as legitimate dead air and skips it. The result is a partial
>  * session that looks complete — so the count is returned and surfaced as a
>  * warning rather than swallowed.
> ```

So the digest reports, explicitly ([:440](../../src/analyze/extract.mjs#L440)):
failed assets, unreadable chunks (*"this session is incomplete and gaps may look
like inactivity"*), dropped frames, an unmeasurable page height, and zero frames
captured.

And **all chunks unreadable is an error, not a thin session**
([:344](../../src/analyze/extract.mjs#L344)):

> ```js
> // Every chunk unreadable is a broken recording, not a thin one: analyzing
> // nothing would report "not enough events" and hide the real cause.
> ```

These warnings ride through to `write_analysis_result`
([__init__.py:2130](../../capture/analyze/__init__.py#L2130)) and are stored with the
write-up. Dropping them to tidy the payload would remove the only signal that a
confident-sounding analysis was built on half a recording.

---

## The process boundary

[`run_extractor`](../../capture/analyze/__init__.py#L1885) is deliberately strict:

- **Preflight before spawning** — `shutil.which("node")` and `REPLAYER_BUNDLE.is_file()`
  produce actionable errors (*"run `npm run build`"*) instead of a subprocess failure.
- **`cwd=BASE_DIR`** ([:1903](../../capture/analyze/__init__.py#L1903)) — the
  extractor resolves paths relative to the repo root. This is why `BASE_DIR` climbs
  two levels from `capture/analyze/__init__.py`, and why the health check for
  `bundles_present` is the only thing that catches a wrong anchor after a layout move.
- **Timeout kills the process** ([:1906](../../capture/analyze/__init__.py#L1906)) —
  `asyncio.wait_for` around `communicate()`, then `proc.kill()`. Without the kill, a
  hung Chromium survives the timeout holding the extraction gate, and the cohort
  stalls forever on a semaphore nobody will release.
- **stdout is the digest, stderr is the diagnosis** — only the last line of stderr
  reaches the error message ([:1912](../../capture/analyze/__init__.py#L1912)),
  which is usually the actual exception.

**Positional argument order is load-bearing** and undefended:

```python
str(ANALYZE_FPS),
str(ANALYZE_MAX_FRAMES),
# Positional, and ahead of the variadic chunk names.
*chunk_names,
```

The extractor destructures the same order at
[:471](../../src/analyze/extract.mjs#L471). Insert an argument on one side only and
the first chunk name is parsed as `maxFrames` — `Number("1700000000000-ab-xyz.json")`
is `NaN`, `NaN || 0` is `0`, so the cap silently disables and that chunk vanishes from
the analysis. No error, on either side.
