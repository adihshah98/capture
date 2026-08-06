import test from 'node:test';
import assert from 'node:assert/strict';

const { beaconEvents, BEACON_SPLIT_FLOOR_BYTES } = await import('../src/recorder/beacon.js');

const event = (bytes) => ({ pad: 'x'.repeat(bytes) });
const encode = (events) => JSON.stringify(events);

function harness({ accept }) {
  const sent = [];
  const fallbacks = [];
  return {
    sent,
    fallbacks,
    send: (json) => (accept(json, sent.length) ? (sent.push(json), true) : false),
    fallback: (json) => fallbacks.push(json),
  };
}

const eventsIn = (jsons) => jsons.reduce((n, j) => n + JSON.parse(j).length, 0);

test('a small batch goes out as one beacon', () => {
  const h = harness({ accept: () => true });
  const ok = beaconEvents([event(100), event(100)], encode, h.send, h.fallback);
  assert.equal(ok, true);
  assert.equal(h.sent.length, 1);
  assert.equal(h.fallbacks.length, 0);
});

test('an oversized batch splits until every piece is accepted', () => {
  const LIMIT = 40 * 1024;
  const h = harness({ accept: (json) => json.length <= LIMIT });
  const events = Array.from({ length: 8 }, () => event(10 * 1024));
  const ok = beaconEvents(events, encode, h.send, h.fallback);
  assert.equal(ok, true);
  assert.equal(h.fallbacks.length, 0);
  assert.ok(h.sent.length >= 2);
  assert.equal(eventsIn(h.sent), 8, 'no event may be lost in the split');
  for (const json of h.sent) assert.ok(json.length <= LIMIT);
});

test('exhausted quota falls back to fetch for the remainder', () => {
  const h = harness({ accept: (json, alreadySent) => alreadySent < 1 && json.length <= 40 * 1024 });
  const events = Array.from({ length: 8 }, () => event(10 * 1024));
  const ok = beaconEvents(events, encode, h.send, h.fallback);
  assert.equal(ok, false);
  assert.ok(h.sent.length >= 1);
  assert.ok(h.fallbacks.length >= 1);
  assert.equal(eventsIn(h.sent) + eventsIn(h.fallbacks), 8, 'every event ships one way or the other');
});

test('a single unbeaconable event goes straight to fallback', () => {
  const h = harness({ accept: () => false });
  const ok = beaconEvents([event(100 * 1024)], encode, h.send, h.fallback);
  assert.equal(ok, false);
  assert.equal(h.sent.length, 0);
  assert.equal(h.fallbacks.length, 1);
});

test('small rejected batches are not split below the floor', () => {
  const h = harness({ accept: () => false });
  const events = [event(1024), event(1024)];
  assert.ok(encode(events).length < BEACON_SPLIT_FLOOR_BYTES);
  beaconEvents(events, encode, h.send, h.fallback);
  assert.equal(h.fallbacks.length, 1, 'one fallback for the whole batch, no split churn');
});
