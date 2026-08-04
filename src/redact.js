/**
 * URL redaction, shared by the recorder (page URLs) and the network plugin
 * (request URLs). Credentials routinely ride in query strings — password
 * reset links, magic links, OAuth codes — and a recording is a durable copy
 * of whatever it captured, so they are stripped before anything is stored.
 */

// Matched as substrings of the parameter name, so `token` also catches
// `access_token` and `csrfToken`. Over-matching only costs debugging detail;
// under-matching stores a live credential.
export const DEFAULT_REDACT_PARAMS = [
  'token',
  'password',
  'passwd',
  'pwd',
  'secret',
  'auth',
  'apikey',
  'api_key',
  'session',
  'signature',
  'credential',
  'jwt',
  'bearer',
  'otp',
  'code',
  'email',
  'phone',
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
 * Free text — console arguments, which are recorded verbatim. A site that
 * logs an analytics or auth payload otherwise stores the secret in the
 * recording. Scrubs URLs by the same rules, plus JSON-ish `"key": "value"`
 * and bare `key=value` pairs whose key looks sensitive. Values under 8
 * characters are left alone: they are rarely credentials, and redacting
 * `"code": 200` would cost more in readability than it buys.
 */
export function redactText(text, names = DEFAULT_REDACT_PARAMS) {
  if (typeof text !== 'string' || !text) return text;
  const sensitive = (key) => names.some((needle) => key.toLowerCase().includes(needle));

  return text
    .replace(/https?:\/\/[^\s"'<>\\]+/gi, (url) => redactUrl(url, names))
    // Logging an object gives plain quotes; logging an already-stringified
    // JSON string gives escaped ones. Both shapes occur in real recordings.
    .replace(/\\"([\w$-]+)\\"(\s*:\s*)\\"([^"\\]{8,})\\"/g, (whole, key, sep) =>
      sensitive(key) ? `\\"${key}\\"${sep}\\"${PLACEHOLDER}\\"` : whole
    )
    .replace(/"([\w$-]+)"(\s*:\s*)"([^"]{8,})"/g, (whole, key, sep) =>
      sensitive(key) ? `"${key}"${sep}"${PLACEHOLDER}"` : whole
    )
    .replace(/\b([\w-]+)=([^\s&"']{8,})/g, (whole, key) =>
      sensitive(key) ? `${key}=${PLACEHOLDER}` : whole
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
