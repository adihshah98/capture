import test from 'node:test';
import assert from 'node:assert/strict';

const { gapEntries, isAttention } = await import('../src/replay/gaps.js');

const T0 = 1_700_000_000_000;
const min = (n) => T0 + n * 60_000;
const sec = (n) => T0 + n * 1000;

const meta = (t) => ({ type: 4, data: {}, timestamp: t });
const mutation = (t) => ({ type: 3, data: { source: 0 }, timestamp: t });
const mousemove = (t) => ({ type: 3, data: { source: 1, positions: [] }, timestamp: t });
const scroll = (t) => ({ type: 3, data: { source: 3, id: 1 }, timestamp: t });
const click = (t) => ({ type: 3, data: { source: 2, type: 2, id: 1 }, timestamp: t });
const blur = (t) => ({ type: 3, data: { source: 2, type: 6, id: 1 }, timestamp: t });
const marker = (tag, t, payload = {}) => ({ type: 5, data: { tag, payload }, timestamp: t });
const seen = (state, t) => marker('capture-visibility', t, { state });

const details = (events, end) => gapEntries(events, end).map((e) => `${e.kind}: ${e.detail}`);

test('a stall shorter than the threshold is not reported', () => {
  const events = [seen('focused', T0), scroll(T0), scroll(sec(14))];
  assert.deepEqual(details(events, sec(14)), []);
});

test('a stall longer than the threshold is reported from the last interaction', () => {
  const events = [seen('focused', T0), scroll(T0), scroll(sec(16))];
  assert.deepEqual(details(events, sec(16)), ['idle: on the page, no interaction for 16s']);
});

test('a page that emits nothing at all still reports the stall', () => {
  const events = [meta(T0), seen('focused', T0), mutation(sec(1)), mutation(min(20))];
  assert.deepEqual(details(events, min(20)), ['idle: on the page, no interaction for 20m 0s']);
});

test('a recording with no visibility markers does not claim the visitor was present', () => {
  const events = [meta(T0), mutation(min(20))];
  assert.deepEqual(details(events, min(20)), ['idle: no interaction for 20m 0s']);
});

test('mouse movement and mutations are not attention, so they do not mask a stall', () => {
  const events = [
    seen('focused', T0),
    scroll(T0),
    mousemove(sec(20)),
    mutation(sec(40)),
    mousemove(sec(50)),
    scroll(sec(60)),
  ];
  assert.deepEqual(details(events, sec(60)), ['idle: on the page, no interaction for 1m 0s']);
});

test('element blur is not attention, so switching away does not end the stall early', () => {
  const events = [seen('focused', T0), scroll(T0), blur(sec(5)), scroll(sec(60))];
  assert.deepEqual(details(events, sec(60)), ['idle: on the page, no interaction for 1m 0s']);
});

test('deliberate interactions end a stall', () => {
  for (const at of [click, scroll]) {
    const events = [seen('focused', T0), scroll(T0), at(sec(30)), at(sec(40))];
    assert.deepEqual(details(events, sec(40)), ['idle: on the page, no interaction for 30s']);
  }
});

test('a click marker counts even when its rrweb interaction is missing', () => {
  const events = [seen('focused', T0), scroll(T0), marker('capture-click', sec(30)), scroll(sec(40))];
  assert.deepEqual(details(events, sec(40)), ['idle: on the page, no interaction for 30s']);
});

test('a stall spent in another tab splits into presence, absence and presence', () => {
  const events = [
    seen('focused', T0),
    scroll(T0),
    seen('hidden', min(1)),
    seen('focused', min(6)),
    scroll(min(7)),
  ];
  assert.deepEqual(details(events, min(7)), [
    'idle: on the page, no interaction for 1m 0s',
    'away: left the tab for 5m 0s',
    'idle: on the page, no interaction for 1m 0s',
  ]);
});

test('an unfocused window is reported separately from a hidden tab', () => {
  const events = [
    seen('focused', T0),
    scroll(T0),
    seen('unfocused', sec(20)),
    seen('focused', sec(80)),
    scroll(sec(90)),
  ];
  assert.deepEqual(details(events, sec(90)), [
    'idle: on the page, no interaction for 20s',
    'unfocused: switched windows for 1m 0s',
    'idle: on the page, no interaction for 10s',
  ]);
});

test('leaving mid-stall and never coming back is not passed off as a duration', () => {
  const events = [seen('focused', T0), scroll(T0), seen('hidden', min(1))];
  assert.deepEqual(details(events, min(20)), [
    'idle: on the page, no interaction for 1m 0s',
    'away: left the tab and did not return',
  ]);
});

