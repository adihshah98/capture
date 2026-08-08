/**
 * Tier 1 session facts: geometry and time only.
 *
 * Everything here is derived from scroll offsets, viewport size, page height
 * and event timestamps — never from a customer's markup. That is the whole
 * point: a CSS refactor on the recorded site cannot change what these numbers
 * mean, so a series built on them stays comparable indefinitely.
 *
 * Selector-keyed facts (a named button, a named section) are deliberately NOT
 * here. They can silently change identity when markup moves, so they belong in
 * a separate tier that carries its own coverage denominator.
 *
 * Pure and dependency-free so the whole tier is unit-testable without a
 * browser, in the same style as the extractors in extract.mjs.
 */

// Quartiles of page height. Coarse on purpose: fine bands are mostly noise on
// short pages, where a decile can be a few dozen pixels.
export const DEPTH_BANDS = [
  [0, 25],
  [25, 50],
  [50, 75],
  [75, 100],
];

export const bandKey = ([lo, hi]) => `${lo}-${hi}`;

/** Scrollable distance: the part of the page that is not the first screenful. */
export function scrollableRange(pageHeight, viewport) {
  const vh = (viewport && viewport.height) || 0;
  return Math.max(0, (pageHeight || 0) - vh);
}

/**
 * How far down the page a scroll offset sits, 0-100.
 *
 * Measured against scrollable distance rather than raw page height, so "100%"
 * means "reached the bottom" on any page. A page shorter than the viewport
 * has nothing to scroll: it is entirely visible at rest, which is 100%.
 */
export function depthPct(y, pageHeight, viewport) {
  const range = scrollableRange(pageHeight, viewport);
  if (range <= 0) return 100;
  const pct = ((y || 0) / range) * 100;
  return Math.max(0, Math.min(100, pct));
}

const bandIndexFor = (pct) => {
  for (let i = 0; i < DEPTH_BANDS.length; i += 1) {
    const [lo, hi] = DEPTH_BANDS[i];
    // Upper edge is inclusive only in the last band, so 25% lands in 25-50.
    if (pct >= lo && (pct < hi || i === DEPTH_BANDS.length - 1)) return i;
  }
  return DEPTH_BANDS.length - 1;
};

/** Total length of the overlap between one interval and a list of spans. */
function overlapMs(from, to, spans) {
  let ms = 0;
  for (const sp of spans) {
    const lo = Math.max(from, sp.start);
    const hi = Math.min(to, sp.end);
    if (hi > lo) ms += hi - lo;
  }
  return ms;
}

/**
 * Active milliseconds spent at each scroll-depth band.
 *
 * The scroll timeline gives the offset held between consecutive scroll events;
 * intersecting each of those stretches with the active spans is what makes this
 * dwell rather than wall-clock. A tab parked mid-page accrues nothing.
 *
 * `points` are {t, y} in absolute ms (as scrollTimeline returns them); spans
 * are {start, end} offsets from t0. Passing t0 explicitly keeps this pure
 * rather than assuming points[0] is the session start.
 */
export function dwellByBand(points, spans, { pageHeight, viewport, t0, endMs }) {
  const totals = DEPTH_BANDS.map(() => 0);
  if (!points || !points.length || !spans || !spans.length) {
    return Object.fromEntries(DEPTH_BANDS.map((b, i) => [bandKey(b), totals[i]]));
  }

  const sorted = [...points].sort((a, b) => a.t - b.t);
  for (let i = 0; i < sorted.length; i += 1) {
    const from = sorted[i].t - t0;
    const to = i + 1 < sorted.length ? sorted[i + 1].t - t0 : endMs;
    if (to <= from) continue;
    const idx = bandIndexFor(depthPct(sorted[i].y, pageHeight, viewport));
    totals[idx] += overlapMs(from, to, spans);
  }

  return Object.fromEntries(DEPTH_BANDS.map((b, i) => [bandKey(b), Math.round(totals[i])]));
}

/**
 * Deepest point reached, 0-100. Read off the raw scroll timeline rather than
 * the dwell bands: a visitor who flew to the bottom and straight back up still
 * reached it, even with no dwell time to show for it.
 */
export function maxDepthPct(points, { pageHeight, viewport }) {
  if (!points || !points.length) return 0;
  let max = 0;
  for (const p of points) max = Math.max(max, depthPct(p.y, pageHeight, viewport));
  return Math.round(max);
}

/**
 * Distinct pages visited, in first-seen order. `urls` as recorded repeats the
 * same page on every chunk boundary, which would inflate a page-view count.
 */
export function pageViews(urls) {
  const seen = [];
  for (const raw of urls || []) {
    const url = typeof raw === 'string' ? raw : raw && raw.url;
    if (url && !seen.includes(url)) seen.push(url);
  }
  return seen;
}

/**
 * The Tier 1 fact row for one session.
 *
 * Flat and scalar by design: this is what a cohort aggregates over, and every
 * field must mean the same thing in every session for a rate across them to be
 * meaningful. `click_count` and `rage_click_count` are counts, deliberately not
 * broken down by selector — the breakdown is what would rot.
 */
export function sessionFacts(digest) {
  const session = (digest && digest.session) || {};
  const clicks = (digest && digest.clicks) || [];
  const viewport = session.viewport || null;
  const pageHeight = session.pageHeight || 0;

  const pages = pageViews(session.urls);
  const durationMs = session.durationMs || 0;
  const activeMsTotal = session.activeMs || 0;

  return {
    schema: 1,
    duration_s: Math.round(durationMs / 1000),
    active_s: Math.round(activeMsTotal / 1000),
    idle_s: Math.round((session.idleMs || 0) / 1000),
    // Share of the session actually spent doing something. Null rather than 0
    // for a zero-length session: "no engagement" and "nothing recorded" are
    // different facts, and averaging them together would be wrong.
    active_pct: durationMs > 0 ? Math.round((activeMsTotal / durationMs) * 100) : null,
    max_depth_pct: session.maxDepthPct != null ? session.maxDepthPct : null,
    dwell_ms_by_band: session.dwellByBand || null,
    page_count: pages.length,
    pages,
    click_count: clicks.length,
    rage_click_count: clicks.filter((c) => c && c.rage).length,
    viewport,
    page_height_px: pageHeight,
    // Geometry facts are meaningless without a viewport to measure against;
    // recording it lets an aggregate exclude such sessions rather than fold in
    // a fabricated zero.
    has_geometry: Boolean(viewport && pageHeight),
  };
}
