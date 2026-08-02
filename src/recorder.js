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
 * Returns the current session id, rotating it when the visitor has been
 * idle past the timeout or the session exceeds its max length.
 */
function getSessionId() {
  const now = Date.now();
  let session = readSession();
  const expired =
    !session ||
    now - session.lastActivity > config.sessionIdleTimeoutMs ||
    now - session.started > config.sessionMaxLengthMs;
  if (expired) {
    session = { id: uuid(), started: now, lastActivity: now };
    writeSession(session);
    return { id: session.id, rotated: true };
  }
  session.lastActivity = now;
  writeSession(session);
  return { id: session.id, rotated: false };
}

function getWindowId() {
  let id = sessionStorage.getItem(WINDOW_STORAGE_KEY);
  if (!id) {
    id = uuid();
    sessionStorage.setItem(WINDOW_STORAGE_KEY, id);
  }
  return id;
}

let { id: sessionId } = getSessionId();
const windowId = getWindowId();

// ---------------------------------------------------------------------------
// Transport: batch + gzip + keepalive fetch
// ---------------------------------------------------------------------------

let buffer = [];
let flushTimer = null;
let inFlight = false;

async function gzipBody(json) {
  if (typeof CompressionStream === 'undefined') {
    return { body: json, compressed: false };
  }
  const stream = new Blob([json]).stream().pipeThrough(new CompressionStream('gzip'));
  const body = await new Response(stream).arrayBuffer();
  return { body, compressed: true };
}

async function flush(useKeepalive) {
  if (buffer.length === 0 || inFlight) return;
  const events = buffer;
  buffer = [];
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
  } finally {
    inFlight = false;
  }
}

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setInterval(() => flush(false), config.flushIntervalMs);
}

// ---------------------------------------------------------------------------
// Recording
// ---------------------------------------------------------------------------

record({
  emit(event) {
    // Keep the session alive + detect idle rotation on real activity.
    const { id, rotated } = getSessionId();
    if (rotated) {
      sessionId = id;
      // A new session must start with a fresh full snapshot to be replayable.
      record.takeFullSnapshot(true);
    }
    buffer.push(event);
    if (buffer.length >= config.maxBatchSize) flush(false);
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

// Final flush when the tab is hidden or closed. pagehide + visibilitychange
// covers desktop and mobile; keepalive lets the request outlive the page.
window.addEventListener('pagehide', () => flush(true));
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') flush(true);
});
