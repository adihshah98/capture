import test from 'node:test';
import assert from 'node:assert/strict';

const { selectorFor, textFor, createRageTracker } = await import('../../src/recorder/clicks.js');

function el(tag, { id, className = '', attrs = {}, text, children = [], masked = false } = {}) {
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    id: id || '',
    className,
    parentElement: null,
    previousElementSibling: null,
    nextElementSibling: null,
    childNodes: [],
    getAttribute: (name) => attrs[name] ?? null,
    matches: (sel) => masked && sel.includes('capture-mask'),
  };
  if (text !== undefined) node.childNodes.push({ nodeType: 3, textContent: text });
  for (const child of children) {
    child.parentElement = node;
    node.childNodes.push(child);
  }
  const kids = node.childNodes.filter((c) => c.nodeType === 1);
  kids.forEach((kid, i) => {
    kid.previousElementSibling = kids[i - 1] || null;
    kid.nextElementSibling = kids[i + 1] || null;
  });
  return node;
}

test('selector stops at the nearest id', () => {
  const button = el('button', { text: 'Buy' });
  el('form', { id: 'order', children: [el('div', { children: [button] })] });
  assert.equal(selectorFor(button), 'form#order > div > button');
});

test('selector includes up to two classes', () => {
  const button = el('button', { className: 'btn btn-primary large' });
  assert.equal(selectorFor(button), 'button.btn.btn-primary');
});

test('selector disambiguates anonymous siblings with nth-of-type', () => {
  const first = el('li');
  const second = el('li');
  el('ul', { className: 'menu', children: [first, second] });
  assert.equal(selectorFor(second), 'ul.menu > li:nth-of-type(2)');
  assert.equal(selectorFor(first), 'ul.menu > li:nth-of-type(1)');
});

test('selector depth is capped', () => {
  const button = el('button');
  let node = button;
  for (let i = 0; i < 10; i++) {
    node = el('div', { className: `level-${i}`, children: [node] });
  }
  assert.ok(selectorFor(button).split(' > ').length <= 5);
});

test('direct text wins', () => {
  assert.equal(textFor(el('button', { text: '  Buy   now  ' })), 'Buy now');
});

test('nested span text is collected', () => {
  const button = el('button', { children: [el('span', { text: 'Buy now' })] });
  assert.equal(textFor(button), 'Buy now');
});

test('masked descendants are skipped', () => {
  const button = el('button', {
    children: [el('span', { text: 'Buy' }), el('span', { text: 'secret', masked: true })],
  });
  assert.equal(textFor(button, '.capture-mask'), 'Buy');
});

test('falls back to aria-label then title', () => {
  assert.equal(textFor(el('a', { attrs: { 'aria-label': 'Checkout' } })), 'Checkout');
  assert.equal(textFor(el('a', { attrs: { title: 'Cart' } })), 'Cart');
});

test('submit inputs use their value, text inputs never do', () => {
  assert.equal(textFor(el('input', { attrs: { type: 'submit', value: 'Order' } })), 'Order');
  assert.equal(textFor(el('input', { attrs: { type: 'text', value: 'anant@example.com' } })), '');
});

test('long digit runs are scrubbed', () => {
  const text = textFor(el('button', { text: 'Card 4242 4242 4242 4242 saved' }));
  assert.ok(!text.includes('4242'));
});

test('text is capped at 64 characters', () => {
  assert.ok(textFor(el('button', { text: 'x'.repeat(500) })).length <= 64);
});

test('three clicks in the same spot within a second is rage', () => {
  const track = createRageTracker();
  assert.equal(track(100, 100, 0), false);
  assert.equal(track(105, 102, 200), false);
  assert.equal(track(98, 99, 400), true);
  assert.equal(track(98, 99, 500), false, 'burst resets after firing');
});

test('spread-out or slow clicks are not rage', () => {
  const track = createRageTracker();
  track(100, 100, 0);
  track(300, 300, 200);
  assert.equal(track(100, 100, 400), false);
  const slow = createRageTracker();
  slow(100, 100, 0);
  slow(100, 100, 1200);
  assert.equal(slow(100, 100, 2400), false);
});
