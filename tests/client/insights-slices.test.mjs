import assert from 'node:assert/strict';
import test from 'node:test';

// None of this needed a DOM, but all of it lived in a 1,756-line module that
// grabbed 50 elements at import, so none of it could be reached from a test.
const {
  SLICE_FIELDS,
  emptySlice,
  sliceIsEmpty,
  rowMatches,
  formatOutcome,
  sliceNameBits,
  suggestCohortName,
} = await import('../../src/replay/insights/slices.js');

test('an empty slice has every field and matches everything', () => {
  const spec = emptySlice();
  assert.deepEqual(Object.keys(spec).sort(), [...SLICE_FIELDS].sort());
  assert.ok(SLICE_FIELDS.every((f) => spec[f] === ''));
  assert.equal(sliceIsEmpty(spec), true);
  assert.equal(rowMatches({ outcome: 'purchased' }, spec), true);
});

test('one set field is enough to stop it being empty', () => {
  assert.equal(sliceIsEmpty({ ...emptySlice(), device: 'mobile' }), false);
});

test('outcome defaults to rest when the row has none', () => {
  const spec = { ...emptySlice(), outcome: 'rest' };
  assert.equal(rowMatches({}, spec), true);
  assert.equal(rowMatches({ outcome: 'purchased' }, spec), false);
});

test('a missing device never matches a device filter', () => {
  // Matches the server: device_class() returns null for an unindexed viewport,
  // and an unknown device must not fall into a device-filtered cohort.
  const spec = { ...emptySlice(), device: 'mobile' };
  assert.equal(rowMatches({ device: 'mobile' }, spec), true);
  assert.equal(rowMatches({}, spec), false);
  assert.equal(rowMatches({ device: null }, spec), false);
});

test('utm_source=meta matches known Meta sources and fbclid, like the server', () => {
  const spec = { ...emptySlice(), utm_source: 'meta' };
  assert.equal(rowMatches({ attribution: { utm_source: '  Meta ' } }, spec), true);
  assert.equal(rowMatches({ attribution: { utm_source: 'facebook' } }, spec), true);
  assert.equal(rowMatches({ attribution: { fbclid: true } }, spec), true);
  assert.equal(rowMatches({ attribution: { utm_source: 'google' } }, spec), false);
  assert.equal(rowMatches({ attribution: null }, spec), false);
});

test('every field in a slice must match, not any', () => {
  const spec = { ...emptySlice(), outcome: 'purchased', device: 'mobile' };
  assert.equal(rowMatches({ outcome: 'purchased', device: 'mobile' }, spec), true);
  assert.equal(rowMatches({ outcome: 'purchased', device: 'desktop' }, spec), false);
});

test('outcome labels match the server', () => {
  assert.equal(formatOutcome('purchased'), 'purchased');
  assert.equal(formatOutcome('checkout'), 'reached checkout');
  assert.equal(formatOutcome('rest'), 'did not reach checkout');
  assert.equal(formatOutcome('all'), 'did not purchase');
});

test('a name is suggested from the same tokens the server uses', () => {
  assert.deepEqual(
    sliceNameBits({ ...emptySlice(), device: 'mobile', outcome: 'purchased' }),
    ['purchased', 'mobile']
  );
  assert.deepEqual(sliceNameBits({ ...emptySlice(), outcome: 'all' }), ['did not purchase']);
  assert.deepEqual(sliceNameBits({ ...emptySlice(), utm_source: 'meta' }), ['Meta']);
  assert.deepEqual(sliceNameBits({ ...emptySlice(), utm_campaign: 'spring' }), [
    'campaign=spring',
  ]);
  const name = suggestCohortName({ sliceA: { ...emptySlice(), device: 'mobile' } });
  assert.equal(typeof name, 'string');
  assert.match(name, /mobile/);
});

test('an unfiltered cohort still gets a name', () => {
  assert.equal(typeof suggestCohortName({ sliceA: emptySlice() }), 'string');
});
