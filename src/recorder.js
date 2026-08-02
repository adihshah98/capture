/**
 * recorder.bundle.js — the lazy-loaded rrweb recorder.
 *
 * Mirrors PostHog's session-recording client architecture:
 * - records DOM via rrweb with masking on by default (privacy-first)
 * - captures console logs via the official rrweb console plugin
 * - manages a session id (30 min idle / 24 h max rotation) + per-tab window id
 * - batches events, gzips them with CompressionStream, POSTs to /ingest
 *   with ?compression=gzip-js (the same convention PostHog uses)
 * - flushes on an interval, on batch-size threshold, and on pagehide
 *   using fetch keepalive so unload flushes survive navigation
 * - ingestion controls: per-session sticky sampling (sampleRate) and
 *   trigger-based recording (triggers: {urls, onException} + capture.trigger())
 *   with a pre-trigger rolling buffer, so a fired trigger keeps the lead-up
 *   and untriggered sessions never leave the browser
 */
import { record } from 'rrweb';
import { getRecordConsolePlugin } from '@rrweb/rrweb-plugin-console-record';

const config = Object.assign(
  {
    endpoint: '/ingest',
    flushIntervalMs: 5000,
    maxBatchSize: 50,
    checkoutEveryNms: 5 * 60 * 1000, // periodic full snapshots so replay can seek
    sessionIdleTimeoutMs: 30 * 60 * 1000,
    sessionMaxLengthMs: 24 * 60 * 60 * 1000,

    // Ingestion controls. sampleRate keeps a session with probability 0..1;
    // the decision is made once per session and sticks for its lifetime.
    sampleRate: 1,
    // triggers: null records unconditionally. Set to an object to buffer
    // events client-side and only send once a trigger fires:
    //   { urls: ['/checkout'], onException: true }
    // plus window.capture.trigger('reason') for manual activation.
    triggers: null,
    // Bound on the pre-trigger rolling buffer (approx. serialized bytes).
    maxBufferBytes: 2 * 1024 * 1024,
  },
  window.CAPTURE_CONFIG || {}
);

// ---------------------------------------------------------------------------
// Session / window identity
// ---------------------------------------------------------------------------

const SESSION_STORAGE_KEY = 'capture_session';
const WINDOW_STORAGE_KEY = 'capture_window_id';

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function readSession() {
  try {
    return JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY)) || null;
  } catch {
    return null;
  }
}

function writeSession(session) {
  localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
}

/**
 * Returns the current session, rotating it when the visitor has been
 * idle past the timeout or the session exceeds its max length.
 *
 * The sampling decision is rolled once at session creation and persisted,
 * so a session is either wholly recorded or wholly skipped — never a
 * fragment (PostHog's per-session sticky sampling).
 */
function getSession() {
  const now = Date.now();
  let session = readSession();
  const expired =
    !session ||
    now - session.lastActivity > config.sessionIdleTimeoutMs ||
    now - session.started > config.sessionMaxLengthMs;
  if (expired) {
    session = {
      id: uuid(),
      started: now,
      lastActivity: now,
      sampled: Math.random() < config.sampleRate,
    };
    writeSession(session);
    return { ...session, rotated: true };
  }
  session.lastActivity = now;
  // Sessions written before sampling existed have no decision stored.
  if (typeof session.sampled !== 'boolean') session.sampled = true;
  writeSession(session);
  return { ...session, rotated: false };
}

function getWindowId() {
  let id = sessionStorage.getItem(WINDOW_STORAGE_KEY);
  if (!id) {
    id = uuid();
    sessionStorage.setItem(WINDOW_STORAGE_KEY, id);
  }
  return id;
}

let session = getSession();
let sessionId = session.id;
const windowId = getWindowId();

// ---------------------------------------------------------------------------
// Recording state machine (PostHog's ingestion controls, in miniature)
//
//   'disabled'  — session sampled out; nothing recorded, nothing sent.
//   'buffering' — recording into a rolling in-memory buffer, nothing sent;
//                 waiting for a trigger. Discarded on unload if none fires.
//   'active'    — events flush to the server.
//
// With no triggers configured, a sampled-in session is 'active' immediately.
// ---------------------------------------------------------------------------

let status = 'disabled';
let stopFn = null; // rrweb's cleanup function while recording

// ---------------------------------------------------------------------------
// Transport: batch + gzip + keepalive fetch
// ---------------------------------------------------------------------------

let buffer = [];
let bufferBytes = 0;
let flushTimer = null;
let inFlight = false;

function resetBuffer() {
  buffer = [];
  bufferBytes = 0;
}

function pushEvent(event) {
  buffer.push(event);
  bufferBytes += JSON.stringify(event).length;
  if (status === 'active') {
    if (buffer.length >= config.maxBatchSize) flush(false);
  } else if (bufferBytes > config.maxBufferBytes) {
    // Trimming old events from the front could drop the full snapshot and
    // leave the buffer unreplayable, so restart from a fresh snapshot instead.
    resetBuffer();
    record.takeFullSnapshot(true);
  }
}

async function gzipBody(json) {
  if (typeof CompressionStream === 'undefined') {
    return { body: json, compressed: false };
  }
  const stream = new Blob([json]).stream().pipeThrough(new CompressionStream('gzip'));
  const body = await new Response(stream).arrayBuffer();
  return { body, compressed: true };
}

