import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(new URL('../../web/demo-triggers.html', import.meta.url), 'utf8');
const inlineScript = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function configFor(queryString) {
  const sandbox = { URLSearchParams, Object, console: { warn: () => {} }, location: { search: queryString } };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(inlineScript, sandbox);
  return sandbox.window.CAPTURE_CONFIG;
}

const withConfig = (obj) => `?config=${encodeURIComponent(JSON.stringify(obj))}`;

test('the URL cannot set the endpoints or the token', () => {
  const config = configFor(
    withConfig({
      recorderUrl: 'https://evil.example/x.js',
      endpoint: 'https://evil.example/collect',
      configUrl: 'https://evil.example/c',
      token: 'stolen',
    })
  );
  for (const key of ['recorderUrl', 'endpoint', 'configUrl', 'token']) {
    assert.ok(!(key in config), `${key} must not be settable from the query string`);
  }
});

test('__proto__ in the payload does not yield a recorderUrl', () => {
  const config = configFor(`?config=${encodeURIComponent('{"__proto__":{"recorderUrl":"https://evil.example/x.js"}}')}`);
  assert.equal({}.recorderUrl, undefined, 'Object.prototype must be unpolluted');
  assert.equal(config.recorderUrl, undefined);
});

test('allowed recording options still apply', () => {
  const config = configFor(withConfig({ sampleRate: 0, minDurationMs: 4000 }));
  assert.equal(config.sampleRate, 0);
  assert.equal(config.minDurationMs, 4000);
});

test('consent options are tunable for demos', () => {
  const config = configFor(withConfig({ requireConsent: true }));
  assert.equal(config.requireConsent, true);
});

test("the page's own default triggers survive", () => {
  const config = configFor(withConfig({ sampleRate: 1 }));
  assert.equal(config.triggers.urls.length, 1);
  assert.equal(config.triggers.urls[0], '#record$');
  assert.equal(config.triggers.onException, true);
});

test('malformed JSON falls back to defaults instead of throwing', () => {
  const config = configFor('?config=%7Bnot-json');
  assert.equal(config.triggers.onException, true);
});
