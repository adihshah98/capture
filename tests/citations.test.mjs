import test from 'node:test';
import assert from 'node:assert/strict';

const { citationMs, linkCitations } = await import('../src/replay/citations.js');

// Matches escapeAttr in insights.js closely enough to prove quotes cannot
// break out of the attribute.
const escapeAttr = (s) =>
  String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

const IDS = ['sid-a', 'sid-b', 'sid-c', 'sid-d'];

test('MM:SS and HH:MM:SS become milliseconds', () => {
  assert.equal(citationMs('00:13'), 13000);
  assert.equal(citationMs('01:09'), 69000);
  assert.equal(citationMs('14:23'), 863000);
  assert.equal(citationMs('01:00:00'), 3600000);
});

test('a malformed stamp is not seekable', () => {
  assert.equal(citationMs('bad'), null);
  assert.equal(citationMs('12'), null);
  // 90s should have been written 01:30 — this is a formatting error, not 90s.
  assert.equal(citationMs('01:90'), null);
  assert.equal(citationMs('1:2:3:4'), null);
});

test('a citation links to the session at that offset', () => {
  const out = linkCitations('Stalled here (S2 [01:09]).', IDS, escapeAttr);
  assert.match(out, /data-cite-sid="sid-b"/);
  assert.match(out, /data-cite-t="69000"/);
  // The visible text is untouched, so the sentence still reads the same.
  assert.match(out, />S2 \[01:09\]</);
});

test('every citation in a long list is linked', () => {
  const text = 'S1 [00:13], S4 [01:09], S3 [00:17]';
  const out = linkCitations(text, IDS, escapeAttr);
  assert.equal(out.match(/data-cite-sid=/g).length, 3);
  assert.match(out, /data-cite-sid="sid-a"/);
  assert.match(out, /data-cite-sid="sid-d"/);
});

test('an ordinal past the group is left as plain text', () => {
  // The bug this guards: S44 cited against a 4-session cut.
  const out = linkCitations('see S44 [00:12]', IDS, escapeAttr);
  assert.equal(out, 'see S44 [00:12]');
});

test('an unresolvable stamp is left as plain text', () => {
  const out = linkCitations('see S2 [01:90]', IDS, escapeAttr);
  assert.equal(out, 'see S2 [01:90]');
});

test('no session ids means no links', () => {
  assert.equal(linkCitations('S1 [00:13]', [], escapeAttr), 'S1 [00:13]');
  assert.equal(linkCitations('S1 [00:13]', null, escapeAttr), 'S1 [00:13]');
});

test('bare prose is returned untouched', () => {
  const text = 'They read the FAQ and left.';
  assert.equal(linkCitations(text, IDS, escapeAttr), text);
});

test('a session id cannot break out of the attribute', () => {
  const out = linkCitations('S1 [00:05]', ['" onclick="evil()'], escapeAttr);
  assert.ok(!out.includes('onclick="evil()'));
  assert.match(out, /data-cite-sid="&quot; onclick=&quot;evil\(\)"/);
});

test('already-escaped text is not double-escaped', () => {
  // insightItem escapes before linkifying; the entities must survive intact.
  const out = linkCitations('&quot;Buy&quot; S1 [00:05]', IDS, escapeAttr);
  assert.match(out, /^&quot;Buy&quot; /);
});
