# Capture — rrweb session replay, PostHog-style

Self-hosted session recording and replay built on [rrweb](https://github.com/rrweb-io/rrweb),
following the same client architecture PostHog uses for its session replay product.

The client SDK is JavaScript (rrweb runs in the browser); the server is Python
(FastAPI). Node is only needed at build time to bundle the client.

## Quick start

```bash
npm install                                  # build-time deps (esbuild, rrweb)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm run dev        # builds bundles + starts the server on :3000
```

- **Demo page** (instrumented): http://localhost:3000/
- **Replay UI**: http://localhost:3000/replay.html

To instrument any page served by this server, add one tag:

```html
<script src="/capture.js"></script>
```

Optional config, before the tag:

```html
<script>
  window.CAPTURE_CONFIG = {
    endpoint: '/ingest',          // where batches are POSTed
    recorderUrl: '/recorder.bundle.js',
    flushIntervalMs: 5000,
    maxBatchSize: 50,
  };
</script>
```

## Architecture (and the PostHog analog)

| This project | What it does | PostHog equivalent |
|---|---|---|
| `public/capture.js` (~0.4 KB) | Loader snippet; lazy-loads the recorder after page idle | `array.js` snippet lazy-loading the recorder bundle |
| `src/recorder.js` → `recorder.bundle.js` | rrweb `record()` + console plugin, session/window ids, batching, gzip, keepalive flush | posthog-js session recording module (vendored rrweb) |
| `server.py` — `POST /ingest?compression=gzip-js` | FastAPI endpoint receiving gzipped JSON event batches | `/s/` endpoint with `compression=gzip-js` (Django, later Rust) |
| `data/sessions/<id>/<ts>-<rand>.json` | Immutable chunk per batch | Kafka → session-recording blob files in S3 |
| `GET /api/sessions`, `GET /api/sessions/:id/events` | Read API for the player | Recording metadata (ClickHouse) + blob fetch |
| `public/replay.html` + `replay.bundle.js` | rrweb-player + console panel | PostHog player with synced DevTools panel |

## Client behavior

- **Session identity** — a session id in `localStorage`, rotated after 30 min idle or
  24 h max; a per-tab window id in `sessionStorage`. On rotation the recorder takes a
  fresh full snapshot so every session is independently replayable.
- **Batching + compression** — events buffer in memory and flush every 5 s or 50
  events, gzipped with `CompressionStream` and POSTed with `fetch`. On `pagehide` /
  tab-hidden a final flush uses `keepalive` so it survives navigation. Failed flushes
  are re-buffered (bounded at 1000 events).
- **Privacy (on by default)** — `maskAllInputs: true` (all input text becomes `*`
  before leaving the browser), `class="capture-block"` redacts an element entirely,
  `class="capture-mask"` masks text content. Verified: raw form values never appear
  in stored chunks.
- **Sampling** — mousemove throttled to 50 ms, scroll to 100 ms, input `last`-value
  only; full snapshot checkout every 5 min so replay can seek without reading the
  whole stream.
- **Console capture** — `@rrweb/rrweb-plugin-console-record` embeds console
  log/info/warn/error into the event stream (type 6 plugin events); the replay UI
  renders them in a panel under the player.

## Commands

```bash
npm run build   # esbuild: recorder.bundle.js, replay.bundle.js(+css), capture.js
npm start       # server only (uvicorn on :3000)
npm run dev     # build + server
```

## Scaling it beyond one box

The wire format is stable, so the upgrade path is swapping storage, not clients:
put `/ingest` behind a queue (Kafka/SQS), write chunks to S3 instead of disk, index
session metadata in a database (ClickHouse/Postgres), and stream chunks to the
player instead of concatenating them in memory. Retention = deleting session
directories older than N days.
