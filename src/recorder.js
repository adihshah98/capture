import { record } from 'rrweb';
import { DEFAULT_REDACT_PARAMS, redactUrl } from './redact.js';

const DEFAULTS = {
  endpoint: '/ingest',
  token: null,

  remoteConfig: true,
  configUrl: '/config',
  remoteConfigTtlMs: 60 * 60 * 1000,

  flushIntervalMs: 5000,
  maxBatchSize: 50,
  // Also flush on serialized size — event count is a weak proxy when a
  // single full snapshot can be most of a megabyte.
  maxBatchBytes: 900 * 1024,
  idleThresholdMs: 5 * 60 * 1000,
  checkoutEveryNms: 5 * 60 * 1000,
  sessionIdleTimeoutMs: 30 * 60 * 1000,
  sessionMaxLengthMs: 24 * 60 * 60 * 1000,

  sampleRate: 1, // sticky per-session keep probability
  triggers: null,
  minDurationMs: 0,
  maxBufferBytes: 2 * 1024 * 1024,

  maskTextSelector: null,
  blockSelector: null,
  redactUrlParams: DEFAULT_REDACT_PARAMS,

  // Read from the inline tag only: a remote value would arrive after the
  // storage write it must prevent.
  requireConsent: false,
  // A legally recognised opt-out signal in several US states.
  respectGlobalPrivacyControl: true,
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
const VISITOR_STORAGE_KEY = 'capture_visitor';
const WINDOW_STORAGE_KEY = 'capture_window_id';
const OPT_OUT_KEY = 'capture_opt_out';

// Storage can throw outright when blocked, and that must never stop a page.
function tryStorage(fn, fallback = null) {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

const isOptedOut = () => tryStorage(() => localStorage.getItem(OPT_OUT_KEY) === '1', false);

// Persisting the refusal is itself a write; the alternative is asking again
// on every page load.
const setOptedOut = (value) =>
  tryStorage(() =>
    value ? localStorage.setItem(OPT_OUT_KEY, '1') : localStorage.removeItem(OPT_OUT_KEY)
  );

const forgetVisitor = () =>
  tryStorage(() => {
    localStorage.removeItem(SESSION_STORAGE_KEY);
    localStorage.removeItem(VISITOR_STORAGE_KEY);
    localStorage.removeItem(REMOTE_CONFIG_KEY);
    sessionStorage.removeItem(WINDOW_STORAGE_KEY);
  });

function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

const readSession = () =>
  tryStorage(() => JSON.parse(localStorage.getItem(SESSION_STORAGE_KEY))) || null;

function writeSession(session) {
  localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
}

// The sampling decision is rolled once at creation and persisted, so a
// session is wholly recorded or wholly skipped — never a fragment.
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

const getVisitorId = () =>
  tryStorage(() => {
    let id = localStorage.getItem(VISITOR_STORAGE_KEY);
    if (!id) {
      id = uuid();
      localStorage.setItem(VISITOR_STORAGE_KEY, id);
    }
    return id;
  });

let session = null;
let sessionId = null;
let windowId = null;
let visitorId = null;

// 'disabled' (sampled out) | 'buffering' (recording into a rolling buffer,
// nothing sent until a trigger fires; discarded on unload) | 'active'
// (events flush to the server) | 'failed' (ingest rejected us permanently) |
// 'awaiting-consent' (requireConsent, banner unanswered) | 'opted-out'.
let status = 'disabled';
let stopFn = null;

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

// rrweb's Meta event carries the page URL verbatim, and it — not our own
// chunk-level copy — is what replay treats as the session's location.
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
  if (status === 'failed' || status === 'opted-out') return;
  event = scrubEvent(event);
  buffer.push(event);
  bufferBytes += JSON.stringify(event).length;
  if (status === 'active') {
    if (buffer.length >= config.maxBatchSize || bufferBytes >= config.maxBatchBytes) {
      flush(false);
    }
    return;
  }
  // The duration gate is time-based, so re-check it as events arrive rather
  // than on a timer of its own.
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
    visitor_id: visitorId,
    url: redactUrl(location.href, config.redactUrlParams),
    sent_at: Date.now(),
    events,
  });
}

const bodyBytes = (body) => (typeof body === 'string' ? body.length : body.byteLength);

function requeue(events) {
  buffer = events.concat(buffer);
  bufferBytes = buffer.reduce((n, e) => n + JSON.stringify(e).length, 0);
  // Trimming the front would drop the full snapshot and leave the buffer
  // unreplayable, so restart from a fresh one.
  if (bufferBytes > config.maxBufferBytes && stopFn) {
    resetBuffer();
    record.takeFullSnapshot(true);
  }
}

