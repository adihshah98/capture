import { record } from 'rrweb';
import { redactUrl } from '../redact.js';
import { config } from './config.js';
import { status, sessionId, windowId, visitorId } from './state.js';
import { tryTransition } from './triggers.js';
import { abort, stopFn } from './recording.js';

const KEEPALIVE_MAX_BYTES = 60 * 1024;

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

export async function flush(useKeepalive) {
  if (status !== 'active' || buffer.length === 0 || inFlight) return;
  let events = buffer;
  resetBuffer();
  inFlight = true;

  try {
    let { body, compressed } = await gzipBody(payloadFor(events));

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
