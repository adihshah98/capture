import assert from 'node:assert/strict';
import test from 'node:test';

// The path a model's write-up takes to the page. It lived inside renderCut,
// which assigned straight to innerHTML, so none of it could be asserted on.
const {
  cutHtml,
  insightItem,
  actionTargets,
  targetForRecommendation,
  recommendationItem,
  withCitations,
  CUT_SECTIONS,
} = await import('../../src/replay/insights/cutview.js');

const cut = (over = {}) => ({
  label: 'Mobile buyers',
  n: 5,
  model: 'gemini-2.5-flash',
  session_ids: ['s1', 's2'],
  citations: null,
  analysis: {},
  ...over,
});

test('the model headline is escaped, not rendered', () => {
  const html = cutHtml(cut({ analysis: { headline: '<img src=x onerror=alert(1)>' } }));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
});

test('an observation cannot smuggle markup through an insight', () => {
  const html = cutHtml(
    cut({ analysis: { journey: [{ observation: '<script>x</script>' }] } })
  );
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

test('a section with no items shows its empty copy', () => {
  const html = cutHtml(cut({ analysis: { journey: [] } }));
  const journey = CUT_SECTIONS.find((s) => s.key === 'journey');
  assert.ok(html.includes(journey.empty));
});

test('caveats are omitted entirely when absent, since they have no empty copy', () => {
  const html = cutHtml(cut({ analysis: {} }));
  assert.ok(!html.includes('id="cut-section-caveats"'));
});

test('a caveat renders when there is one', () => {
  const html = cutHtml(cut({ analysis: { caveats: ['only two sessions'] } }));
  assert.ok(html.includes('id="cut-section-caveats"'));
  assert.ok(html.includes('only two sessions'));
});

test('a small group is marked as thin evidence', () => {
  assert.ok(cutHtml(cut({ n: 2, analysis: { journey: ['a'] } })).includes('cut-thin'));
  assert.ok(!cutHtml(cut({ n: 3, analysis: { journey: ['a'] } })).includes('cut-thin'));
  // n=0 is not "thin", it is empty
  assert.ok(!cutHtml(cut({ n: 0, analysis: { journey: ['a'] } })).includes('cut-thin'));
});

test('the group size is shown once, on the first section', () => {
  const html = cutHtml(cut({ n: 7, analysis: { journey: ['a'], intent: ['b'] } }));
  assert.equal(html.split('(n=7)').length - 1, 1);
});

test('a recommendation links to the insight whose action it repeats', () => {
  const html = cutHtml(
    cut({
      analysis: {
        blockers: [{ observation: 'form is long', action: 'Shorten the form' }],
        recommendations: ['Shorten the form'],
      },
    })
  );
  assert.ok(html.includes('data-scroll-to="cut-insight-blockers-0"'));
});

test('a recommendation matching nothing is still shown, just not linked', () => {
  const html = cutHtml(cut({ analysis: { recommendations: ['Something unrelated'] } }));
  assert.ok(html.includes('Something unrelated'));
  assert.ok(!html.includes('data-scroll-to'));
});

test('no recommendations means no recommendations block', () => {
  assert.ok(!cutHtml(cut({ analysis: { journey: ['a'] } })).includes('recommendations'));
});

test('actionTargets skips caveats, which are not actionable', () => {
  const targets = actionTargets({
    caveats: [{ observation: 'c', action: 'ignore me' }],
    blockers: [{ observation: 'b', action: 'do this' }],
  });
  assert.equal(targets.has('ignore me'), false);
  assert.equal(targets.get('do this'), 'cut-insight-blockers-0');
});

test('targetForRecommendation prefers the longest overlapping action', () => {
  const targets = new Map([
    ['shorten', 'id-short'],
    ['shorten the checkout form', 'id-long'],
  ]);
  assert.equal(targetForRecommendation('Shorten the checkout form', targets), 'id-long');
  assert.equal(targetForRecommendation('', targets), null);
  assert.equal(targetForRecommendation('nothing alike', targets), null);
});

test('an insight renders its share and action when present', () => {
  const html = insightItem(
    { observation: 'they scrolled past', share: '4 of 5', action: 'move it up' },
    'cut-insight-journey-0',
    [],
    null
  );
  assert.ok(html.includes('4 of 5'));
  assert.ok(html.includes('move it up'));
  assert.ok(html.includes('id="cut-insight-journey-0"'));
});

test('a bare string insight still renders', () => {
  assert.equal(insightItem('just text', null, [], null), '<li>just text</li>');
});

test('recommendationItem escapes and only adds a button when linked', () => {
  assert.equal(recommendationItem('<b>x</b>', null), '<li>&lt;b&gt;x&lt;/b&gt;</li>');
  assert.ok(recommendationItem('x', 'target-1').includes('data-scroll-to="target-1"'));
});

test('withCitations escapes before linkifying', () => {
  assert.ok(!withCitations('<i>hi</i>', [], null).includes('<i>'));
});

test('the meta line reports the group size and the model', () => {
  const html = cutHtml(cut({ n: 12, model: 'gemini-x' }));
  assert.ok(html.includes('12 analyzed session write-up(s)'));
  assert.ok(html.includes('gemini-x'));
});

test('a missing model is described rather than left blank', () => {
  assert.ok(cutHtml(cut({ model: undefined })).includes('the model'));
});

test('an empty analysis still produces a usable body', () => {
  const html = cutHtml({ n: 0, analysis: {} });
  assert.equal(typeof html, 'string');
  assert.ok(html.includes('cut-meta'));
});
