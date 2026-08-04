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

// --- console text -----------------------------------------------------------

const { redactText } = await import('../src/redact.js');

test('redacts the analytics token shape found in real recordings', () => {
  // Copied from a stored chunk: an analytics payload logged to the console.
  const logged =
    '{"percent":25,"lp_variant":"lp_home","page":"homepage","device_type":"desktop",' +
    '"distinct_id":"$device:410940fb-283b-40f8-bc32-b3618af11fef",' +
    '"token":"0cdb79cbfd3d47721c52bcfc653c162c","time":1785741807}';
  const out = redactText(logged);
  assert.ok(!out.includes('0cdb79cbfd3d47721c52bcfc653c162c'), 'the token must not survive');
  assert.ok(out.includes('"token":"REDACTED"'));
  // Everything else is what makes the log worth keeping.
  assert.ok(out.includes('"percent":25'));
  assert.ok(out.includes('"page":"homepage"'));
});

test('redacts the escaped-quote form rrweb produces for stringified JSON', () => {
  // console.log(JSON.stringify(obj)) — rrweb escapes the inner quotes.
  const logged = '"{\\"percent\\":25,\\"token\\":\\"0cdb79cbfd3d47721c52bcfc653c162c\\"}"';
  const out = redactText(logged);
  assert.ok(!out.includes('0cdb79cbfd3d47721c52bcfc653c162c'));
  assert.ok(out.includes('\\"token\\":\\"REDACTED\\"'));
  assert.ok(out.includes('\\"percent\\":25'));
});

test('redacts secrets in logged URLs', () => {
  const out = redactText('fetching https://api.example.com/v1?api_key=abcdef123456&page=2 now');
  assert.ok(!out.includes('abcdef123456'));
  assert.ok(out.includes('page=2'));
});

test('redacts a bare key=value pair', () => {
  assert.equal(redactText('using token=abcdef1234567890'), 'using token=REDACTED');
});

test('leaves ordinary log lines untouched', () => {
  for (const line of [
    'user clicked the checkout button',
    '{"code":200,"ok":true}',
    '{"count":42,"page":"home"}',
    'GET /api/orders 200 in 34ms',
  ]) {
    assert.equal(redactText(line), line);
  }
});

test('does not redact short values that are unlikely to be secrets', () => {
  assert.equal(redactText('{"code":"200"}'), '{"code":"200"}');
});

test('is safe on non-string input', () => {
  for (const value of [null, undefined, 42, {}]) {
    assert.doesNotThrow(() => redactText(value));
  }
});

test('honours a caller-supplied parameter list', () => {
  assert.equal(
    redactUrl('https://brand.example/a?custom=v&token=t', ['custom']),
    'https://brand.example/a?custom=REDACTED&token=t'
  );
});
