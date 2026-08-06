import test from 'node:test';
import assert from 'node:assert/strict';

const { queryParams } = await import('../src/replay/params.js');

const cases = [
  ['plain URL has no params', 'https://brand.example/checkout', []],
  [
    'query params keep their order',
    'https://brand.example/?utm_source=google&utm_campaign=summer&page=2',
    [
      { key: 'utm_source', value: 'google' },
      { key: 'utm_campaign', value: 'summer' },
      { key: 'page', value: '2' },
    ],
  ],
  ['bare keys survive with empty values', 'https://brand.example/?promo', [{ key: 'promo', value: '' }]],
  ['encoded values are decoded', 'https://brand.example/?q=running%20shoes', [{ key: 'q', value: 'running shoes' }]],
  [
    'redacted values pass through untouched',
    'https://brand.example/?email=REDACTED&page=1',
    [
      { key: 'email', value: 'REDACTED' },
      { key: 'page', value: '1' },
    ],
  ],
  [
    'OAuth-style fragment params are included',
    'https://brand.example/cb?state=ok#access_token=REDACTED',
    [
      { key: 'state', value: 'ok' },
      { key: 'access_token', value: 'REDACTED' },
    ],
  ],
  ['SPA route fragments are not params', 'https://brand.example/#/checkout?step=2', []],
  ['plain anchors are not params', 'https://brand.example/docs#section-3', []],
  ['repeated keys are all kept', 'https://brand.example/?tag=a&tag=b', [
    { key: 'tag', value: 'a' },
    { key: 'tag', value: 'b' },
  ]],
];

for (const [name, url, expected] of cases) {
  test(name, () => assert.deepEqual(queryParams(url), expected));
}

test('never throws on malformed input', () => {
  for (const bad of ['', 'not a url', null, undefined, 42]) {
    assert.deepEqual(queryParams(bad), []);
  }
});
