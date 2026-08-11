import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const DOC = fs.readFileSync(new URL('../../docs/integration.md', import.meta.url), 'utf8');
const CODE = DOC.match(/```html\n<script>([\s\S]*?)<\/script>/)[1];
const HOST = 'https://capture.example.com';

function run({ readyState = 'loading', existingConfig, idleSupported = true, times = 1 } = {}) {
  const appended = [];
  const listeners = {};
  let pending = null;
  const sandbox = {
    Object,
    document: {
      readyState,
      head: { appendChild: (el) => appended.push(el) },
      createElement: () => ({}),
    },
    addEventListener: (type, fn) => {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    setTimeout: (fn) => {
      pending = fn;
    },
  };
  if (idleSupported) sandbox.requestIdleCallback = (fn) => (pending = fn);
  sandbox.window = sandbox;
  if (existingConfig) sandbox.window.CAPTURE_CONFIG = existingConfig;
  vm.createContext(sandbox);
  for (let i = 0; i < times; i++) vm.runInContext(CODE, sandbox);
  return {
    appended,
    sandbox,
    fireLoad: () => (listeners.load || []).forEach((fn) => fn()),
    runIdle: () => pending && pending(),
  };
}

test('nothing is fetched while the page is still loading', () => {
  const { appended } = run({ readyState: 'loading' });
  assert.deepEqual(appended, []);
});

test('nothing is fetched even once load fires, until the browser is idle', () => {
  const page = run({ readyState: 'loading' });
  page.fireLoad();
  assert.deepEqual(page.appended, []);
});

test('the recorder is loaded async from the capture origin after idle', () => {
  const page = run({ readyState: 'loading' });
  page.fireLoad();
  page.runIdle();
  assert.equal(page.appended.length, 1);
  assert.equal(page.appended[0].src, `${HOST}/recorder.bundle.js`);
  assert.ok(page.appended[0].async, 'must not be a blocking script');
});

test('a page that has already loaded schedules immediately', () => {
  const page = run({ readyState: 'complete' });
  page.runIdle();
  assert.equal(page.appended.length, 1);
});

test('browsers without requestIdleCallback still load the recorder', () => {
  const page = run({ readyState: 'complete', idleSupported: false });
  page.runIdle();
  assert.equal(page.appended.length, 1);
});

test('the endpoints and token are absolute so any origin can host the page', () => {
  const { sandbox } = run({});
  assert.equal(sandbox.window.CAPTURE_CONFIG.endpoint, `${HOST}/ingest`);
  assert.equal(sandbox.window.CAPTURE_CONFIG.configUrl, `${HOST}/config`);
  assert.equal(sandbox.window.CAPTURE_CONFIG.token, 'cap_YOURTOKEN');
});

test('a config the page set beforehand survives and wins', () => {
  const { sandbox } = run({ existingConfig: { requireConsent: true, token: 'cap_theirs' } });
  assert.equal(sandbox.window.CAPTURE_CONFIG.requireConsent, true);
  assert.equal(sandbox.window.CAPTURE_CONFIG.token, 'cap_theirs');
  assert.equal(sandbox.window.CAPTURE_CONFIG.endpoint, `${HOST}/ingest`);
});

test('pasting the snippet twice still loads the recorder once', () => {
  const page = run({ readyState: 'complete', times: 2 });
  page.runIdle();
  assert.equal(page.appended.length, 1);
});

test('a failed load releases the guard so a later attempt can retry', () => {
  const page = run({ readyState: 'complete' });
  page.runIdle();
  assert.equal(page.sandbox.window.__captureLoaded, 1);
  page.appended[0].onerror();
  assert.equal(page.sandbox.window.__captureLoaded, 0);
});

test('the documented snippet never references capture.js', () => {
  assert.ok(
    !CODE.includes('capture.js'),
    'the whole point is that no loader file sits in the critical path'
  );
});
