# Capture — self-hosted session replay

Self-hosted session recording and replay built on [rrweb](https://github.com/rrweb-io/rrweb):
watch real user sessions on your sites as DOM-accurate replays, with synced
console and network panels.

The client SDK is JavaScript (rrweb runs in the browser); the server is Python
(FastAPI). Node is only needed at build time to bundle the client.

## Quick start

```bash
npm install                                  # build-time deps (esbuild, rrweb)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm run dev        # builds bundles + starts the server on :3000
npm test           # client + server tests (no test dependencies to install)
```

`npm test` runs the client tests through Node's built-in runner and the server
tests through `unittest`. The server tests boot real uvicorn processes against
a throwaway `CAPTURE_DATA_DIR`, so they exercise what ships — middleware order,
env parsing, status codes — and never touch your recordings.

- **Demo page** (instrumented): http://localhost:3000/
- **Replay UI**: http://localhost:3000/replay.html

To instrument any page served by this server, add one tag:

```html
<script src="/capture.js"></script>
```

## Instrumenting a brand's site

The snippet is self-configuring: it derives the capture server's location
from its own `src` and reads the project token from `data-token`, so an
external site's entire integration is one tag.

1. Create a project (a brand) on the server:

   ```bash
   python server.py add-project "Acme Brand"
   # project 'Acme Brand' created — token: cap_xxxxxxxxxxxxxxxxxxxxxxxx
   ```

2. Send the brand [docs/integration.md](docs/integration.md) — the complete
   brand-facing guide — along with their token and your server address.
   Their side of the integration is the printed tag:

   ```html
   <script src="https://capture.yourdomain.com/capture.js" data-token="cap_xxx"></script>
   ```

   (The guide also covers what the tag costs their page, the privacy
   defaults, Google Tag Manager, CSP, and how they verify it's working.)

That's it — recordings land under `data/projects/<token>/sessions/` and the
replay UI grows a project selector. Operator notes:

- **Creating the first project switches ingest to token-required**: batches
  without a known token are rejected with 401 (so strangers can't fill your
  disk). With no projects configured the server is single-tenant and tokens
  are ignored — the quick-start demo needs no setup.
- Tokens identify, they don't authenticate — they're visible in page
  source, and that's fine: they only grant the ability to *send*
  recordings, never to read them.
- Per-project remote config: `data/projects/<token>/config.json` (falls back
  to the global `data/config.json`), so each brand can have its own sample
  rate, triggers, etc.
- Protect the replay side in production:

  ```bash
  REPLAY_PASSWORD=... .venv/bin/uvicorn server:app --port 3000
  ```

  `/replay.html` and `/api/*` require the password (HTTP Basic, any
  username). With no password set they return 503 rather than serving
  recordings openly; `REPLAY_AUTH_DISABLED=1` opts out of that for local
  development, and `npm run dev` sets it for you. `/ingest`, `/config` and
  the bundles stay open — the snippet runs on other people's pages. Serve
  everything over HTTPS (any reverse proxy in front of uvicorn); browsers
  block plain-HTTP ingest from HTTPS pages.

## Giving brands their own logins (WorkOS)

`REPLAY_PASSWORD` is single-operator: whoever holds it sees every project.
To let each brand's team watch **only their own** recordings, switch the read
side to per-user logins backed by [WorkOS AuthKit](https://workos.com/docs/authkit)
(free tier covers this comfortably):

1. In the WorkOS dashboard: create an application, enable AuthKit, and add
   `https://capture.yourdomain.com/auth/callback` as a redirect URI.
2. Create one WorkOS **Organization** per brand and invite that brand's
   users to it.
3. On the server, set the environment and restart:

   ```bash
   WORKOS_API_KEY=sk_...          # API key — also signs session cookies
   WORKOS_CLIENT_ID=client_...
   CAPTURE_OPERATORS=you@example.com   # comma-separated; these emails see every project
   ```

4. Tie each project to its brand's organization:

   ```bash
   python server.py set-org cap_xxxxxxxx org_yyyyyyyy
   ```

With both `WORKOS_*` vars set, `/replay.html` redirects to a hosted login
instead of prompting for the Basic password; sessions are 7-day HMAC-signed
cookies (rotate `SESSION_SECRET` or the API key to log everyone out). A
logged-in user sees exactly the projects whose `org_id` matches one of their
organizations — the project selector, session lists, chunks, and the Delete
button all scope to that. Operators see everything, including the legacy
no-project pool. Unset the `WORKOS_*` vars to fall back to `REPLAY_PASSWORD`.
The write side (`/ingest`, `/config`, bundles) is unaffected — the snippet
still runs on brand pages with no login.

If a CDN fronts the server, it must forward cookies and query strings to the
origin for `/replay.html`, `/api/*`, `/login`, and `/auth/*` (with caching
disabled on those paths, which is the safe default for the read side anyway).

Optional config, before the tag (inline values win over the derived ones):

```html
<script>
  window.CAPTURE_CONFIG = {
    endpoint: '/ingest',          // where batches are POSTed
    token: null,                  // project token (usually via data-token)
    recorderUrl: '/recorder.bundle.js',
    remoteConfig: true,           // fetch settings from the server first
    configUrl: '/config',
    remoteConfigTtlMs: 60 * 60 * 1000,
    flushIntervalMs: 5000,
    maxBatchSize: 50,
    maxBatchBytes: 900 * 1024,    // flush on payload size, not just count
    idleThresholdMs: 5 * 60 * 1000, // drop background events past this idle time

    // Ingestion controls (all optional):
    sampleRate: 1,                // keep this fraction of sessions (sticky per session)
    triggers: {                   // omit to record unconditionally
      urls: ['/checkout'],        // regexes matched against location.href (SPA-aware)
      onException: true,          // uncaught error / unhandled rejection
    },
    maxBufferBytes: 2 * 1024 * 1024, // pre-trigger rolling buffer bound
    minDurationMs: 0,             // sessions shorter than this are never sent

    // Privacy (inputs are always masked; these add page text and URLs):
    maskTextSelector: null,       // CSS selector whose text becomes asterisks
    blockSelector: null,          // CSS selector not recorded at all
    redactUrlParams: [...],       // query/fragment params stripped from URLs
    redactConsole: true,          // scrub secrets out of captured console args

    // Consent (inline only — a remote value arrives too late to gate storage):
    requireConsent: false,        // touch nothing until grantConsent() is called
    respectGlobalPrivacyControl: true, // honour navigator.globalPrivacyControl

    // Network capture (URLs/method/status/timings by default; false disables):
    network: {
      captureHeaders: false,      // opt-in; sensitive headers always redacted
      captureBody: false,         // opt-in; capped at 64 KB
      captureResources: true,     // img/script/css from the Performance API
      ignoreUrls: [],             // regexes; ingest endpoint always ignored
    },
  };
</script>
```

## Architecture

| Component | What it does |
|---|---|
| `public/capture.js` (~1 KB) | Loader snippet; derives server URL + token from its own tag, lazy-loads the recorder after page idle |
| `data/projects.json` + `?token=` on ingest/config | Per-brand projects: token-gated ingest, isolated storage, per-project config |
| `REPLAY_PASSWORD` (HTTP Basic on `/replay.html` + `/api/*`) | Keeps recordings readable only to you; unset means the read side is refused, not open |
| `WORKOS_API_KEY` + `WORKOS_CLIENT_ID` (per-user logins) | Replaces the Basic password with WorkOS AuthKit; each project's `org_id` scopes which brand team can read it |
| `src/recorder.js` → `recorder.bundle.js` | rrweb `record()` + console plugin, session/window ids, batching, gzip, keepalive flush |
| `server.py` — `POST /ingest?compression=gzip-js` | FastAPI endpoint receiving gzipped JSON event batches |
| `GET /config` ← `data/config.json` | Remote recording settings, fetched before recording starts |
| `POST /order?session_id&token` | Checkout outcome from a Shopify custom pixel (checkout pages run no theme code); stored as `order.json` beside the session's chunks and shown as a Purchased / Reached checkout badge in the session list |
| `data/sessions/<id>/<ts>-<rand>.json` | Immutable chunk per batch |
| `GET /api/sessions`, `/:id/chunks`, `/:id/chunks/:name` | Read API: session list, chunk manifest, single immutable chunk |
| `GET /healthz` | 200 when storage is writable, disk is above the floor and the bundles exist; 503 otherwise. Unauthenticated and boolean-only, so `deploy.sh` and uptime checks can use it |
| `public/replay.html` + `replay.bundle.js` | rrweb-player + synced Console/Network panels |

## Client behavior

- **Remote config** — before recording starts, the client fetches `/config`
  (served from `data/config.json`) and caches it in `localStorage` for 1 h, so
  recording behavior — sample rate, triggers, idle threshold, network options —
  can change fleet-wide by editing one server file, no page redeploys. Merge
  precedence: built-in defaults < remote config < inline `window.CAPTURE_CONFIG`
  (the page always wins). A missing/invalid config file or unreachable endpoint
  degrades gracefully to local settings. Inspect the effective merge with
  `window.capture.config()`.
- **Session identity** — a session id in `localStorage`, rotated after 30 min idle or
  24 h max; a per-tab window id in `sessionStorage`. On rotation the recorder takes a
  fresh full snapshot so every session is independently replayable.
- **Batching + compression** — events buffer in memory and flush every 5 s, at 50
  events, or at ~0.9 MB of payload (a single full snapshot can dwarf dozens of small
  events), gzipped with `CompressionStream` and POSTed with `fetch`. On `pagehide` /
  tab-hidden a final flush uses `keepalive` so it survives navigation. Failed flushes
  are re-buffered (bounded at 1000 events).
- **Idle detection** — after 5 min without real interaction (mouse, scroll, input,
  touch) the recorder goes idle: background noise (animation/timer mutations,
  plugin events) is dropped instead of recorded, marked by a `capture-idle` event.
  Interaction wakes it with a `capture-idle-end` marker plus a fresh full snapshot
  so replay resumes from current reality. Only real interaction extends the
  session's lifetime — a tab left open on an animating page still rotates.
- **Privacy (on by default)** — `maskAllInputs: true` (all input text becomes `*`
  before leaving the browser), `class="capture-block"` redacts an element entirely,
  `class="capture-mask"` masks text content. Verified: raw form values never appear
  in stored chunks.
- **Sampling** — mousemove throttled to 50 ms, scroll to 100 ms, input `last`-value
  only; full snapshot checkout every 5 min so replay can seek without reading the
  whole stream.
- **Lazy replay loading** — the player fetches the chunk manifest, starts
  playback from the first chunk (every session opens with a full snapshot), and
  streams the remaining chunks in via `player.addEvent()` while you watch — the
  server never assembles a whole session in memory, and long sessions start
  playing immediately. Chunks are immutable, so they're served with
  `max-age=86400` and scrubbing re-visits hit the browser cache.
- **Console capture** — `@rrweb/rrweb-plugin-console-record` embeds console
  log/info/warn/error into the event stream (type 6 plugin events); the replay UI
  renders them in a panel under the player.
- **Multi-tab replay** — each browser tab records its own independent rrweb
  stream (own window id, own full snapshot) into the shared session; the ingest
  server encodes the window id into chunk filenames so the manifest groups
  chunks per tab without opening a single file. Multi-tab sessions get a tab
  picker in the player (largest stream plays first); single-tab and legacy
  sessions look exactly as before.
- **Activity-density seekbar** — a histogram of user interactions (clicks,
  scrolls, inputs) painted above the player's progress bar, so the interesting
  moments of a session are visible before you scrub; click-through, per tab,
  lightly smoothed.
- **Playhead-synced inspector** — the Console and Network panels track the
  replay position: rows the playhead hasn't reached yet are dimmed, the
  latest-fired row is highlighted and kept scrolled into view, and clicking any
  row seeks the player to that moment.
- **Network capture** — `src/network-plugin.js` wraps `fetch` + `XMLHttpRequest`
  (method, status, duration, failures) and reads buffered PerformanceObserver
  resource entries (scripts, images, styles — including ones loaded before the
  recorder), emitted as `capture/network@1` plugin events. Privacy-first: URLs
  and timings only by default; headers/bodies are opt-in, sensitive headers
  (authorization, cookie, …) are always redacted, URL credentials are stripped,
  and the ingest endpoint itself is never captured. The replay UI shows a
  Network tab beside Console with per-request rows and error highlighting.
- **Ingestion controls** — `sampleRate` rolls a sticky per-session
  keep/skip decision at session creation, so a session is wholly recorded or never
  leaves the browser. With `triggers` configured, the recorder starts in a
  `buffering` state: events accumulate in a bounded in-memory rolling buffer
  (`maxBufferBytes`, replayability-safe — it restarts from a fresh full snapshot
  rather than trimming past one) and nothing is sent until a trigger fires — a URL
  regex match (SPA navigations included), an uncaught exception, or a manual
  `window.capture.trigger('reason')`. `minDurationMs` adds a second gate — quick
  bounces are never sent, and combined with triggers every configured gate must
  pass before activation. Activation flushes the buffered lead-up, stamps
  a `capture-trigger` custom event into the stream, and persists the decision on the
  session so later page loads stay active. Untriggered sessions are discarded on
  unload. `window.capture.status()` reports `disabled | buffering | active`.
  Try it: **/demo-triggers.html** buffers until you fire one of its three triggers.

## Commands

```bash
npm run build   # esbuild: recorder.bundle.js, replay.bundle.js(+css), capture.js
npm start       # server only (uvicorn on :3000)
npm run dev     # build + server
```

## Scaling it beyond one box

The wire format is stable, so the upgrade path is swapping storage, not clients:
put `/ingest` behind a queue (Kafka/SQS), write chunks to S3 instead of disk, and
index session metadata in a database (ClickHouse/Postgres). The player already
streams chunks lazily, so swapping the chunk endpoint for signed S3 URLs changes
nothing client-side.

Retention is built in: a daily sweep deletes sessions whose newest chunk is older
than `RETENTION_DAYS` (default 30; `RETENTION_DAYS=0` keeps everything forever).

Deletion on demand, for erasure requests that can't wait for retention:

```bash
python server.py list-projects                  # tokens, names, session counts, size
python server.py remove-project <token>         # revoke the token, keep the recordings
python server.py remove-project <token> --purge # revoke and erase them
```

Individual sessions have a **Delete** button in the replay UI (two clicks to
confirm), backed by `DELETE /api/sessions/<id>`. Like the rest of `/api` it sits
behind `REPLAY_PASSWORD`, and `DELETE` is deliberately absent from the CORS
allowlist so no other origin can call it. All of these are irreversible.

Ingest is bounded, because project tokens are public and anyone can post with
one:

| Variable | Default | Effect |
|---|---|---|
| `MAX_PROJECT_BYTES` | 2 GB | Per-project storage cap; over it `/ingest` returns 429 and the recorder drops the batch. `0` disables. |
| `MIN_FREE_DISK_BYTES` | 1 GB | Free-space floor for the box; below it `/ingest` returns 507 and the recorder retries. `0` disables. |
| `MAX_DECOMPRESSED_BYTES` | 32 MB | Ceiling on gzip expansion, so a small bomb can't exhaust memory. |
