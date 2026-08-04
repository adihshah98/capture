/**
 * Redaction shared by the recorder and the network plugin. A recording is a
 * durable copy of whatever it captured, and credentials ride in query strings
 * and console payloads, so they are stripped before anything is stored.
 */

// Matched as substrings, so `token` also catches `access_token` and
// `csrfToken`. Over-matching costs detail; under-matching stores a credential.
export const DEFAULT_REDACT_PARAMS = [
  'token', 'password', 'passwd', 'pwd', 'secret', 'auth', 'apikey', 'api_key',
  'session', 'signature', 'credential', 'jwt', 'bearer', 'otp', 'code', 'email', 'phone',
];

const PLACEHOLDER = 'REDACTED';

/** Returns the rewritten query string, or null when nothing matched. */
function redactPairs(query, names) {
  const params = new URLSearchParams(query);
  let hit = false;
  for (const name of [...params.keys()]) {
    const lower = name.toLowerCase();
    if (names.some((needle) => lower.includes(needle))) {
      params.set(name, PLACEHOLDER);
      hit = true;
    }
  }
  return hit ? params.toString() : null;
}

/**
 * Free text, i.e. console arguments. Values under 8 characters are left
 * alone: rarely credentials, and redacting `"code": 200` costs more than it
 * buys.
 */
export function redactText(text, names = DEFAULT_REDACT_PARAMS) {
  if (typeof text !== 'string' || !text) return text;
  const sensitive = (key) => names.some((needle) => key.toLowerCase().includes(needle));

  return text
    // ) excluded so a stack frame's bracket survives; :line:column split off
    // so frames stay readable.
    .replace(/https?:\/\/[^\s"'<>\\)]+/gi, (url) => {
      const frame = url.match(/^(.*?)(:\d+:\d+)$/);
      return frame ? redactUrl(frame[1], names) + frame[2] : redactUrl(url, names);
    })
    // Logging an object gives plain quotes, a stringified one escaped quotes.
    .replace(/\\"([\w$-]+)\\"(\s*:\s*)\\"([^"\\]{8,})\\"/g, (whole, key, sep) =>
      sensitive(key) ? `\\"${key}\\"${sep}\\"${PLACEHOLDER}\\"` : whole
    )
    .replace(/"([\w$-]+)"(\s*:\s*)"([^"]{8,})"/g, (whole, key, sep) =>
      sensitive(key) ? `"${key}"${sep}"${PLACEHOLDER}"` : whole
    )
    // Skips values already replaced above: re-matching a redacted URL would
    // swallow whatever trails it, such as a stack frame's :line:column.
    .replace(/\b([\w-]+)=([^\s&"']{8,})/g, (whole, key, value) =>
      sensitive(key) && !value.startsWith(PLACEHOLDER) ? `${key}=${PLACEHOLDER}` : whole
    );
}

export function redactUrl(url, names = DEFAULT_REDACT_PARAMS) {
  try {
    const parsed = new URL(url, location.href);
    parsed.username = '';
    parsed.password = '';

    if (parsed.search) {
      const cleaned = redactPairs(parsed.search.slice(1), names);
      if (cleaned !== null) parsed.search = cleaned;
    }
    // OAuth implicit flow returns access tokens in the fragment, so a hash
    // shaped like a query string gets the same treatment.
    if (parsed.hash.includes('=')) {
      const cleaned = redactPairs(parsed.hash.slice(1), names);
      if (cleaned !== null) parsed.hash = cleaned;
    }
    return parsed.href;
  } catch {
    return String(url);
  }
}
