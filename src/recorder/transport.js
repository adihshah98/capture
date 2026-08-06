import { record } from 'rrweb';
import { redactUrl } from '../redact.js';
import { beaconEvents } from './beacon.js';
import { config } from './config.js';
import { status, sessionId, windowId, visitorId } from './state.js';
import { tryTransition } from './triggers.js';
import { abort, stopFn } from './recording.js';

const KEEPALIVE_SAFE_BYTES = 51 * 1024;

let buffer = [];
let bufferBytes = 0;
let flushTimer = null;
let inFlight = false;

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
  const events = buffer;
  resetBuffer();
  inFlight = true;

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
    }
  } catch {
    requeue(events);
  } finally {
    inFlight = false;
  }
}

function flushBeacon() {
  const events = buffer;
  resetBuffer();
  const url = ingestUrl(false);
  beaconEvents(
    events,
    (batch) => payloadFor(batch),
    (json) => {
      try {
        return Boolean(
          navigator.sendBeacon && navigator.sendBeacon(url, new Blob([json], { type: 'text/plain' }))
        );
      } catch {
        return false;
      }
    },
    (json) => {
      try {
        fetch(url, { method: 'POST', body: json, headers: { 'Content-Type': 'application/json' } });
      } catch {
      }
    }
  );
}

export function flush(useKeepalive) {
  if (status !== 'active' || buffer.length === 0) return;
  if (useKeepalive) {
    flushBeacon();
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
