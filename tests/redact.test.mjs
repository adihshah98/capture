/**
 * URL redaction. Credentials ride in query strings and OAuth fragments, and a
 * recording is a durable copy of whatever it captured.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

globalThis.location = { href: 'https://brand.example/checkout' };
const { redactUrl } = await import('../src/redact.js');

const cases = [
  ['leaves a plain URL alone', 'https://brand.example/checkout', 'https://brand.example/checkout'],
  ['keeps harmless parameters', 'https://brand.example/p?page=2&sort=asc', 'https://brand.example/p?page=2&sort=asc'],
  ['redacts a reset token', 'https://brand.example/reset?token=abc123', 'https://brand.example/reset?token=REDACTED'],
  ['matches substrings: access_token', 'https://brand.example/cb?access_token=xyz', 'https://brand.example/cb?access_token=REDACTED'],
  ['matches camelCase: csrfToken', 'https://brand.example/a?csrfToken=q1', 'https://brand.example/a?csrfToken=REDACTED'],
  ['redacts an email', 'https://brand.example/i?email=a%40b.com', 'https://brand.example/i?email=REDACTED'],
  ['keeps safe params while dropping secrets', 'https://brand.example/x?page=2&api_key=k', 'https://brand.example/x?page=2&api_key=REDACTED'],
  ['redacts an OAuth implicit-flow fragment', 'https://brand.example/cb#access_token=zz&state=ok', 'https://brand.example/cb#access_token=REDACTED&state=ok'],
  ['leaves an ordinary fragment alone', 'https://brand.example/docs#section-3', 'https://brand.example/docs#section-3'],
  ['strips basic-auth credentials', 'https://user:pw@brand.example/a', 'https://brand.example/a'],
  ['is case-insensitive on parameter names', 'https://brand.example/a?TOKEN=x', 'https://brand.example/a?TOKEN=REDACTED'],
  ['resolves relative URLs', '/reset?password=hunter2', 'https://brand.example/reset?password=REDACTED'],
];

for (const [name, input, expected] of cases) {
  test(name, () => assert.equal(redactUrl(input), expected));
}

test('never throws on malformed input', () => {
  for (const bad of ['', 'not a url', '://///', null, undefined, 123]) {
    assert.doesNotThrow(() => redactUrl(bad));
  }
});

test('honours a caller-supplied parameter list', () => {
  assert.equal(
    redactUrl('https://brand.example/a?custom=v&token=t', ['custom']),
    'https://brand.example/a?custom=REDACTED&token=t'
  );
});
