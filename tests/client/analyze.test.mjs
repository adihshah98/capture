import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activeMs,
  activeSpans,
  clicksOf,
  collectEvents,
  idleWindows,
  pickFrameTimes,
  scrollTimeline,
  viewportOf,
} from '../../src/analyze/extract.mjs';

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

const scrollAt = (y, timestamp) => scroll(y, timestamp);
const snapshot = (timestamp) => ({ type: 2, data: { initialOffset: { top: 0 } }, timestamp });
const click = (timestamp) => ({
  type: 5,
  data: { tag: 'capture-click', payload: { selector: 'button#cta', x: 1, y: 1 } },
  timestamp,
});

test('activeSpans splits on gaps longer than the activity threshold', () => {
  const spans = activeSpans([
    snapshot(0),
    scrollAt(10, 1000),
    scrollAt(20, 2000),
    // 60s of nothing
    scrollAt(30, 62000),
    scrollAt(40, 63000),
  ]);
  assert.equal(spans.length, 2);
  assert.deepEqual(spans[0], { start: 0, end: 2000 });
  assert.deepEqual(spans[1], { start: 62000, end: 63000 });
});

test('activeSpans needs no idle markers to find dead air', () => {
  const events = [snapshot(0), scrollAt(10, 3000), scrollAt(20, 400000)];
  assert.equal(idleWindows(events).length, 0);
  const spans = activeSpans(events);
  assert.equal(spans.length, 2);
  assert.equal(spans[0].end, 3000);
  assert.equal(spans[1].start, 400000);
});

test('pickFrameTimes samples about once per second of activity', () => {
  const events = [snapshot(0)];
  for (let t = 1000; t <= 6000; t += 1000) events.push(scrollAt(t / 10, t));
  const times = pickFrameTimes(events);
  // ~6s of activity, so ~6 frames rather than one per event or one per ms
  assert.ok(times.length >= 5 && times.length <= 8, `got ${times.length}`);
});

test('pickFrameTimes skips dead air', () => {
  const events = [snapshot(0), scrollAt(10, 5000), scrollAt(20, 500000), click(500500)];
  const times = pickFrameTimes(events);
  assert.equal(times.filter((t) => t > 20000 && t < 480000).length, 0);
  assert.ok(times.some((t) => t >= 480000));
});

test('pickFrameTimes returns ascending times inside the recording', () => {
  const events = [snapshot(0), scrollAt(10, 4000), click(9000), scrollAt(20, 12000)];
  const times = pickFrameTimes(events);
  const duration = events[events.length - 1].timestamp - events[0].timestamp;
  assert.ok(times.every((t, i) => i === 0 || t > times[i - 1]));
  assert.ok(times.every((t) => t >= 0 && t <= duration));
});

test('pickFrameTimes is deterministic', () => {
  const events = [snapshot(0), scrollAt(10, 3000), click(7000), scrollAt(20, 11000)];
  assert.deepEqual(pickFrameTimes(events), pickFrameTimes(events));
});

test('pickFrameTimes honours a frame cap', () => {
  const events = [snapshot(0)];
  for (let t = 1000; t <= 60000; t += 1000) events.push(scrollAt(t / 100, t));
  const capped = pickFrameTimes(events, { maxFrames: 10 });
  assert.ok(capped.length <= 10, `got ${capped.length}`);
  assert.ok(capped.every((t, i) => i === 0 || t > capped[i - 1]));
});

test('pickFrameTimes survives a session too short to sample', () => {
  const times = pickFrameTimes([snapshot(0), scrollAt(1, 100)]);
  assert.ok(times.length >= 1);
  assert.ok(times.every((t) => Number.isFinite(t)));
});

// A long, continuously-active recording used to sample a frame per second with
// no ceiling — every frame a full screenshot sent to the model.
const busyEvents = () => {
  const events = [snapshot(0)];
  for (let t = 100; t <= 200000; t += 100) events.push(scrollAt(t / 100, t));
  return events;
};

test('pickFrameTimes clamps an absurd fps instead of multiplying frames', () => {
  const events = busyEvents();
  // 50fps would be a frame every 20ms — ~10k frames for this recording.
  const wild = pickFrameTimes(events, { fps: 50 });
  const fastest = pickFrameTimes(events, { fps: 4 });
  assert.deepEqual(wild, fastest, 'fps above the band collapses onto the band');
  assert.ok(wild.length < 900, `got ${wild.length}`);
});

test('pickFrameTimes clamps an fps below the floor', () => {
  const events = busyEvents();
  const slowest = pickFrameTimes(events, { fps: 0.2 });
  assert.deepEqual(pickFrameTimes(events, { fps: 0.001 }), slowest);
});

test('pickFrameTimes treats a missing or invalid fps as 1', () => {
  const events = busyEvents();
  const oneFps = pickFrameTimes(events, { fps: 1 });
  // 0 is the "unset" spelling; NaN comes from Number(undefined) at the CLI edge.
  assert.deepEqual(pickFrameTimes(events, { fps: 0 }), oneFps);
  assert.deepEqual(pickFrameTimes(events, { fps: NaN }), oneFps);
  assert.deepEqual(pickFrameTimes(events, { fps: -5 }), oneFps);
});

test('pickFrameTimes honours the default frame cap used in production', () => {
  // Mirrors ANALYZE_MAX_FRAMES=90 in analyze.py: the cap is what keeps a
  // continuously-active session from growing without bound.
  const capped = pickFrameTimes(busyEvents(), { maxFrames: 90 });
  assert.ok(capped.length <= 90, `got ${capped.length}`);
  assert.ok(capped.every((t, i) => i === 0 || t > capped[i - 1]));
});
