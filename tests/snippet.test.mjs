// Runs against public/capture.js — the artifact that is actually served.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const BUILT = new URL('../public/capture.js', import.meta.url);
const CODE = fs.readFileSync(BUILT, 'utf8');
const CAPTURE_ORIGIN = 'https://capture.example.com';

function run({ pageOrigin, snippetSrc = `${CAPTURE_ORIGIN}/capture.js`, pageConfig }) {
  const appended = [];
  const errors = [];
  const currentScript = { src: snippetSrc, getAttribute: () => 'cap_test' };
  const sandbox = {
    console: { error: (...a) => errors.push(a.join(' ')), warn: () => {} },
    URL,
    setTimeout: (fn) => fn(), // the snippet's non-idle path, run synchronously
    addEventListener: () => {},
    location: { origin: pageOrigin },
    document: {
      currentScript,
      baseURI: `${pageOrigin}/page.html`,
      readyState: 'complete',
      head: { appendChild: (el) => appended.push(el.src) },
      createElement: () => ({}),
    },
  };
  sandbox.window = sandbox;
  sandbox.window.CAPTURE_CONFIG = pageConfig;
  vm.createContext(sandbox);
  vm.runInContext(CODE, sandbox);
  return { appended, errors };
}

test('loads the bundle from the capture origin by default', () => {
  const { appended } = run({ pageOrigin: 'https://brand.example' });
  assert.equal(appended.length, 1);
  assert.ok(appended[0].startsWith(CAPTURE_ORIGIN));
});

test('a brand may self-host the bundle on its own origin', () => {
  const { appended } = run({
    pageOrigin: 'https://brand.example',
    pageConfig: { recorderUrl: '/recorder.bundle.js' },
  });
  assert.deepEqual(appended, ['https://brand.example/recorder.bundle.js']);
});

for (const [name, recorderUrl] of [
  ['a third-party absolute URL', 'https://evil.example/x.js'],
  ['a protocol-relative URL', '//evil.example/x.js'],
  ['a javascript: URL', 'javascript:alert(1)'],
]) {
  test(`refuses ${name}`, () => {
    const { appended, errors } = run({ pageOrigin: CAPTURE_ORIGIN, pageConfig: { recorderUrl } });
    assert.deepEqual(appended, [], 'nothing may be appended to the document');
    assert.ok(errors.some((e) => e.includes('capture:')), 'refusal should be reported');
  });
}

test('runs only once per page', () => {
  const { appended } = run({ pageOrigin: 'https://brand.example' });
  assert.equal(appended.length, 1);
});
