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

// The model drifts around the format the prompt asks for. Each of these was
// rendering as dead text while its neighbours linked — the "some have a link
// and some don't" report.
const DRIFT = [
  ['a linking word before the stamp', 'S2 at [00:08]', 8000],
  ['an @ before the stamp', 'S2 @ [00:08]', 8000],
  ['parentheses instead of brackets', 'S2 (00:12)', 12000],
  ['no space at all', 'S2[00:46]', 46000],
  ['a single-digit minute', 'S2 [1:09]', 69000],
  ['fractional seconds', 'S2 [00:17.5]', 17500],
  ['a hyphen range seeks to the start', 'S2 [00:05-00:12]', 5000],
  ['an en-dash range seeks to the start', 'S2 [00:05 – 00:12]', 5000],
];

for (const [name, text, ms] of DRIFT) {
  test(`links ${name}`, () => {
    const out = linkCitations(text, IDS, escapeAttr);
    assert.match(out, /class="cut-cite"/, text);
    assert.match(out, new RegExp(`data-cite-t="${ms}"`), text);
    assert.match(out, /data-cite-sid="sid-b"/);
  });
}

test('a range keeps its full text in the link', () => {
  // Seeking to the start must not visibly truncate what the model wrote.
  const out = linkCitations('S2 [00:05-00:12]', IDS, escapeAttr);
  assert.match(out, />S2 \[00:05-00:12\]</);
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