async function flush(useKeepalive) {
  if (status !== 'active' || buffer.length === 0 || inFlight) return;
  const events = buffer;
  resetBuffer();
  inFlight = true;

  const payload = JSON.stringify({
    session_id: sessionId,
    window_id: windowId,
    url: location.href,
    sent_at: Date.now(),
    events,
  });

  try {
    const { body, compressed } = await gzipBody(payload);
    const params = new URLSearchParams({ session_id: sessionId, window_id: windowId });
    if (compressed) params.set('compression', 'gzip-js');
    await fetch(`${config.endpoint}?${params}`, {
      method: 'POST',
      body,
      keepalive: Boolean(useKeepalive),
      headers: { 'Content-Type': compressed ? 'application/octet-stream' : 'application/json' },
    });
  } catch {
    // Put events back so the next flush retries them (bounded so memory
    // can't grow without limit if the endpoint is down).
    buffer = events.concat(buffer).slice(-1000);
    bufferBytes = buffer.reduce((n, e) => n + JSON.stringify(e).length, 0);
  } finally {
    inFlight = false;
  }
}

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setInterval(() => flush(false), config.flushIntervalMs);
}

// ---------------------------------------------------------------------------
// Triggers
// ---------------------------------------------------------------------------

const urlTriggers = ((config.triggers && config.triggers.urls) || []).map(
  (pattern) => new RegExp(pattern)
);

/** buffering → active: fire the buffered lead-up at the server. */
function activate(reason) {
  if (status !== 'buffering') return;
  status = 'active';
  // The decision is per session, like sampling: persist it so page
  // navigations within a triggered session keep recording immediately.
  const current = readSession();
  if (current) {
    current.triggered = true;
    writeSession(current);
  }
  // Stamp the reason into the event stream so replay shows why this
  // session was kept (custom events are ignored by the player itself).
  record.addCustomEvent('capture-trigger', { reason });
  flush(false);
}

function checkUrlTriggers() {
  if (status !== 'buffering' || urlTriggers.length === 0) return;
  if (urlTriggers.some((re) => re.test(location.href))) activate('url');
}

if (urlTriggers.length) {
  // SPA route changes don't reload the page; wrap history so URL triggers
  // see them.
  for (const method of ['pushState', 'replaceState']) {
    const original = history[method].bind(history);
    history[method] = (...args) => {
      const result = original(...args);
      checkUrlTriggers();
      return result;
    };
  }
  window.addEventListener('popstate', checkUrlTriggers);
  window.addEventListener('hashchange', checkUrlTriggers);
}

if (config.triggers && config.triggers.onException) {
  window.addEventListener('error', () => activate('exception'));
  window.addEventListener('unhandledrejection', () => activate('exception'));
}

// Manual API: capture.trigger('saw-bug') from the host page.
window.capture = Object.assign(window.capture || {}, {
  trigger: (reason) => activate(reason || 'manual'),
  status: () => status,
});

// ---------------------------------------------------------------------------
// Recording
// ---------------------------------------------------------------------------

/** Handle an idle/max-length rotation observed mid-page. */
function onSessionRotated(next) {
  sessionId = next.id;
  resetBuffer();
  if (!next.sampled) {
    status = 'disabled';
    // Defer the stop: we're inside rrweb's emit right now.
    setTimeout(() => {
      if (stopFn) {
        stopFn();
        stopFn = null;
      }
      watchForNextSession();
    }, 0);
    return;
  }
  status = config.triggers ? 'buffering' : 'active';
  // A new session must start with a fresh full snapshot to be replayable.
  record.takeFullSnapshot(true);
  checkUrlTriggers();
}

function startRecording() {
  // A session that already fired a trigger stays active across page loads.
  status = config.triggers && !readSession()?.triggered ? 'buffering' : 'active';
  stopFn = record({
    emit(event) {
      // Keep the session alive + detect idle rotation on real activity.
      const current = getSession();
      if (current.rotated) {
        onSessionRotated(current);
        if (status === 'disabled') return;
      }
      pushEvent(event);
    },
    checkoutEveryNms: config.checkoutEveryNms,

    // Privacy defaults: mask every input before it leaves the browser; block
    // whole elements with class="capture-block"; mask text with
    // class="capture-mask".
    maskAllInputs: true,
    blockClass: 'capture-block',
    maskTextClass: 'capture-mask',

    // Trim high-frequency noise.
    sampling: {
      mousemove: 50, // ms between mousemove events
      scroll: 100,
      media: 800,
      input: 'last', // only record final value of rapid input bursts
    },

    plugins: [
      getRecordConsolePlugin({
        level: ['log', 'info', 'warn', 'error'],
        lengthThreshold: 1000,
        stringifyOptions: { stringLengthLimit: 1000, numOfKeysLimit: 100, depthOfLimit: 3 },
      }),
    ],
  });
  scheduleFlush();
  checkUrlTriggers();
}

/**
 * Sampled-out sessions record nothing, but the page stays instrumented:
 * lightweight activity listeners keep the stored session alive, and when it
 * eventually rotates (idle timeout / max length) the new session re-rolls
 * the sampling dice and may start recording.
 */
function watchForNextSession() {
  let lastCheck = 0;
  const onActivity = () => {
    const now = Date.now();
    if (now - lastCheck < 5000) return; // throttle localStorage writes
    lastCheck = now;
    const current = getSession();
    if (!current.rotated) return;
    sessionId = current.id;
    if (!current.sampled) return; // re-rolled and sampled out again
    for (const type of ['pointerdown', 'keydown', 'scroll']) {
      removeEventListener(type, onActivity, true);
    }
    startRecording();
  };
  for (const type of ['pointerdown', 'keydown', 'scroll']) {
    addEventListener(type, onActivity, { capture: true, passive: true });
  }
}

if (session.sampled) {
  startRecording();
} else {
  watchForNextSession();
}

// Final flush when the tab is hidden or closed. pagehide + visibilitychange
// covers desktop and mobile; keepalive lets the request outlive the page.
// A still-buffering session is discarded here by design: no trigger fired,
// so it was never worth keeping (flush() is a no-op unless 'active').
window.addEventListener('pagehide', () => flush(true));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') flush(true);
});
