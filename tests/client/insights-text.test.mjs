import assert from 'node:assert/strict';
import test from 'node:test';

const { escapeHtml, escapeAttr, formatViewClock, progressText, clampGroupSize, analyzedCount } =
  await import('../../src/replay/insights/text.js');
const { chatMarkdown, chatInline } = await import('../../src/replay/insights/markdown.js');

// escapeHtml is the only thing between a model write-up and innerHTML.
test('escapeHtml neutralises a script tag', () => {
  assert.equal(
    escapeHtml('<script>alert(1)</script>'),
    '&lt;script&gt;alert(1)&lt;/script&gt;'
  );
});

test('escapeHtml escapes ampersands first, so entities are not double-decoded', () => {
  assert.equal(escapeHtml('&lt;'), '&amp;lt;');
});

test('escapeHtml handles quotes and non-strings', () => {
  assert.equal(escapeHtml('say "hi"'), 'say &quot;hi&quot;');
  assert.equal(escapeHtml(null), 'null');
  assert.equal(escapeHtml(42), '42');
});

test('escapeAttr additionally escapes the single quote', () => {
  assert.equal(escapeAttr("it's"), 'it&#39;s');
  // and keeps everything escapeHtml does
  assert.equal(escapeAttr('<a>'), '&lt;a&gt;');
});

test('clampGroupSize falls back rather than sampling zero sessions', () => {
  assert.equal(clampGroupSize('abc'), 100);
  assert.equal(clampGroupSize(''), 100);
  assert.equal(clampGroupSize(0), 100);
  assert.equal(clampGroupSize('12'), 12);
  assert.equal(clampGroupSize(12.4), 12);
});

test('clampGroupSize will not exceed the size the server accepts', () => {
  assert.equal(clampGroupSize(99999), 500);
});

test('formatViewClock returns empty for a non-number rather than NaN', () => {
  assert.equal(formatViewClock('nope'), '');
  assert.equal(formatViewClock(undefined), '');
  assert.notEqual(formatViewClock(1700000000000), '');
});

test('analyzedCount counts only the sessions that finished', () => {
  const cohort = {
    sessions: [
      { status: 'done', text: 'a write-up' },
      { status: 'error' },
      { status: 'queued' },
      { status: 'done', text: 'another' },
      // done with nothing written is stale, not analyzed
      { status: 'done' },
    ],
  };
  assert.equal(analyzedCount(cohort), 2);
});

test('analyzedCount survives a cohort with no sessions', () => {
  assert.equal(analyzedCount({}), 0);
  assert.equal(analyzedCount(null), 0);
  assert.equal(analyzedCount({ sessions: [] }), 0);
});

test('progressText describes a cohort without throwing on a partial one', () => {
  assert.equal(typeof progressText({ sessions: [{ status: 'done' }] }), 'string');
  assert.equal(typeof progressText({}), 'string');
});

test('chat markdown renders bullets as a list and prose as paragraphs', () => {
  assert.equal(chatMarkdown('- one\n- two'), '<ul><li>one</li><li>two</li></ul>');
  assert.equal(chatMarkdown('hello'), '<p>hello</p>');
  assert.equal(chatMarkdown('a\n\nb'), '<p>a</p><p>b</p>');
});

test('a single newline is a line break, not a new paragraph', () => {
  assert.equal(chatMarkdown('a\nb'), '<p>a<br>b</p>');
});

test('a mixed block is a paragraph, not a half-list', () => {
  assert.equal(chatMarkdown('intro\n- one'), '<p>intro<br>- one</p>');
});

test('chatMarkdown emits nothing for empty input', () => {
  assert.equal(chatMarkdown(''), '');
  assert.equal(chatMarkdown('\n\n'), '');
});

test('chatInline renders backticks as code', () => {
  assert.equal(chatInline('use `npm test`'), 'use <code>npm test</code>');
  assert.equal(chatInline('none here'), 'none here');
});

test('markdown runs on escaped text, so a tag cannot survive the round trip', () => {
  const rendered = chatInline(chatMarkdown(escapeHtml('<img onerror=x>')));
  assert.ok(!rendered.includes('<img'));
  assert.ok(rendered.includes('&lt;img'));
});
