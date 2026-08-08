import { formatDuration } from './format.js';

const AWAY_LABELS = { hidden: 'left the tab', unfocused: 'switched windows' };
const MIN_AWAY_MS = 3000;

const overlapMs = (a, b) => Math.max(0, Math.min(a.end, b.end) - Math.max(a.start, b.start));

const awayKind = (state) => (state === 'hidden' ? 'away' : 'unfocused');

const awayEntry = (t, state, detail) => ({ t, kind: awayKind(state), css: 'kind-away', detail });

const idleEntry = (t, detail) => ({ t, kind: 'idle', css: 'kind-idle', detail });

function awaySpans(markers, end) {
  const points = markers.filter((m) => m.data.tag === 'capture-visibility');
  const spans = [];
  points.forEach((point, i) => {
    const state = (point.data.payload || {}).state;
    if (!AWAY_LABELS[state]) return;
    const next = points[i + 1];
    spans.push({
      state,
      start: point.timestamp,
      end: next ? next.timestamp : end,
      returned: Boolean(next),
    });
  });
  return spans.filter((s) => !s.returned || s.end - s.start >= MIN_AWAY_MS);
}

function idleGaps(markers, end) {
  const gaps = [];
  let start = null;
  for (const marker of markers) {
    const tag = marker.data.tag;
    if (tag === 'capture-idle' && start === null) start = marker.timestamp;
    else if (tag === 'capture-idle-end' && start !== null) {
      gaps.push({ start, end: marker.timestamp });
      start = null;
    }
  }
  if (start !== null && end > start) gaps.push({ start, end });
  return gaps;
}

function gapEntry(gap, spans, tracked) {
  const length = gap.end - gap.start;
  if (!tracked) return idleEntry(gap.start, `no interaction for ${formatDuration(length)}`);

  const awayBy = (state) =>
    spans.reduce((ms, span) => (span.state === state ? ms + overlapMs(span, gap) : ms), 0);
  const hidden = awayBy('hidden');
  const unfocused = awayBy('unfocused');
  if (hidden + unfocused < length / 2) {
    return idleEntry(gap.start, `on the page, no interaction for ${formatDuration(length)}`);
  }

  const state = hidden >= unfocused ? 'hidden' : 'unfocused';
  const gone = state === 'hidden' ? hidden : unfocused;
  const openEnded = spans.some((s) => s.state === state && !s.returned && overlapMs(s, gap) > 0);
  return awayEntry(
    gap.start,
    state,
    openEnded
      ? `${AWAY_LABELS[state]} and did not return`
      : `${AWAY_LABELS[state]} for ${formatDuration(gone)} of ${formatDuration(length)}`
  );
}

export function gapEntries(markers, end) {
  const spans = awaySpans(markers, end);
  const gaps = idleGaps(markers, end);
  const tracked = markers.some((m) => m.data.tag === 'capture-visibility');
  const summaries = gaps.map((gap) => ({ gap, entry: gapEntry(gap, spans, tracked) }));
  const entries = summaries.map((s) => s.entry);

  const summarized = (span) =>
    summaries.some(
      ({ gap, entry }) =>
        entry.kind === awayKind(span.state) && overlapMs(span, gap) * 2 > span.end - span.start
    );

  for (const span of spans) {
    if (summarized(span)) continue;
    entries.push(
      awayEntry(
        span.start,
        span.state,
        span.returned
          ? `${AWAY_LABELS[span.state]} for ${formatDuration(span.end - span.start)}`
          : `${AWAY_LABELS[span.state]} — recording ends here`
      )
    );
  }
  return entries;
}