function stopRecording(nextStatus) {
  status = nextStatus;
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

function abort(message) {
  console.error(`capture: ${message} — recording stopped`);
  stopRecording('failed');
}

async function flush(useKeepalive) {
  if (status !== 'active' || buffer.length === 0 || inFlight) return;
  let events = buffer;
  resetBuffer();
  inFlight = true;

  try {
    let { body, compressed } = await gzipBody(payloadFor(events));

    // An oversized final batch would be rejected outright at the keepalive
    // cap. Keep the oldest events that fit: a gap breaks replay of
    // everything after it.
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

function gatesSatisfied() {
  const current = readSession();
  const triggerOk = !config.triggers || Boolean(current && current.triggered);
  const durationOk =
    !config.minDurationMs ||
    (current && Date.now() - current.started >= config.minDurationMs);
  return triggerOk && durationOk;
}

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
    config: () => ({ ...config }),
  });
}

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

// rrweb IncrementalSource values for real user interaction: MouseMove(1),
// MouseInteraction(2), Scroll(3), Input(5), TouchMove(6).
const INTERACTIVE_SOURCES = new Set([1, 2, 3, 5, 6]);
const isInteractive = (event) =>
  event.type === 3 && INTERACTIVE_SOURCES.has(event.data && event.data.source);

let idle = false;
let lastUserActivity = Date.now();

// Markers are pushed directly: addCustomEvent would recurse through emit.
function pushMarker(tag) {
  pushEvent({ type: 5, data: { tag, payload: {} }, timestamp: Date.now() });
}

function startRecording() {
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
          // Waking up: fresh snapshot so replay resumes from current
          // reality rather than a stale pre-idle DOM.
          idle = false;
          pushMarker('capture-idle-end');
          record.takeFullSnapshot(true);
        }
        pushEvent(event);
        return;
      }

      if (idle) return;
      if (Date.now() - lastUserActivity > config.idleThresholdMs) {
        idle = true;
        pushMarker('capture-idle');
        flush(false);
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
      mousemove: 50,
      scroll: 100,
      media: 800,
      input: 'last', // only the final value of rapid input bursts
    },
  });
  scheduleFlush();
  checkUrlTriggers();
}

// Sampled-out sessions record nothing, but lightweight listeners keep the
// stored session alive so the eventual rotation re-rolls the sampling dice.
function watchForNextSession() {
  let lastCheck = 0;
  const onActivity = () => {
    const now = Date.now();
    if (now - lastCheck < 5000) return;
    lastCheck = now;
    const current = getSession();
    if (!current.rotated) return;
    sessionId = current.id;
    if (!current.sampled) return;
    for (const type of ['pointerdown', 'keydown', 'scroll']) {
      removeEventListener(type, onActivity, true);
    }
    startRecording();
  };
  for (const type of ['pointerdown', 'keydown', 'scroll']) {
    addEventListener(type, onActivity, { capture: true, passive: true });
  }
}

let booted = false;

async function boot() {
  if (booted) return;
  booted = true;

  // Nothing records until the effective config is known — remote settings
  // must not race the first events.
  config = await resolveConfig();

  session = getSession();
  sessionId = session.id;
  windowId = getWindowId();
  visitorId = getVisitorId();
  setupTriggers();

  if (session.sampled) {
    startRecording();
  } else {
    watchForNextSession();
  }

  // pagehide + visibilitychange covers desktop and mobile; keepalive lets
  // the request outlive the page. A still-buffering session is discarded
  // here by design: no trigger fired, so it was never worth keeping.
  window.addEventListener('pagehide', () => flush(true));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush(true);
  });
}

// Published before the consent gate so a consent banner can always reach it,
// whatever state recording is in.
function publishConsentApi() {
  window.capture = Object.assign(window.capture || {}, {
    status: () => status,
    grantConsent: () => {
      setOptedOut(false);
      boot();
    },
    optIn: () => window.capture.grantConsent(),
    optOut: () => {
      setOptedOut(true);
      stopRecording('opted-out');
      forgetVisitor();
    },
    isOptedOut,
  });
}

function main() {
  const inline = window.CAPTURE_CONFIG || {};
  publishConsentApi();

  if (isOptedOut()) {
    status = 'opted-out';
    return;
  }
  // Checked from navigator and the inline tag alone, so honouring it costs
  // no storage access and no config fetch. grantConsent() still overrides:
  // an explicit banner acceptance is the brand's call to make.
  if (navigator.globalPrivacyControl === true && inline.respectGlobalPrivacyControl !== false) {
    status = 'opted-out';
    return;
  }
  if (inline.requireConsent === true) {
    status = 'awaiting-consent';
    return;
  }

  boot();
}

main();
