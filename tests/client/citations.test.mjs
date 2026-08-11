import test from 'node:test';
import assert from 'node:assert/strict';

const { citationMs, linkCitations } = await import('../../src/replay/citations.js');

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

// A claim about a repeated action puts every occurrence in one bracket. This
// matched neither branch: STAMP wanted "]" and found ", 01:54]", and the bare
// fence wanted punctuation and found " [". Inside parens, nothing fired.
test('a multi-stamp bracket seeks to the first time', () => {
  const text =
    "2% (1 of 46) clicked 'BUY NOW' twice without it adding to cart " +
    '(S2 [00:33, 01:54]), leading to abandonment.';
  const out = linkCitations(text, IDS, escapeAttr);
  assert.match(out, /data-cite-sid="sid-b"/);
  assert.match(out, /data-cite-t="33000"/);
});

test('a multi-stamp bracket is wrapped whole', () => {
  // A partial match would leave ", 01:54]" dangling outside the link.
  const out = linkCitations('S2 [00:33, 01:54]', IDS, escapeAttr);
  assert.match(out, />S2 \[00:33, 01:54\]<\/button>/);
});

test('three stamps in one bracket still link', () => {
  const out = linkCitations('(S2 [00:33, 01:54, 02:10])', IDS, escapeAttr);
  assert.match(out, /data-cite-t="33000"/);
  assert.match(out, />S2 \[00:33, 01:54, 02:10\]<\/button>/);
});

test('a malformed time in a stamp list is not seekable', () => {
  // The list must be times throughout; "01:90" is a formatting error.
  const out = linkCitations('see S2 [01:90, 02:10]', IDS, escapeAttr);
  assert.doesNotMatch(out, /cut-cite/);
});

test('a range keeps its full text in the link', () => {
  // Seeking to the start must not visibly truncate what the model wrote.
  const out = linkCitations('S2 [00:05-00:12]', IDS, escapeAttr);
  assert.match(out, />S2 \[00:05-00:12\]</);
});

// Counting claims cite bare ordinals — the model only has a moment to point
// at when the claim is about a moment. Both forms appear in one sentence.
const IDS7 = ['s1', 's2', 's3', 's4', 's5', 's6', 's7'];

test('bare ordinals in a citation list all link', () => {
  const out = linkCitations('(S1, S2, S4, S7)', IDS7, escapeAttr);
  assert.equal((out.match(/cut-cite/g) || []).length, 4);
  // No stamp means open at the start, which is still the right session.
  assert.equal((out.match(/data-cite-t="0"/g) || []).length, 4);
});

test('bare and stamped citations mix in one observation', () => {
  // The reported case: the stamped group linked, the bare group did not.
  const text =
    "4 of 7 purchased the default '90-Night Supply' (S1, S2, S4, S7), while 3 " +
    "switched to '30-Night Supply' (S3 [01:10], S5 [00:10], S6 [00:36]).";
  const out = linkCitations(text, IDS7, escapeAttr);
  assert.equal((out.match(/cut-cite/g) || []).length, 7);
  assert.match(out, /data-cite-sid="s1" data-cite-t="0"/);
  assert.match(out, /data-cite-sid="s3" data-cite-t="70000"/);
});

test('a bare ordinal links at a clause end', () => {
  for (const text of ['S2', 'S2.', 'S2;', 'S1 and S2', 'S2)']) {
    assert.match(linkCitations(text, IDS, escapeAttr), /cut-cite/, text);
  }
});

// The clause fence assumes the citation trails its claim, but the model also
// writes the session as the sentence's subject, where a verb follows the
// ordinal. These rendered dead next to linking neighbours — the second round
// of "some have a link and some don't".
const LEADING = [
  ['a verb follows the ordinal', "S3 attempted to cancel an order via chat"],
  ['a stamp sits downstream', "S3 clicked 'BUY NOW' twice [00:33, 01:54]"],
  ['several stamps sit downstream', 'S3 clicked the CTA at [01:55], but it scrolled [01:56]'],
  ['after a sentence end', 'Users hesitated. S2 scrolled past the price.'],
  ['as a bullet', '- S2 abandoned at checkout'],
];

for (const [name, text] of LEADING) {
  test(`links a leading citation when ${name}`, () => {
    const out = linkCitations(text, IDS, escapeAttr);
    assert.match(out, /class="cut-cite"/, text);
    // A downstream stamp belongs to the event, not the ordinal: S43's line has
    // one each for the click, the scroll and the recovery, so choosing among
    // them would seek to a moment the sentence is not about. Open at the start.
    assert.match(out, /data-cite-t="0"/, text);
  });
}

test('a leading citation still resolves through the map', () => {
  const out = linkCitations('S1 clicked BUY NOW twice', IDS, escapeAttr, { 1: 'mapped' });
  assert.match(out, /data-cite-sid="mapped"/);
});

test('a leading ordinal past the group stays plain text', () => {
  assert.doesNotMatch(linkCitations('S44 attempted to cancel', IDS, escapeAttr), /cut-cite/);
});

test('a leading citation links only the ordinal, not the sentence', () => {
  const out = linkCitations('S2 clicked BUY NOW twice', IDS, escapeAttr);
  assert.match(out, />S2<\/button> clicked BUY NOW twice$/);
});

// A bare S<n> is loose enough to hit ordinary prose, so it is fenced.
test('prose that merely looks like a citation is not linked', () => {
  for (const text of [
    'the S3 model',
    'size S4 shirt',
    'costs $S3',
    'CSS3 rules',
    'HTTPS3',
    'AS1 BS2',
  ]) {
    assert.doesNotMatch(linkCitations(text, IDS, escapeAttr), /cut-cite/, text);
  }
});

test('a zero ordinal is not a session', () => {
  assert.doesNotMatch(linkCitations('S0 [00:05]', IDS, escapeAttr), /cut-cite/);
});

// The stored map is what the model was actually shown. It wins over
// session_ids, so a later change to row ordering cannot repoint old links.
test('the stored map resolves the ordinal', () => {
  const map = { 1: 'from-map', 2: 'also-map' };
  const out = linkCitations('S1 [00:05]', IDS, escapeAttr, map);
  assert.match(out, /data-cite-sid="from-map"/);
});

test('the map wins over positional session_ids', () => {
  // Deliberately conflicting: position 1 is "sid-a", the map says otherwise.
  const map = { 1: 'authoritative' };
  const out = linkCitations('S1', IDS, escapeAttr, map);
  assert.match(out, /data-cite-sid="authoritative"/);
  assert.doesNotMatch(out, /sid-a/);
});

test('an ordinal missing from the map does not fall back to position', () => {
  // The map is complete by construction, so a miss means "not a session".
  const map = { 1: 'only-one' };
  assert.doesNotMatch(linkCitations('S2', IDS, escapeAttr, map), /cut-cite/);
});

test('cuts stored before the map fall back to session_ids', () => {
  for (const empty of [null, undefined, {}]) {
    const out = linkCitations('S2 [00:05]', IDS, escapeAttr, empty);
    assert.match(out, /data-cite-sid="sid-b"/, String(empty));
  }
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
