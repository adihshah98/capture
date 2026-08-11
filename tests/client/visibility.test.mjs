import test from 'node:test';
import assert from 'node:assert/strict';

function fakeTarget() {
  const listeners = [];
  return {
    addEventListener: (type, fn) => listeners.push({ type, fn }),
    removeEventListener: (type, fn) => {
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    },
    fire: (type) => listeners.filter((l) => l.type === type).forEach((l) => l.fn()),
    listenerCount: () => listeners.length,
  };
}

const win = fakeTarget();
const doc = fakeTarget();
doc.visibilityState = 'visible';
doc.focused = true;
doc.hasFocus = () => doc.focused;

globalThis.addEventListener = win.addEventListener;
globalThis.removeEventListener = win.removeEventListener;
globalThis.document = doc;

const { startVisibilityCapture } = await import('../../src/recorder/visibility.js');

const settle = () => new Promise((r) => setTimeout(r, 200));

function start() {
  const emitted = [];
  const stop = startVisibilityCapture((state, at) => emitted.push({ state, at }));
  return { emitted, states: () => emitted.map((e) => e.state), stop };
}

function startVisible() {
  doc.visibilityState = 'visible';
  doc.focused = true;
  return start();
}

const blur = () => {
  doc.focused = false;
  win.fire('blur');
};
const hide = () => {
  doc.visibilityState = 'hidden';
  doc.focused = false;
  doc.fire('visibilitychange');
};
const show = () => {
  doc.visibilityState = 'visible';
  doc.fire('visibilitychange');
};
const focus = () => {
  doc.focused = true;
  win.fire('focus');
};

test('the starting state is recorded so replays know tracking was on', () => {
  const { states, stop } = startVisible();
  stop();
  assert.deepEqual(states(), ['focused']);
});

test('a page that loads in a background tab starts hidden', () => {
  doc.visibilityState = 'hidden';
  doc.focused = false;
  const { states, stop } = start();
  stop();
  assert.deepEqual(states(), ['hidden']);
});

test('a tab switch emits hidden once, not a blur blip first', async () => {
  const { states, stop } = startVisible();
  blur();
  hide();
  await settle();
  stop();
  assert.deepEqual(states(), ['focused', 'hidden']);
});

test('coming back emits focused once', async () => {
  const { states, stop } = startVisible();
  blur();
  hide();
  await settle();
  show();
  focus();
  await settle();
  stop();
  assert.deepEqual(states(), ['focused', 'hidden', 'focused']);
});

test('losing focus with the page still visible reports unfocused', async () => {
  const { states, stop } = startVisible();
  blur();
  await settle();
  focus();
  await settle();
  stop();
  assert.deepEqual(states(), ['focused', 'unfocused', 'focused']);
});

test('a hide is timestamped when it happened, not when it settles', async () => {
  const { emitted, stop } = startVisible();
  const at = Date.now();
  hide();
  await settle();
  stop();
  assert.ok(Math.abs(emitted[1].at - at) < 50);
});

test('repeated events for an unchanged state stay silent', async () => {
  const { emitted, stop } = startVisible();
  win.fire('focus');
  doc.fire('visibilitychange');
  await settle();
  stop();
  assert.equal(emitted.length, 1);
});

test('stop removes every listener', () => {
  const { stop } = startVisible();
  assert.equal(win.listenerCount(), 2);
  assert.equal(doc.listenerCount(), 1);
  stop();
  assert.equal(win.listenerCount(), 0);
  assert.equal(doc.listenerCount(), 0);
});
