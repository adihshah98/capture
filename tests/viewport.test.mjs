import test from 'node:test';
import assert from 'node:assert/strict';

function fakeViewport(props) {
  const listeners = [];
  return Object.assign(
    {
      addEventListener: (type, fn) => listeners.push({ type, fn }),
      removeEventListener: (type, fn) => {
        const i = listeners.findIndex((l) => l.type === type && l.fn === fn);
        if (i >= 0) listeners.splice(i, 1);
      },
      fire: (type) => listeners.filter((l) => l.type === type).forEach((l) => l.fn()),
      listenerCount: () => listeners.length,
    },
    props
  );
}

globalThis.window = globalThis;

const { startViewportCapture } = await import('../src/recorder/viewport.js');

const settle = () => new Promise((r) => setTimeout(r, 200));

test('no visualViewport is a no-op', () => {
  window.visualViewport = undefined;
  const stop = startViewportCapture(() => assert.fail('emitted'));
  stop();
});

test('zooming emits the visual viewport page rect', async () => {
  const vv = fakeViewport({ pageLeft: 0, pageTop: 0, width: 402, height: 768, scale: 1 });
  window.visualViewport = vv;
  const emitted = [];
  const stop = startViewportCapture((p) => emitted.push(p));
  Object.assign(vv, { pageLeft: 100, pageTop: 900, width: 201, height: 384, scale: 2 });
  vv.fire('resize');
  await settle();
  stop();
  assert.deepEqual(emitted, [{ x: 100, y: 900, w: 201, h: 384, scale: 2 }]);
});

test('pans while zoomed emit, sub-4px jitter does not', async () => {
  const vv = fakeViewport({ pageLeft: 100, pageTop: 900, width: 201, height: 384, scale: 2 });
  window.visualViewport = vv;
  const emitted = [];
  const stop = startViewportCapture((p) => emitted.push(p));
  await settle();
  vv.pageTop = 902;
  vv.fire('scroll');
  await settle();
  vv.pageTop = 1200;
  vv.fire('scroll');
  await settle();
  stop();
  assert.deepEqual(
    emitted.map((p) => p.y),
    [900, 1200]
  );
});

test('returning to scale 1 emits once, later unzoomed scrolls stay silent', async () => {
  const vv = fakeViewport({ pageLeft: 100, pageTop: 900, width: 201, height: 384, scale: 2 });
  window.visualViewport = vv;
  const emitted = [];
  const stop = startViewportCapture((p) => emitted.push(p));
  await settle();
  Object.assign(vv, { pageLeft: 0, pageTop: 500, width: 402, height: 768, scale: 1 });
  vv.fire('resize');
  await settle();
  vv.pageTop = 1500;
  vv.fire('scroll');
  await settle();
  stop();
  assert.deepEqual(
    emitted.map((p) => p.scale),
    [2, 1]
  );
});

test('stop removes listeners', () => {
  const vv = fakeViewport({ pageLeft: 0, pageTop: 0, width: 402, height: 768, scale: 1 });
  window.visualViewport = vv;
  const stop = startViewportCapture(() => {});
  assert.equal(vv.listenerCount(), 2);
  stop();
  assert.equal(vv.listenerCount(), 0);
});
