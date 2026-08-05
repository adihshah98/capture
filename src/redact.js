// Matched as substrings, so `token` also catches `access_token` and
// `csrfToken`. Over-matching costs detail; under-matching stores a credential.
export const DEFAULT_REDACT_PARAMS = [
  'token', 'password', 'passwd', 'pwd', 'secret', 'auth', 'apikey', 'api_key',
  'session', 'signature', 'credential', 'jwt', 'bearer', 'otp', 'code', 'email', 'phone',
];

const PLACEHOLDER = 'REDACTED';

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