test('a hide that lands on the last event is the exit, not a stall', () => {
  const events = [seen('focused', T0), scroll(T0), scroll(sec(110)), seen('hidden', sec(120))];
  assert.deepEqual(details(events, sec(120)), [
    'idle: on the page, no interaction for 1m 50s',
    'away: left the tab — recording ends here',
  ]);
});

test('a short exit tail is still reported as the exit', () => {
  const events = [seen('focused', T0), scroll(T0), scroll(sec(60)), seen('hidden', sec(61))];
  assert.deepEqual(details(events, sec(69)), [
    'idle: on the page, no interaction for 1m 0s',
    'away: left the tab — recording ends here',
  ]);
});

test('a background-tab open is not reported as leaving', () => {
  const events = [seen('hidden', T0), meta(T0), seen('focused', sec(30)), scroll(sec(31))];
  assert.deepEqual(details(events, sec(60)), [
    'opened: opened in a background tab — first viewed after 30s',
    'idle: on the page, no interaction for 29s',
  ]);
});

test('a background tab that is never looked at says so', () => {
  const events = [seen('hidden', T0), meta(T0), mutation(min(5))];
  assert.deepEqual(details(events, min(5)), [
    'opened: opened in a background tab — never viewed',
  ]);
});

test('a window that loads without focus is not reported as switching away', () => {
  const events = [seen('unfocused', T0), meta(T0), seen('focused', sec(20)), scroll(sec(21))];
  assert.deepEqual(details(events, sec(21)), ['opened: loaded without focus for 20s']);
});

test('a momentary unfocus at load is ignored rather than reported', () => {
  const events = [seen('unfocused', T0), meta(T0), seen('focused', sec(1)), scroll(sec(30))];
  assert.deepEqual(details(events, sec(30)), ['idle: on the page, no interaction for 30s']);
});

test('interaction during the opening spell proves the visitor was there', () => {
  const events = [seen('unfocused', T0), meta(T0), scroll(sec(5)), seen('focused', sec(40))];
  assert.deepEqual(details(events, sec(60)), [
    'unfocused: switched windows for 35s',
    'idle: on the page, no interaction for 20s',
  ]);
});

test('a flicker shorter than three seconds is absorbed without losing time', () => {
  const events = [
    seen('focused', T0),
    scroll(T0),
    seen('hidden', sec(30)),
    seen('focused', sec(32)),
    scroll(sec(60)),
  ];
  assert.deepEqual(details(events, sec(60)), ['idle: on the page, no interaction for 1m 0s']);
});

test('a glance back mid-absence does not split the absence', () => {
  const events = [
    seen('focused', T0),
    scroll(T0),
    seen('hidden', sec(10)),
    seen('focused', min(5)),
    seen('hidden', sec(302)),
    seen('focused', min(10)),
    scroll(min(10)),
  ];
  assert.deepEqual(details(events, min(10)), [
    'idle: on the page, no interaction for 10s',
    'away: left the tab for 9m 50s',
  ]);
});

test('duplicated chunks do not create phantom rows', () => {
  const events = [
    seen('focused', T0),
    seen('focused', T0),
    scroll(T0),
    scroll(T0),
    seen('hidden', min(1)),
    seen('hidden', min(1)),
    seen('focused', min(6)),
    scroll(min(7)),
    scroll(min(7)),
  ];
  assert.deepEqual(details(events, min(7)), [
    'idle: on the page, no interaction for 1m 0s',
    'away: left the tab for 5m 0s',
    'idle: on the page, no interaction for 1m 0s',
  ]);
});

test('input order does not change the result', () => {
  const ordered = [
    seen('focused', T0),
    scroll(T0),
    seen('hidden', min(1)),
    seen('focused', min(6)),
    scroll(min(7)),
  ];
  const shuffled = [ordered[3], ordered[0], ordered[4], ordered[2], ordered[1]];
  assert.deepEqual(details(shuffled, min(7)), details(ordered, min(7)));
});

test('entries anchor to the moment the stall or absence begins', () => {
  const events = [
    seen('focused', T0),
    scroll(T0),
    seen('hidden', min(1)),
    seen('focused', min(6)),
    scroll(min(7)),
  ];
  assert.deepEqual(
    gapEntries(events, min(7)).map((e) => e.t),
    [T0, min(1), min(6)]
  );
});

