import test from 'node:test';
import assert from 'node:assert/strict';

const { gapEntries } = await import('../src/replay/gaps.js');

const T0 = 1_700_000_000_000;
const min = (n) => T0 + n * 60_000;
const sec = (n) => T0 + n * 1000;

const marker = (tag, t, payload = {}) => ({ type: 5, data: { tag, payload }, timestamp: t });
const seen = (state, t) => marker('capture-visibility', t, { state });

const details = (markers, end) => gapEntries(markers, end).map((e) => `${e.kind}: ${e.detail}`);

test('an untracked recording states the gap without claiming presence', () => {
  assert.deepEqual(details([marker('capture-idle', min(5)), marker('capture-idle-end', min(11))], min(12)), [
    'idle: no interaction for 6m 0s',
  ]);
});

test('a gap with the tab in the foreground is hesitation, not absence', () => {
  const markers = [
    seen('focused', T0),
    marker('capture-idle', min(5)),
    marker('capture-idle-end', min(11)),
  ];
  assert.deepEqual(details(markers, min(12)), ['idle: on the page, no interaction for 6m 0s']);
});

test('a gap spent in another tab is reported as away, with how much of it', () => {
  const markers = [
    seen('focused', T0),
    marker('capture-idle', min(5)),
    seen('hidden', min(6)),
    seen('focused', min(11)),
    marker('capture-idle-end', min(11)),
  ];
  assert.deepEqual(details(markers, min(12)), ['away: left the tab for 5m 0s of 6m 0s']);
});

test('a gap spent in another window is reported separately from a tab switch', () => {
  const markers = [
    seen('focused', T0),
    marker('capture-idle', min(5)),
    seen('unfocused', min(5)),
    seen('focused', min(11)),
    marker('capture-idle-end', min(11)),
  ];
  assert.deepEqual(details(markers, min(12)), ['unfocused: switched windows for 6m 0s of 6m 0s']);
});

test('a brief tab switch inside a long gap still counts as hesitation', () => {
  const markers = [
    seen('focused', T0),
    marker('capture-idle', min(5)),
    seen('hidden', min(6)),
    seen('focused', min(7)),
    marker('capture-idle-end', min(11)),
  ];
  assert.deepEqual(details(markers, min(12)), [
    'idle: on the page, no interaction for 6m 0s',
    'away: left the tab for 1m 0s',
  ]);
});

test('leaving mid-gap and never returning is not passed off as a duration', () => {
  const markers = [seen('focused', T0), marker('capture-idle', min(5)), seen('hidden', min(6))];
  assert.deepEqual(details(markers, min(20)), ['away: left the tab and did not return']);
});

test('an away stretch outside any idle gap gets its own row', () => {
  const markers = [seen('focused', T0), seen('hidden', sec(30)), seen('focused', sec(75))];
  assert.deepEqual(details(markers, sec(120)), ['away: left the tab for 45s']);
});

test('the hide that ends a recording is marked as the exit, not as a gap', () => {
  const markers = [seen('focused', T0), seen('hidden', sec(120))];
  assert.deepEqual(details(markers, sec(120)), ['away: left the tab — recording ends here']);
});

test('sub-3s flickers are ignored', () => {
  const markers = [seen('focused', T0), seen('unfocused', sec(30)), seen('focused', sec(32))];
  assert.deepEqual(details(markers, sec(120)), []);
});

test('an idle gap left open by a lost tail runs to the last event', () => {
  const markers = [seen('focused', T0), marker('capture-idle', min(5))];
  assert.deepEqual(details(markers, min(9)), ['idle: on the page, no interaction for 4m 0s']);
});

test('entries carry the gap start so the timeline can seek to it', () => {
  const markers = [
    seen('focused', T0),
    marker('capture-idle', min(5)),
    marker('capture-idle-end', min(11)),
  ];
  assert.deepEqual(
    gapEntries(markers, min(12)).map((e) => e.t),
    [min(5)]
  );
});
