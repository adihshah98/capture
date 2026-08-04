/**
 * recorder.bundle.js — the lazy-loaded rrweb recorder: records the DOM
 * (inputs masked), batches + gzips event chunks, and ships them to /ingest.
 */
import { record } from 'rrweb';
import { getRecordConsolePlugin } from '@rrweb/rrweb-plugin-console-record';
import { getRecordNetworkPlugin } from './network-plugin.js';
import { DEFAULT_REDACT_PARAMS, redactUrl } from './redact.js';

const DEFAULTS = {
  endpoint: '/ingest',
  token: null, // project token, usually set via data-token on the snippet tag

  remoteConfig: true,
  configUrl: '/config',
  remoteConfigTtlMs: 60 * 60 * 1000,

  flushIntervalMs: 5000,
  maxBatchSize: 50,
  // Also flush on serialized size — event count is a weak proxy when a
  // single full snapshot can be most of a megabyte.
  maxBatchBytes: 900 * 1024,
  // Past this much time without user interaction, non-interactive events
  // (animation/timer mutations, background tabs) are dropped, not recorded.
  idleThresholdMs: 5 * 60 * 1000,
  checkoutEveryNms: 5 * 60 * 1000, // periodic full snapshots so replay can seek
  sessionIdleTimeoutMs: 30 * 60 * 1000,
  sessionMaxLengthMs: 24 * 60 * 60 * 1000,

  // Ingestion gates; every configured gate must pass before events are sent.
  sampleRate: 1, // sticky per-session keep probability
  // null records unconditionally; an object buffers client-side until a
  // trigger fires: { urls: ['/checkout'], onException: true }, or a manual
  // window.capture.trigger('reason').
  triggers: null,
  minDurationMs: 0, // sessions shorter than this never leave the browser
  maxBufferBytes: 2 * 1024 * 1024, // pre-trigger rolling buffer bound

  // Inputs are always masked. These add page text: a CSS selector whose text
  // is replaced with asterisks, and one whose elements are not recorded at
  // all. The .capture-mask / .capture-block classes work without config.
  maskTextSelector: null,
  blockSelector: null,
  // Query/fragment parameter names whose values are stripped from every
  // recorded URL, page and network alike.
  redactUrlParams: DEFAULT_REDACT_PARAMS,

  // false disables network capture entirely.
  network: {
    captureHeaders: false,
    captureBody: false,
    captureResources: true,
    ignoreUrls: [],
  },
};

// Local-only merge so helpers work before init; main() swaps in the full
// defaults < remote < inline merge before anything records.
let config = Object.assign({}, DEFAULTS, window.CAPTURE_CONFIG || {});

const REMOTE_CONFIG_KEY = 'capture_remote_config';

async function resolveConfig() {
  const inline = window.CAPTURE_CONFIG || {};
  if (config.remoteConfig === false) return Object.assign({}, DEFAULTS, inline);

  let remote = null;
  try {
    const cached = JSON.parse(localStorage.getItem(REMOTE_CONFIG_KEY));
    if (cached && Date.now() - cached.fetchedAt < config.remoteConfigTtlMs) {
      remote = cached.config;
    }
  } catch {
    /* corrupt cache — refetch */
  }
  if (!remote) {
    try {
      const configUrl = config.token
        ? `${config.configUrl}${config.configUrl.includes('?') ? '&' : '?'}token=${encodeURIComponent(config.token)}`
        : config.configUrl;
      const res = await fetch(configUrl);
      if (res.ok) {
        remote = await res.json();
        localStorage.setItem(
          REMOTE_CONFIG_KEY,
          JSON.stringify({ config: remote, fetchedAt: Date.now() })
        );
      }
    } catch {
      /* server unreachable — record with local config rather than not at all */
    }
  }
  // Precedence: defaults < remote < inline (the page always wins). Shallow
  // merge: to override one key of e.g. `network`, remote must send the object.
  return Object.assign({}, DEFAULTS, remote || {}, inline);
}

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
 * Current session, rotated after idle timeout or max length. The sampling
 * decision is rolled once at creation and persisted, so a session is
 * wholly recorded or wholly skipped — never a fragment.
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

// Assigned in main() once config is resolved — the sampling decision
// depends on the effective sampleRate.
let session = null;
let sessionId = null;
let windowId = null;

// 'disabled' (sampled out) | 'buffering' (recording into a rolling buffer,
// nothing sent until a trigger fires; discarded on unload) | 'active'
// (events flush to the server) | 'failed' (ingest rejected us permanently).
let status = 'disabled';
let stopFn = null; // rrweb's cleanup function while recording

// Browsers cap in-flight keepalive request bodies at 64 KB; leave headroom.
const KEEPALIVE_MAX_BYTES = 60 * 1024;

let buffer = [];
let bufferBytes = 0;
let flushTimer = null;
let inFlight = false;

