import { formatDuration } from './format.js';

const IDLE_THRESHOLD_MS = 15000;
const MIN_SEGMENT_MS = 3000;
const LOAD_TOLERANCE_MS = 1000;

const ATTENTION_SOURCES = new Set([2, 3, 5, 6, 7, 12, 14]);
const PASSIVE_MOUSE_INTERACTIONS = new Set([5, 6]);
const ATTENTION_TAGS = new Set(['capture-click', 'capture-viewport']);

const AWAY = { hidden: 'away', unfocused: 'unfocused' };
const KIND = { untracked: 'idle', present: 'idle', hidden: 'away', unfocused: 'unfocused' };

const STALLED = {
  untracked: (d) => `no interaction for ${d}`,
  present: (d) => `on the page, no interaction for ${d}`,
  hidden: (d) => `left the tab for ${d}`,
  unfocused: (d) => `switched windows for ${d}`,
};
const NEVER_RETURNED = {
  hidden: 'left the tab and did not return',
  unfocused: 'switched windows and did not return',
};
const ENDED_AWAY = {
  hidden: 'left the tab — recording ends here',
  unfocused: 'switched windows — recording ends here',
};
const OPENED_AWAY = {
  hidden: (d) => `opened in a background tab — first viewed after ${d}`,
  unfocused: (d) => `loaded without focus for ${d}`,
};
const OPENED_STAYED_AWAY = {
  hidden: 'opened in a background tab — never viewed',
  unfocused: 'loaded without focus — never focused',
};

const entry = (t, kind, detail, open = false) => ({
  t,
  kind,
  css: kind === 'idle' ? 'kind-idle' : 'kind-away',
  detail,
  open,
});

export function isAttention(event) {
  if (!event) return false;
  if (event.type === 5) return Boolean(event.data && ATTENTION_TAGS.has(event.data.tag));
  if (event.type !== 3 || !event.data) return false;
  const { source, type } = event.data;
  if (!ATTENTION_SOURCES.has(source)) return false;
  return !(source === 2 && PASSIVE_MOUSE_INTERACTIONS.has(type));
}

function attentionTimes(events) {
  const times = [];
  for (const event of events) {
    if (isAttention(event) && typeof event.timestamp === 'number') times.push(event.timestamp);
  }
  return times.sort((a, b) => a - b);
}

function visibilityPoints(events) {
  const points = [];
  for (const event of events) {
    if (event.type !== 5 || !event.data || event.data.tag !== 'capture-visibility') continue;
    const state = (event.data.payload || {}).state;
    if (state !== 'hidden' && state !== 'unfocused' && state !== 'focused') continue;
    points.push({ state, t: event.timestamp });
  }
  points.sort((a, b) => a.t - b.t);
  return points.filter((p, i) => i === 0 || p.state !== points[i - 1].state);
}

function presenceIntervals(points, from, end) {
  const raw = points.length ? [] : [{ cls: 'untracked', start: from, end }];
  if (points.length && points[0].t > from) {
    raw.push({ cls: 'untracked', start: from, end: points[0].t });
  }
  points.forEach((point, i) => {
    const next = points[i + 1];
    raw.push({
      cls: point.state === 'focused' ? 'present' : point.state,
      start: point.t,
      end: next ? next.t : end,
      open: !next,
    });
  });
  return raw
    .map((iv) => ({
      cls: iv.cls,
      start: Math.max(iv.start, from),
      end: Math.min(iv.end, end),
      open: Boolean(iv.open),
    }))
    .filter((iv) => iv.end > iv.start);
}

function idleGaps(times, from, end) {
  const gaps = [];
  let previous = from;
  for (const t of times) {
    if (t <= previous) continue;
    if (t - previous > IDLE_THRESHOLD_MS) gaps.push({ start: previous, end: t });
    previous = t;
  }
  if (end - previous > IDLE_THRESHOLD_MS) gaps.push({ start: previous, end });
  return gaps;
}

function segmentsFor(gap, intervals) {
  const raw = [];
  for (const iv of intervals) {
    const start = Math.max(iv.start, gap.start);
    const stop = Math.min(iv.end, gap.end);
    if (stop > start) raw.push({ cls: iv.cls, start, end: stop, open: iv.open && stop >= iv.end });
  }
  raw.sort((a, b) => a.start - b.start);

  const out = [];
  for (const seg of raw) {
    const last = out[out.length - 1];
    if (last && (last.cls === seg.cls || seg.end - seg.start < MIN_SEGMENT_MS)) {
      last.end = seg.end;
      last.open = seg.open;
    } else out.push({ ...seg });
  }
  if (out.length > 1 && out[0].end - out[0].start < MIN_SEGMENT_MS) {
    out[1].start = out[0].start;
    out.shift();
  }
  return out;
}

function openingEntry(intervals, times, streamStart) {
  const lead = intervals[0];
  if (!lead || !AWAY[lead.cls]) return null;
  if (lead.start > streamStart + LOAD_TOLERANCE_MS) return null;
  if (times.some((t) => t >= lead.start && t < lead.end)) return null;
  const returned = intervals.length > 1;
  if (returned && lead.end - lead.start < MIN_SEGMENT_MS) return null;
  const detail = returned
    ? OPENED_AWAY[lead.cls](formatDuration(lead.end - lead.start))
    : OPENED_STAYED_AWAY[lead.cls];
  return { at: lead.end, item: entry(lead.start, 'opened', detail, !returned) };
}

export function gapEntries(events, end) {
  const list = Array.isArray(events) ? events : [];
  let streamStart = Infinity;
  for (const event of list) {
    if (typeof event.timestamp === 'number' && event.timestamp < streamStart) {
      streamStart = event.timestamp;
    }
  }
  if (!Number.isFinite(streamStart) || !(end > streamStart)) return [];

  const times = attentionTimes(list);
  const points = visibilityPoints(list);
  const intervals = presenceIntervals(points, streamStart, end);
  const entries = [];

  let from = streamStart;
  const opening = openingEntry(intervals, times, streamStart);
  if (opening) {
    entries.push(opening.item);
    from = opening.at;
  }

  const scoped = intervals
    .map((iv) => ({ cls: iv.cls, start: Math.max(iv.start, from), end: iv.end, open: iv.open }))
    .filter((iv) => iv.end > iv.start);

  let reportedUntil = from;
  for (const gap of idleGaps(times, from, end)) {
    for (const seg of segmentsFor(gap, scoped)) {
      const open = Boolean(AWAY[seg.cls]) && seg.open;
      const detail = open
        ? NEVER_RETURNED[seg.cls]
        : STALLED[seg.cls](formatDuration(seg.end - seg.start));
      entries.push(entry(seg.start, KIND[seg.cls], detail, open));
      reportedUntil = Math.max(reportedUntil, seg.end);
    }
  }

  const last = points[points.length - 1];
  const unreported = last && last.t >= reportedUntil && !entries.some((e) => e.open);
  if (unreported && AWAY[last.state]) {
    entries.push(entry(last.t, AWAY[last.state], ENDED_AWAY[last.state], true));
  }

  return entries.sort((a, b) => a.t - b.t);
}
