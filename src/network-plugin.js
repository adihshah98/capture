/**
 * Network capture plugin for rrweb: wraps fetch + XMLHttpRequest, and reads
 * PerformanceObserver resource entries for everything else (images, scripts,
 * styles). Privacy-first: URLs/method/status/timings only by default —
 * headers and bodies are opt-in, and sensitive headers are redacted even
 * when opted in.
 */

export const NETWORK_PLUGIN_NAME = 'capture/network@1';

const REDACTED_HEADERS = new Set([
  'authorization',
  'proxy-authorization',
  'cookie',
  'set-cookie',
  'x-api-key',
  'x-auth-token',
  'x-csrf-token',
]);

const DEFAULTS = {
  ignoreUrls: [], // regex strings; the recorder adds its own ingest endpoint
  captureHeaders: false,
  captureBody: false,
  maxBodyBytes: 64 * 1024,
  captureResources: true,
};

function sanitizeUrl(url) {
  try {
    const u = new URL(url, location.href);
    u.username = '';
    u.password = '';
    return u.href;
  } catch {
    return String(url);
  }
}

function sanitizeHeaders(entries) {
  const out = {};
  for (const [name, value] of entries) {
    out[name.toLowerCase()] = REDACTED_HEADERS.has(name.toLowerCase())
      ? '[redacted]'
      : value;
  }
  return out;
}

function headerEntries(headers) {
  if (!headers) return [];
  if (typeof headers.entries === 'function') return [...headers.entries()];
  if (Array.isArray(headers)) return headers;
  return Object.entries(headers);
}

function truncate(text, maxBytes) {
  return text.length > maxBytes ? text.slice(0, maxBytes) + '…[truncated]' : text;
}

function initNetworkObserver(cb, win, options) {
  const opts = Object.assign({}, DEFAULTS, options);
  const ignore = opts.ignoreUrls.map((p) => new RegExp(p));
  const shouldIgnore = (url) => ignore.some((re) => re.test(url));
  const cleanups = [];

  const originalFetch = win.fetch;
  win.fetch = async function (input, init) {
    const url = sanitizeUrl(
      typeof input === 'string' ? input : input instanceof Request ? input.url : input
    );
    if (shouldIgnore(url)) return originalFetch.apply(this, arguments);

    const method =
      (init && init.method) || (input instanceof Request && input.method) || 'GET';
    const startMs = Date.now();
    const start = performance.now();
    const entry = { type: 'fetch', url, method: method.toUpperCase(), startMs };

    if (opts.captureHeaders) {
      entry.requestHeaders = sanitizeHeaders(
        headerEntries((init && init.headers) || (input instanceof Request && input.headers))
      );
    }
    if (opts.captureBody && init && typeof init.body === 'string') {
      entry.requestBody = truncate(init.body, opts.maxBodyBytes);
    }

    try {
      const response = await originalFetch.apply(this, arguments);
      entry.status = response.status;
      entry.durationMs = Math.round(performance.now() - start);
      if (opts.captureHeaders) {
        entry.responseHeaders = sanitizeHeaders(headerEntries(response.headers));
      }
      if (opts.captureBody) {
        try {
          entry.responseBody = truncate(await response.clone().text(), opts.maxBodyBytes);
        } catch {
          /* opaque/stream body — skip */
        }
      }
      cb(entry);
      return response;
    } catch (err) {
      entry.status = 0;
      entry.durationMs = Math.round(performance.now() - start);
      entry.error = String(err && err.message ? err.message : err);
      cb(entry);
      throw err;
    }
  };
  cleanups.push(() => {
    win.fetch = originalFetch;
  });

  const XHR = win.XMLHttpRequest;
  const originalOpen = XHR.prototype.open;
  const originalSend = XHR.prototype.send;
  XHR.prototype.open = function (method, url, ...rest) {
    this.__capture_net = { method: String(method).toUpperCase(), url: sanitizeUrl(url) };
    return originalOpen.call(this, method, url, ...rest);
  };
  XHR.prototype.send = function (body) {
    const meta = this.__capture_net;
    if (meta && !shouldIgnore(meta.url)) {
      const startMs = Date.now();
      const start = performance.now();
      this.addEventListener('loadend', () => {
        const entry = {
          type: 'xhr',
          url: meta.url,
          method: meta.method,
          status: this.status,
          startMs,
          durationMs: Math.round(performance.now() - start),
        };
        if (this.status === 0) entry.error = 'network error or aborted';
        if (opts.captureBody && typeof body === 'string') {
          entry.requestBody = truncate(body, opts.maxBodyBytes);
        }
        if (opts.captureBody && (this.responseType === '' || this.responseType === 'text')) {
          try {
            entry.responseBody = truncate(this.responseText, opts.maxBodyBytes);
          } catch {
            /* responseText not readable for this responseType */
          }
        }
        cb(entry);
      });
    }
    return originalSend.call(this, body);
  };
  cleanups.push(() => {
    XHR.prototype.open = originalOpen;
    XHR.prototype.send = originalSend;
  });

  if (opts.captureResources && typeof PerformanceObserver !== 'undefined') {
    const timeOrigin = Date.now() - performance.now(); // performance.now() → epoch ms
    const seen = new Set();
    const observer = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        // fetch/xhr already come from the wrappers above, with status codes.
        if (e.initiatorType === 'fetch' || e.initiatorType === 'xmlhttprequest') continue;
        const url = sanitizeUrl(e.name);
        if (shouldIgnore(url) || seen.has(e.name + e.startTime)) continue;
        seen.add(e.name + e.startTime);
        cb({
          type: 'resource',
          url,
          initiatorType: e.initiatorType,
          startMs: Math.round(timeOrigin + e.startTime),
          durationMs: Math.round(e.duration),
          transferSize: e.transferSize || undefined,
          status: e.responseStatus || undefined,
        });
      }
    });
    // buffered: true replays entries from before the recorder loaded, so
    // page-load resources aren't lost to the lazy-loaded bundle.
    observer.observe({ type: 'resource', buffered: true });
    cleanups.push(() => observer.disconnect());
  }

  return () => cleanups.forEach((fn) => fn());
}

export function getRecordNetworkPlugin(options) {
  return {
    name: NETWORK_PLUGIN_NAME,
    observer: initNetworkObserver,
    options: options || {},
  };
}