function resetBuffer() {
  buffer = [];
  bufferBytes = 0;
}

/**
 * rrweb's Meta event carries the page URL verbatim, and it — not our own
 * chunk-level copy — is what replay treats as the session's location. It has
 * to go through the same redaction or the secrets survive in the stream.
 */
function scrubEvent(event) {
  if (event.type === 4 && event.data && event.data.href) {
    return {
      ...event,
      data: { ...event.data, href: redactUrl(event.data.href, config.redactUrlParams) },
    };
  }
  return event;
}

function pushEvent(event) {
  if (status === 'failed') return;
  event = scrubEvent(event);
  buffer.push(event);
  bufferBytes += JSON.stringify(event).length;
  if (status === 'active') {
    if (buffer.length >= config.maxBatchSize || bufferBytes >= config.maxBatchBytes) {
      flush(false);
    }
    return;
  }
  // Buffering: the duration gate is time-based, so re-check it as events
  // arrive rather than on a timer of its own.
  if (status === 'buffering' && config.minDurationMs) tryTransition('min-duration');
  if (status === 'buffering' && bufferBytes > config.maxBufferBytes) {
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

function payloadFor(events) {
  return JSON.stringify({
    session_id: sessionId,
    window_id: windowId,
    url: redactUrl(location.href, config.redactUrlParams),
    sent_at: Date.now(),
    events,
  });
}

const bodyBytes = (body) => (typeof body === 'string' ? body.length : body.byteLength);

/** Put events back for the next flush to retry. */
function requeue(events) {
  buffer = events.concat(buffer);
  bufferBytes = buffer.reduce((n, e) => n + JSON.stringify(e).length, 0);
  // Trimming the front would drop the full snapshot and leave the buffer
  // unreplayable, so restart from a fresh one — as the pre-trigger buffer does.
  if (bufferBytes > config.maxBufferBytes && stopFn) {
    resetBuffer();
    record.takeFullSnapshot(true);
  }
}

/** A rejection no retry can fix: stop rather than record into the void. */
function abort(message) {
  console.error(`capture: ${message} — recording stopped`);
  status = 'failed';
  resetBuffer();
  if (flushTimer) {
    clearInterval(flushTimer);
    flushTimer = null;
  }
  if (stopFn) {
    stopFn();
    stopFn = null;
  }
}

async function flush(useKeepalive) {
  if (status !== 'active' || buffer.length === 0 || inFlight) return;
  let events = buffer;
  resetBuffer();
  inFlight = true;

  try {
    let { body, compressed } = await gzipBody(payloadFor(events));

    // In-flight keepalive requests share a 64 KB budget, so an oversized final
    // batch is rejected outright. Keep the oldest events that fit: a gap breaks
    // replay of everything after it.
    let dropped = 0;
    while (useKeepalive && bodyBytes(body) > KEEPALIVE_MAX_BYTES && events.length > 1) {
      const keep = Math.floor(events.length / 2);
      dropped += events.length - keep;
      events = events.slice(0, keep);
      ({ body, compressed } = await gzipBody(payloadFor(events)));
    }
    if (dropped) {
      console.warn(`capture: dropped ${dropped} trailing event(s) over the keepalive limit`);
    }

    const params = new URLSearchParams({ session_id: sessionId, window_id: windowId });
    if (config.token) params.set('token', config.token);
    if (compressed) params.set('compression', 'gzip-js');
    const res = await fetch(`${config.endpoint}?${params}`, {
      method: 'POST',
      body,
      keepalive: Boolean(useKeepalive),
      headers: { 'Content-Type': compressed ? 'application/octet-stream' : 'application/json' },
    });

    if (res.status === 401) {
      abort('ingest rejected the project token');
    } else if (res.status >= 500) {
      requeue(events);
    } else if (!res.ok) {
      console.error(`capture: ingest rejected a batch (${res.status}), dropping it`);
    }
  } catch {
    requeue(events);
  } finally {
    inFlight = false;
  }
}

function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setInterval(() => flush(false), config.flushIntervalMs);
}

let urlTriggers = [];

/** Every configured gate — a fired trigger, the minimum duration — must pass. */
function gatesSatisfied() {
  const current = readSession();
  const triggerOk = !config.triggers || Boolean(current && current.triggered);
  const durationOk =
    !config.minDurationMs ||
    (current && Date.now() - current.started >= config.minDurationMs);
  return triggerOk && durationOk;
}

/** buffering → active once every gate passes: fire the buffered lead-up. */
function tryTransition(reason) {
  if (status !== 'buffering' || !gatesSatisfied()) return;
  status = 'active';
  const current = readSession();
  // Stamped into the event stream so replay shows why the session was kept.
  record.addCustomEvent('capture-trigger', {
    reason: (current && current.triggerReason) || reason,
  });
  flush(false);
}

/**
 * A trigger fired: persist the decision on the session so later page loads
 * within it start recording immediately, then try to send.
 */
function activate(reason) {
  if (status !== 'buffering') return;
  const current = readSession();
  if (current && !current.triggered) {
    current.triggered = true;
    current.triggerReason = reason;
    writeSession(current);
  }
  tryTransition(reason);
}

function checkUrlTriggers() {
  if (status !== 'buffering' || urlTriggers.length === 0) return;
  if (urlTriggers.some((re) => re.test(location.href))) activate('url');
}

function setupTriggers() {
  urlTriggers = ((config.triggers && config.triggers.urls) || []).map(
    (pattern) => new RegExp(pattern)
  );

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

  window.capture = Object.assign(window.capture || {}, {
    trigger: (reason) => activate(reason || 'manual'),
    status: () => status,
    config: () => ({ ...config }), // effective (merged) config, for debugging
  });
}

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
  status = gatesSatisfied() ? 'active' : 'buffering';
  // A new session must start with a fresh full snapshot to be replayable.
  record.takeFullSnapshot(true);
  checkUrlTriggers();
}

