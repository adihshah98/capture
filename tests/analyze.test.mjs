import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activeMs,
  clicksOf,
  collectEvents,
  dwellBySection,
  idleWindows,
  scrollTimeline,
  viewportOf,
} from '../src/analyze/extract.mjs';

const marker = (tag, timestamp) => ({ type: 5, data: { tag, payload: {} }, timestamp });
const scroll = (y, timestamp) => ({ type: 3, data: { source: 3, id: 1, x: 0, y }, timestamp });

test('collectEvents merges chunks in timestamp order', () => {
  const events = collectEvents([
    { events: [{ timestamp: 30 }, { timestamp: 10 }] },
    { events: [{ timestamp: 20 }] },
  ]);
  assert.deepEqual(events.map((e) => e.timestamp), [10, 20, 30]);
});

test('idleWindows pairs idle markers and leaves an unclosed one open', () => {
  const windows = idleWindows([
    marker('capture-idle', 100),
    marker('capture-idle-end', 400),
    marker('capture-idle', 900),
  ]);
  assert.deepEqual(windows, [[100, 400], [900, Infinity]]);
});

test('activeMs subtracts overlapping idle time only', () => {
  assert.equal(activeMs(0, 1000, []), 1000);
  assert.equal(activeMs(0, 1000, [[200, 400]]), 800);
  // idle entirely outside the span changes nothing
  assert.equal(activeMs(0, 1000, [[2000, 3000]]), 1000);
  // idle partially overlapping clips to the span
  assert.equal(activeMs(500, 1000, [[0, 700]]), 300);
});

test('activeMs never returns negative time when idle covers the span', () => {
  assert.equal(activeMs(100, 200, [[0, 5000]]), 0);
});

test('viewportOf prefers the latest resize over the initial meta', () => {
  assert.deepEqual(
    viewportOf([
      { type: 4, data: { width: 800, height: 600 }, timestamp: 1 },
      { type: 3, data: { source: 4, width: 375, height: 812 }, timestamp: 2 },
    ]),
    { width: 375, height: 812 }
  );
});

test('scrollTimeline includes the full-snapshot initial offset', () => {
  const points = scrollTimeline([
    { type: 2, data: { initialOffset: { top: 40, left: 0 } }, timestamp: 5 },
    scroll(120, 10),
  ]);
  assert.deepEqual(points, [{ t: 5, y: 40 }, { t: 10, y: 120 }]);
});

test('clicksOf carries image identity and the rage flag', () => {
  const clicks = clicksOf([
    {
      type: 5,
      timestamp: 50,
      data: {
        tag: 'capture-click',
        payload: {
          selector: 'div > img',
          text: '',
          tag: 'img',
          x: 10,
          y: 20,
          image: { src: 'https://cdn/tubes.webp', alt: 'three tubes' },
          rage: true,
        },
      },
    },
    marker('capture-idle', 60),
  ]);
  assert.equal(clicks.length, 1);
  assert.equal(clicks[0].image.alt, 'three tubes');
  assert.equal(clicks[0].rage, true);
});

test('dwellBySection credits every section visible in the viewport', () => {
  const sections = [
    { id: 's0', label: 'Hero', top: 0, bottom: 500 },
    { id: 's1', label: 'Benefits', top: 500, bottom: 1500 },
  ];
  const dwell = dwellBySection(
    [{ t: 0, y: 0 }, { t: 10000, y: 600 }],
    sections,
    { width: 800, height: 400 },
    [],
    12000
  );
  const byLabel = Object.fromEntries(dwell.map((d) => [d.label, d.dwellMs]));
  // y=0..400 shows only the hero for 10s; y=600..1000 shows only benefits for 2s
  assert.equal(byLabel.Hero, 10000);
  assert.equal(byLabel.Benefits, 2000);
});

test('dwellBySection excludes idle time from the winning section', () => {
  const sections = [{ id: 's0', label: 'Hero', top: 0, bottom: 500 }];
  const dwell = dwellBySection(
    [{ t: 0, y: 0 }],
    sections,
    { width: 800, height: 400 },
    [[2000, 8000]],
    10000
  );
  assert.equal(dwell[0].dwellMs, 4000);
});

test('dwellBySection returns nothing without measured geometry', () => {
  assert.deepEqual(dwellBySection([{ t: 0, y: 0 }], [], { width: 800, height: 400 }, [], 100), []);
});
