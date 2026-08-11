import test from 'node:test';
import assert from 'node:assert/strict';

const { parseFrames, splitSettled } = await import('../../src/replay/chat-stream.js');

test('whole frames are parsed and the partial tail is returned', () => {
  const [frames, rest] = parseFrames(
    'event: meta\ndata: {"n":12}\n\nevent: delta\ndata: {"text":"hi"}\n\nevent: del'
  );
  assert.deepEqual(frames, [
    { event: 'meta', data: { n: 12 } },
    { event: 'delta', data: { text: 'hi' } },
  ]);
  // The caller feeds this back with the next chunk.
  assert.equal(rest, 'event: del');
});

test('a frame split across chunks arrives once both halves are in', () => {
  let [frames, buffer] = parseFrames('event: delta\ndata: {"te');
  assert.deepEqual(frames, []);
  [frames, buffer] = parseFrames(buffer + 'xt":"done"}\n\n');
  assert.deepEqual(frames, [{ event: 'delta', data: { text: 'done' } }]);
  assert.equal(buffer, '');
});

test('unreadable frames are skipped rather than half-rendered', () => {
  const [frames] = parseFrames(
    'event: delta\ndata: {not json}\n\ndata: {"orphan":1}\n\nevent: delta\ndata: {"text":"ok"}\n\n'
  );
  // The bad JSON and the event-less frame are both dropped; the good one lands.
  assert.deepEqual(frames, [{ event: 'delta', data: { text: 'ok' } }]);
});

test('an event with no data line still fires', () => {
  const [frames] = parseFrames('event: done\ndata: {}\n\n');
  assert.deepEqual(frames, [{ event: 'done', data: {} }]);
});

test('text with no citation in flight is entirely settled', () => {
  assert.deepEqual(splitSettled('stalled on the guarantee.'), [
    'stalled on the guarantee.',
    '',
  ]);
  assert.deepEqual(splitSettled(''), ['', '']);
});

test('a citation that could still grow is held back', () => {
  // "S3" may yet become "S3 [00:41]" — linking it now would point at 0:00.
  assert.deepEqual(splitSettled('8 of 12 (S3'), ['8 of 12 (', 'S3']);
  assert.deepEqual(splitSettled('8 of 12 (S3 [00:'), ['8 of 12 (', 'S3 [00:']);
  assert.deepEqual(splitSettled('a range S4 at [00:05-'), ['a range ', 'S4 at [00:05-']);
});

test('a finished citation is settled and safe to linkify', () => {
  assert.deepEqual(splitSettled('8 of 12 (S3 [00:41]'), ['8 of 12 (S3 [00:41]', '']);
  assert.deepEqual(splitSettled('S4 at [00:05-00:12]'), ['S4 at [00:05-00:12]', '']);
});

test('a bare ordinal list settles once the clause moves on', () => {
  // Mid-token: the trailing S2 could still take a stamp.
  assert.deepEqual(splitSettled('4 of 7 kept it (S1, S2'), ['4 of 7 kept it (S1, ', 'S2']);
  // Once punctuation follows, nothing can grow into a citation.
  assert.deepEqual(splitSettled('4 of 7 kept it (S1, S2).'), [
    '4 of 7 kept it (S1, S2).',
    '',
  ]);
});

test('the whole stream reassembles to the text that was sent', () => {
  // Rendering settled+pending must never drop or duplicate a character.
  const answer = 'Split on price: S3 [01:10] and S5 left; S1, S2 bought.';
  for (let i = 0; i <= answer.length; i++) {
    const [settled, pending] = splitSettled(answer.slice(0, i));
    assert.equal(settled + pending, answer.slice(0, i));
  }
});
