import { record } from 'rrweb';
import { redactUrl } from './redact.js';
import { beaconEvents } from './beacon.js';
import { config } from './config.js';
import { status, sessionId, windowId, visitorId } from './state.js';
import { tryTransition } from './triggers.js';
import { abort, stopFn } from './recording.js';

const KEEPALIVE_SAFE_BYTES = 51 * 1024;
const BEACON_SAFE_BYTES = 60 * 1024;
const HEAL_COOLDOWN_MS = 60 * 1000;

let buffer = [];
let bufferBytes = 0;
let flushTimer = null;
let inFlight = false;
let snapshotDeliveredFor = null;
let lastHealAt = 0;

export function resetBuffer() {
  buffer = [];
  bufferBytes = 0;
}

function scrubEvent(event) {
  if (event.type === 4 && event.data && event.data.href) {
    return {
      ...event,
      data: { ...event.data, href: redactUrl(event.data.href, config.redactUrlParams) },
    };
  }
  return event;
}

export function pushEvent(event) {
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
  if (status === 'buffering' && config.minDurationMs) tryTransition('min-duration');
  if (status === 'buffering' && bufferBytes > config.maxBufferBytes) {
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

function noteDelivery(events, sid) {
  if (events.some((e) => e.type === 2)) snapshotDeliveredFor = sid;
}

function healMissingSnapshot() {
  if (!stopFn || !sessionId || snapshotDeliveredFor === sessionId) return;
  if (buffer.some((e) => e.type === 2)) return;
  if (Date.now() - lastHealAt < HEAL_COOLDOWN_MS) return;
  lastHealAt = Date.now();
  record.takeFullSnapshot(true);
}

function dropFromBuffer(events) {
  const sent = new Set(events);
  const remaining = buffer.filter((e) => !sent.has(e));
  if (remaining.length !== buffer.length) {
    buffer = remaining;
    bufferBytes = buffer.reduce((n, e) => n + JSON.stringify(e).length, 0);
  }
}

function requeue(events) {
  buffer = events.concat(buffer);
  bufferBytes = buffer.reduce((n, e) => n + JSON.stringify(e).length, 0);
  if (bufferBytes > config.maxBufferBytes && stopFn) {
    resetBuffer();
    record.takeFullSnapshot(true);
  }
}

function ingestUrl(compressed) {
  const params = new URLSearchParams({ session_id: sessionId, window_id: windowId });
  if (config.token) params.set('token', config.token);
  if (compressed) params.set('compression', 'gzip-js');
  return `${config.endpoint}?${params}`;
}

async function flushAsync() {
  if (inFlight) return;
  inFlight = true;
  try {
    healMissingSnapshot();
  } catch {
  }
  const events = buffer;
  resetBuffer();
  const sid = sessionId;

  try {
    const { body, compressed } = await gzipBody(payloadFor(events));
    const res = await fetch(ingestUrl(compressed), {
      method: 'POST',
      body,
      keepalive: bodyBytes(body) < KEEPALIVE_SAFE_BYTES,
      headers: { 'Content-Type': compressed ? 'application/octet-stream' : 'application/json' },
    });

    if (res.status === 401) {
      abort('ingest rejected the project token');
    } else if (res.status >= 500) {
      requeue(events);
    } else if (!res.ok) {
      console.error(`capture: ingest rejected a batch (${res.status}), dropping it`);
    } else {
      noteDelivery(events, sid);
    }
  } catch {
    requeue(events);
  } finally {
    inFlight = false;
  }
}

function flushBeacon(unloading) {
  const events = buffer;
  resetBuffer();
  const url = ingestUrl(false);
  const sid = sessionId;
  let batch = [];
  const kept = [];
  beaconEvents(
    events,
    (b) => {
      batch = b;
      return payloadFor(b);
    },
    (json) => {
      try {
        const ok = Boolean(
          navigator.sendBeacon && navigator.sendBeacon(url, new Blob([json], { type: 'text/plain' }))
        );
        if (ok) noteDelivery(batch, sid);
        return ok;
      } catch {
        return false;
      }
    },
    (json) => {
      if (!unloading) {
        kept.push(...batch);
        return;
      }
      try {
        fetch(url, { method: 'POST', body: json, headers: { 'Content-Type': 'application/json' } });
      } catch {
      }
    }
  );
  if (kept.length) retainAndRetry(kept, sid);
}

async function retainAndRetry(events, sid) {
  requeue(events);
  try {
    const { body, compressed } = await gzipBody(payloadFor(events));
    let delivered = false;
    if (compressed && bodyBytes(body) < BEACON_SAFE_BYTES && navigator.sendBeacon) {
      delivered = navigator.sendBeacon(ingestUrl(true), new Blob([body], { type: 'text/plain' }));
    }
    if (!delivered) {
      const res = await fetch(ingestUrl(compressed), {
        method: 'POST',
        body,
        headers: { 'Content-Type': compressed ? 'application/octet-stream' : 'application/json' },
      });
      delivered = res.ok;
    }
    if (delivered) {
      dropFromBuffer(events);
      noteDelivery(events, sid);
    }
  } catch {
  }
}

export function flush(mode) {
  if (status !== 'active' || buffer.length === 0) return;
  if (mode) {
    flushBeacon(mode === 'unload');
  } else {
    return flushAsync();
  }
}

export function scheduleFlush() {
  if (flushTimer) return;
  flushTimer = setInterval(() => flush(false), config.flushIntervalMs);
}

export function stopFlushTimer() {
  if (flushTimer) {
    clearInterval(flushTimer);
    flushTimer = null;
  }
}
