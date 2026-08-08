import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEPTH_BANDS,
  depthPct,
  dwellByBand,
  maxDepthPct,
  pageViews,
  scrollableRange,
  sessionFacts,
} from '../src/analyze/facts.mjs';

const VIEWPORT = { width: 400, height: 800 };
// 1800px of scrollable range, so 900px of offset is exactly halfway.
const GEOMETRY = { pageHeight: 2600, viewport: VIEWPORT, t0: 0, endMs: 10000 };

test('scrollableRange excludes the first screenful', () => {
  assert.equal(scrollableRange(2600, VIEWPORT), 1800);
  // A page shorter than the viewport cannot be scrolled at all.
  assert.equal(scrollableRange(500, VIEWPORT), 0);
});

test('depthPct measures against scrollable distance, not raw height', () => {
  assert.equal(depthPct(0, 2600, VIEWPORT), 0);
  assert.equal(depthPct(900, 2600, VIEWPORT), 50);
  assert.equal(depthPct(1800, 2600, VIEWPORT), 100);
});

test('depthPct clamps past-the-end offsets from overscroll', () => {
  assert.equal(depthPct(9999, 2600, VIEWPORT), 100);
  assert.equal(depthPct(-50, 2600, VIEWPORT), 0);
});

test('depthPct treats an unscrollable page as fully seen', () => {
  // Nothing to scroll means the whole page was on screen at rest; reporting 0%
  // would read as "never looked at it".
  assert.equal(depthPct(0, 500, VIEWPORT), 100);
});

test('dwellByBand attributes active time to the band held at the time', () => {
  const points = [
    { t: 0, y: 0 }, // 0%   -> band 0-25
    { t: 2000, y: 900 }, // 50%  -> band 50-75
    { t: 5000, y: 1800 }, // 100% -> band 75-100
  ];
  const spans = [{ start: 0, end: 10000 }];
  const dwell = dwellByBand(points, spans, GEOMETRY);

  assert.equal(dwell['0-25'], 2000);
  assert.equal(dwell['50-75'], 3000);
  assert.equal(dwell['75-100'], 5000);
  assert.equal(dwell['25-50'], 0);
});

test('dwellByBand counts only active time, not wall clock', () => {
  const points = [{ t: 0, y: 0 }];
  // Ten seconds parked at the top, but only the first two were active.
  const dwell = dwellByBand(points, [{ start: 0, end: 2000 }], GEOMETRY);
  assert.equal(dwell['0-25'], 2000);
});

test('dwellByBand splits one stretch across disjoint active spans', () => {
  const points = [{ t: 0, y: 0 }];
  const spans = [
    { start: 0, end: 1000 },
    { start: 6000, end: 6500 },
  ];
  assert.equal(dwellByBand(points, spans, GEOMETRY)['0-25'], 1500);
});

test('dwellByBand returns every band at zero when nothing was recorded', () => {
  const dwell = dwellByBand([], [], GEOMETRY);
  assert.deepEqual(Object.keys(dwell), DEPTH_BANDS.map(([lo, hi]) => `${lo}-${hi}`));
  assert.ok(Object.values(dwell).every((v) => v === 0));
});

test('dwellByBand tolerates unsorted scroll points', () => {
  const points = [
    { t: 5000, y: 1800 },
    { t: 0, y: 0 },
  ];
  const dwell = dwellByBand(points, [{ start: 0, end: 10000 }], GEOMETRY);
  assert.equal(dwell['0-25'], 5000);
  assert.equal(dwell['75-100'], 5000);
});

test('band edges fall in the upper band, and 100% is included', () => {
  // 450px of 1800 is exactly 25%, the boundary between the first two bands.
  const atEdge = dwellByBand([{ t: 0, y: 450 }], [{ start: 0, end: 1000 }], GEOMETRY);
  assert.equal(atEdge['25-50'], 1000);
  assert.equal(atEdge['0-25'], 0);

  const atEnd = dwellByBand([{ t: 0, y: 1800 }], [{ start: 0, end: 1000 }], GEOMETRY);
  assert.equal(atEnd['75-100'], 1000);
});

test('maxDepthPct reports the deepest point even with no dwell there', () => {
  const points = [
    { t: 0, y: 0 },
    { t: 100, y: 1800 }, // bottom, immediately left again
    { t: 200, y: 0 },
  ];
  assert.equal(maxDepthPct(points, GEOMETRY), 100);
  assert.equal(maxDepthPct([], GEOMETRY), 0);
});

test('pageViews dedupes the repeats chunk boundaries produce', () => {
  assert.deepEqual(pageViews(['/a', '/a', '/b', '/a']), ['/a', '/b']);
  assert.deepEqual(pageViews([{ url: '/a' }, { url: '/b' }]), ['/a', '/b']);
  assert.deepEqual(pageViews(null), []);
});

test('sessionFacts summarises a digest into flat scalars', () => {
  const facts = sessionFacts({
    session: {
      durationMs: 60000,
      activeMs: 45000,
      idleMs: 15000,
      viewport: VIEWPORT,
      pageHeight: 2600,
      maxDepthPct: 82,
      dwellByBand: { '0-25': 1000 },
      urls: ['/p/item', '/p/item', '/cart'],
    },
    clicks: [{ rage: false }, { rage: true }, { rage: true }],
  });

  assert.equal(facts.duration_s, 60);
  assert.equal(facts.active_s, 45);
  assert.equal(facts.active_pct, 75);
  assert.equal(facts.max_depth_pct, 82);
  assert.equal(facts.click_count, 3);
  assert.equal(facts.rage_click_count, 2);
  assert.equal(facts.page_count, 2);
  assert.equal(facts.has_geometry, true);
  assert.equal(facts.schema, 1);
});

test('sessionFacts reports active_pct as null for a zero-length session', () => {
  // Distinguishable from 0%: one is "no engagement", the other "nothing
  // recorded", and averaging them together would understate engagement.
  const facts = sessionFacts({ session: { durationMs: 0, activeMs: 0 }, clicks: [] });
  assert.equal(facts.active_pct, null);
  assert.equal(facts.has_geometry, false);
});

test('sessionFacts survives a digest with nothing in it', () => {
  const facts = sessionFacts({});
  assert.equal(facts.click_count, 0);
  assert.equal(facts.page_count, 0);
  assert.equal(facts.has_geometry, false);
});