test('degenerate input yields nothing rather than a wrong row', () => {
  assert.deepEqual(gapEntries([], 0), []);
  assert.deepEqual(gapEntries(undefined, min(5)), []);
  assert.deepEqual(gapEntries([meta(T0)], T0), []);
  assert.deepEqual(gapEntries([meta(min(5))], T0), []);
});

test('at most one entry can claim the visitor never came back', () => {
  const cases = [
    [[seen('focused', T0), scroll(T0), seen('hidden', min(1))], min(20)],
    [[seen('hidden', T0), meta(T0), mutation(min(5))], min(5)],
    [[seen('focused', T0), scroll(T0), scroll(sec(110)), seen('hidden', sec(120))], sec(120)],
    [[seen('focused', T0), scroll(T0), seen('unfocused', min(1)), seen('hidden', min(2))], min(9)],
  ];
  for (const [events, end] of cases) {
    const entries = gapEntries(events, end);
    assert.equal(entries.filter((e) => e.open).length <= 1, true);
    assert.deepEqual(
      entries.map((e) => e.t),
      [...entries.map((e) => e.t)].sort((a, b) => a - b)
    );
    for (const e of entries) assert.equal(e.t >= T0 && e.t <= end, true);
  }
});

test('attention is deliberate interaction only', () => {
  const attentive = [
    { type: 3, data: { source: 2, type: 2 } },
    { type: 3, data: { source: 3 } },
    { type: 3, data: { source: 5 } },
    { type: 3, data: { source: 6 } },
    { type: 3, data: { source: 7 } },
    { type: 3, data: { source: 12 } },
    { type: 3, data: { source: 14 } },
    { type: 5, data: { tag: 'capture-click' } },
    { type: 5, data: { tag: 'capture-viewport' } },
  ];
  const passive = [
    { type: 3, data: { source: 0 } },
    { type: 3, data: { source: 1 } },
    { type: 3, data: { source: 4 } },
    { type: 3, data: { source: 8 } },
    { type: 3, data: { source: 2, type: 5 } },
    { type: 3, data: { source: 2, type: 6 } },
    { type: 5, data: { tag: 'capture-visibility' } },
    { type: 5, data: { tag: 'capture-trigger' } },
    { type: 2, data: {} },
    { type: 4, data: {} },
    null,
  ];
  for (const e of attentive) assert.equal(isAttention(e), true, JSON.stringify(e));
  for (const e of passive) assert.equal(isAttention(e), false, JSON.stringify(e));
});

const parseDuration = (detail) => {
  const hms = detail.match(/(\d+)m (\d+)s/);
  if (hms) return (Number(hms[1]) * 60 + Number(hms[2])) * 1000;
  const s = detail.match(/(\d+)s/);
  return s ? Number(s[1]) * 1000 : 0;
};

const rng = (seed) => () => {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff;
  return seed / 0x7fffffff;
};

test('reported stretches never overlap, whatever the session looks like', () => {
  for (let run = 0; run < 500; run++) {
    const random = rng(run + 1);
    const span = 30_000 + Math.floor(random() * 900_000);
    const end = T0 + span;
    const events = [meta(T0)];
    const states = ['focused', 'hidden', 'unfocused'];
    if (random() < 0.85) {
      let at = T0;
      let i = random() < 0.25 ? 1 + Math.floor(random() * 2) : 0;
      while (at < end) {
        events.push(seen(states[i % 3], at));
        at += Math.floor(random() * span) / 3;
        i += 1 + Math.floor(random() * 2);
      }
    }
    for (let n = Math.floor(random() * 12); n > 0; n--) {
      const at = T0 + Math.floor(random() * span);
      events.push(random() < 0.5 ? scroll(at) : click(at));
      if (random() < 0.4) events.push(mousemove(at + 10), mutation(at + 20));
    }
    events.sort((a, b) => a.timestamp - b.timestamp);

    const entries = gapEntries(events, end);
    assert.equal(entries.filter((e) => e.open).length <= 1, true, `run ${run}: multiple open entries`);
    for (let i = 0; i < entries.length; i++) {
      assert.equal(entries[i].t >= T0 && entries[i].t <= end, true, `run ${run}: entry out of bounds`);
      const finish = entries[i].t + parseDuration(entries[i].detail);
      assert.equal(finish <= end + 1000, true, `run ${run}: entry runs past the recording`);
      if (i + 1 < entries.length) {
        assert.equal(
          finish <= entries[i + 1].t + 1000,
          true,
          `run ${run}: "${entries[i].detail}" overlaps "${entries[i + 1].detail}"`
        );
      }
    }
  }
});