// rrweb IncrementalSource values that represent real user interaction:
// MouseMove(1), MouseInteraction(2), Scroll(3), Input(5), TouchMove(6).
const INTERACTIVE_SOURCES = new Set([1, 2, 3, 5, 6]);
const isInteractive = (event) =>
  event.type === 3 && INTERACTIVE_SOURCES.has(event.data && event.data.source);

let idle = false;
let lastUserActivity = Date.now();

/** Marker events pushed directly (addCustomEvent would recurse through emit). */
function pushMarker(tag) {
  pushEvent({ type: 5, data: { tag, payload: {} }, timestamp: Date.now() });
}

function startRecording() {
  // Gate state is per session and persisted, so a session that already
  // passed its gates stays active across page loads.
  status = gatesSatisfied() ? 'active' : 'buffering';
  idle = false;
  lastUserActivity = Date.now();
  stopFn = record({
    emit(event) {
      if (isInteractive(event)) {
        lastUserActivity = Date.now();
        // Only real activity keeps the session alive / detects rotation —
        // background mutations must not stop a session from ever rotating.
        const current = getSession();
        if (current.rotated) {
          onSessionRotated(current);
          if (status === 'disabled') return;
        }
        if (idle) {
          // Waking up: mark it and take a fresh snapshot so replay resumes
          // from current reality rather than a stale pre-idle DOM.
          idle = false;
          pushMarker('capture-idle-end');
          record.takeFullSnapshot(true);
        }
        pushEvent(event);
        return;
      }

      if (idle) return; // dropped: nobody is watching
      if (Date.now() - lastUserActivity > config.idleThresholdMs) {
        idle = true;
        pushMarker('capture-idle');
        flush(false); // ship what we have; the stream pauses here
        return;
      }
      pushEvent(event);
    },
    checkoutEveryNms: config.checkoutEveryNms,

    maskAllInputs: true,
    blockClass: 'capture-block',
    maskTextClass: 'capture-mask',
    ...(config.maskTextSelector ? { maskTextSelector: config.maskTextSelector } : {}),
    ...(config.blockSelector ? { blockSelector: config.blockSelector } : {}),

    sampling: {
      mousemove: 50, // ms between events
      scroll: 100,
      media: 800,
      input: 'last', // only the final value of rapid input bursts
    },

    plugins: [
      getRecordConsolePlugin({
        level: ['log', 'info', 'warn', 'error'],
        lengthThreshold: 1000,
        stringifyOptions: { stringLengthLimit: 1000, numOfKeysLimit: 100, depthOfLimit: 3 },
      }),
      ...(config.network === false
        ? []
        : [
            getRecordNetworkPlugin({
              // Global redaction applies to request URLs too, unless the
              // network block overrides it explicitly.
              redactUrlParams: config.redactUrlParams,
              ...config.network,
              ignoreUrls: [
                // Never capture our own ingest traffic (feedback loop).
                new URL(config.endpoint, location.href).href.replace(
                  /[.*+?^${}()|[\]\\]/g,
                  '\\$&'
                ),
                ...((config.network && config.network.ignoreUrls) || []),
              ],
            }),
          ]),
    ],
  });
  scheduleFlush();
  checkUrlTriggers();
}

/**
 * Sampled-out sessions record nothing, but the page stays instrumented:
 * lightweight activity listeners keep the stored session alive, and the
 * eventual rotation re-rolls the sampling dice and may start recording.
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

async function main() {
  // Nothing records until the effective config is known — remote settings
  // must not race the first events.
  config = await resolveConfig();

  session = getSession();
  sessionId = session.id;
  windowId = getWindowId();
  setupTriggers();

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
}

main();
