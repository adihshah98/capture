import { readFileSync, mkdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { chromium } from 'playwright';

const SCROLL_SOURCE = 3;
const NAV_TIMEOUT_MS = 30000;
const SETTLE_MS = 1200;

// Mirrors ACTIVE_SOURCES in src/player.js: the incremental sources that mean a
// human was doing something, as opposed to the page mutating on its own.
const ACTIVE_SOURCES = new Set([1, 2, 3, 4, 5, 6, 7, 12]);

// A gap longer than this between active events is dead air. Same threshold the
// replay UI uses to offer skip-ahead, so "active" means the same thing in both.
const ACTIVITY_THRESHOLD_MS = 5000;

// Seeking before the first full snapshot renders nothing, and the player
// clamps to it anyway; give the rebuild a moment to settle before sampling.
const FIRST_FRAME_LEAD_MS = 500;

// Stay off the very end: seekTo within 50ms of duration routes to finish().
const END_MARGIN_MS = 100;

// The player re-applies recorded scroll at 400ms; shoot after that plus reflow.
const SEEK_SETTLE_MS = 550;
const IMAGE_SETTLE_CAP_MS = 600;
const JPEG_QUALITY = 72;

// fps multiplies directly into the frame count, and every frame is a full
// screenshot sent to the model. A mistyped env var should degrade the sampling
// rate, not turn one session into thousands of images: 50fps yields ~10k frames
// for a 200s recording. The band spans one frame every 5s to 4 per second.
const MIN_FPS = 0.2;
const MAX_FPS = 4;

// Two rrweb rules the player depends on, inlined rather than loaded from the
// emitted stylesheet: deploy.sh ships only extract.mjs and the JS bundle, so a
// file dependency would work locally and break in production.
const STAGE_CSS = `
  html, body { margin: 0; padding: 0; }
  #stage { position: relative; overflow: hidden; }
  #stage iframe { border: 0; }
  .replayer-wrapper { position: relative; }
  .replayer-mouse-tail { position: absolute; pointer-events: none; }
  /* rescale() writes an inline transform; it must not shift the screenshot. */
  #stage .replayer-wrapper {
    transform: none !important; position: relative !important;
    top: 0 !important; left: 0 !important;
  }
  /* Replay-UI chrome that was never on the visitor's screen. */
  .replayer-mouse, .replayer-mouse-tail, .click-pulse { display: none !important; }
`;

/**
 * Parse the session's chunks, reporting the ones that could not be read.
 *
 * A dropped chunk does not leave a visible hole: collectEvents sorts whatever
 * parsed, so a missing middle chunk reads as continuous, and activeSpans then
 * treats the gap as legitimate dead air and skips it. The result is a partial
 * session that looks complete — so the count is returned and surfaced as a
 * warning rather than swallowed.
 */
function readChunks(sessionDir, names) {
  const chunks = [];
  const failed = [];
  for (const name of names) {
    try {
      chunks.push(JSON.parse(readFileSync(join(sessionDir, name), 'utf8')));
    } catch (err) {
      failed.push({ name, message: String((err && err.message) || err) });
    }
  }
  return { chunks, failed };
}

export function collectEvents(chunks) {
  const events = [];
  for (const chunk of chunks) events.push(...(chunk.events || []));
  events.sort((a, b) => a.timestamp - b.timestamp);
  return events;
}

export function idleWindows(events) {
  const windows = [];
  let start = null;
  for (const e of events) {
    const tag = e.type === 5 && e.data && e.data.tag;
    if (tag === 'capture-idle') start = e.timestamp;
    else if (tag === 'capture-idle-end' && start != null) {
      windows.push([start, e.timestamp]);
      start = null;
    }
  }
  if (start != null) windows.push([start, Infinity]);
  return windows;
}

export function activeMs(from, to, idle) {
  let ms = Math.max(0, to - from);
  for (const [s, e] of idle) {
    const overlap = Math.min(to, e) - Math.max(from, s);
    if (overlap > 0) ms -= overlap;
  }
  return Math.max(0, ms);
}

export function viewportOf(events) {
  let vp = null;
  for (const e of events) {
    if (e.type === 4 && e.data && e.data.width) vp = { width: e.data.width, height: e.data.height };
    else if (e.type === 3 && e.data && e.data.source === 4 && e.data.width) {
      vp = { width: e.data.width, height: e.data.height };
    }
  }
  return vp;
}

export function scrollTimeline(events) {
  const points = [];
  for (const e of events) {
    if (e.type === 2 && e.data && e.data.initialOffset) {
      points.push({ t: e.timestamp, y: e.data.initialOffset.top || 0 });
    } else if (e.type === 3 && e.data && e.data.source === SCROLL_SOURCE) {
      points.push({ t: e.timestamp, y: e.data.y || 0 });
    }
  }
  points.sort((a, b) => a.t - b.t);
  return points;
}

export function clicksOf(events) {
  const out = [];
  for (const e of events) {
    if (e.type !== 5 || !e.data || e.data.tag !== 'capture-click') continue;
    const p = e.data.payload || {};
    out.push({
      t: e.timestamp,
      selector: p.selector || '',
      text: p.text || '',
      tag: p.tag || '',
      x: p.x,
      y: p.y,
      image: p.image || null,
      rage: !!p.rage,
    });
  }
  return out;
}

/**
 * Stretches where something was actually happening, as offsets from the first
 * event. Derived from gaps between active events rather than from the
 * capture-idle markers: those are only emitted when a non-interactive event
 * happens to arrive after the idle threshold, so a tab left parked with
 * nothing firing produces no marker at all. Real recordings frequently have
 * none. Marker-derived windows are subtracted on top when they do exist.
 */
export function activeSpans(events, idle = []) {
  if (!events.length) return [];
  const t0 = events[0].timestamp;
  const spans = [];
  let span = null;
  for (const e of events) {
    const active =
      e.type === 2 ||
      e.type === 4 ||
      (e.type === 3 && e.data && ACTIVE_SOURCES.has(e.data.source));
    if (!active) continue;
    const t = e.timestamp - t0;
    if (span && t - span.end <= ACTIVITY_THRESHOLD_MS) span.end = t;
    else {
      if (span) spans.push(span);
      span = { start: t, end: t };
    }
  }
  if (span) spans.push(span);

  if (!idle.length) return spans;

  // Carve marker-reported idle out of the spans; a span can split in two.
  let out = spans;
  for (const [s, e] of idle) {
    const from = s - t0;
    const to = (e === Infinity ? events[events.length - 1].timestamp : e) - t0;
    const next = [];
    for (const sp of out) {
      if (to <= sp.start || from >= sp.end) next.push(sp);
      else {
        if (from > sp.start) next.push({ start: sp.start, end: from });
        if (to < sp.end) next.push({ start: to, end: sp.end });
      }
    }
    out = next;
  }
  return out;
}

/**
 * Timestamps to screenshot, ascending. One frame per second of active time —
 * dead air is skipped, since those frames would be pixel-identical to their
 * neighbours. Pure and deterministic so the choice of frames is unit-testable
 * without a browser.
 */
export function pickFrameTimes(events, { fps = 1, maxFrames = 0 } = {}) {
  if (!events || events.length < 2) return events && events.length ? [0] : [];

  const t0 = events[0].timestamp;
  const duration = events[events.length - 1].timestamp - t0;
  const last = Math.max(0, duration - END_MARGIN_MS);
  // A non-finite or non-positive fps means "unset", not "as fast as possible".
  const wanted = Number.isFinite(fps) && fps > 0 ? fps : 1;
  const step = Math.max(1, Math.round(1000 / Math.min(Math.max(wanted, MIN_FPS), MAX_FPS)));

  const firstSnapshot = events.find((e) => e.type === 2);
  const earliest = firstSnapshot
    ? Math.min(firstSnapshot.timestamp - t0 + FIRST_FRAME_LEAD_MS, last)
    : 0;

  const times = new Set([earliest]);
  for (const span of activeSpans(events, idleWindows(events))) {
    for (let t = Math.max(span.start, earliest); t < span.end; t += step) {
      times.add(Math.min(t, last));
    }
    // The tail of a span is where a scroll or click came to rest; a span
    // shorter than one step would otherwise contribute nothing.
    times.add(Math.min(Math.max(span.end, earliest), last));
  }

  const sorted = [...times].filter((t) => t >= 0 && t <= last).sort((a, b) => a - b);
  if (maxFrames > 0 && sorted.length > maxFrames) {
    const stride = sorted.length / maxFrames;
    const thinned = [];
    for (let i = 0; i < maxFrames; i++) thinned.push(sorted[Math.floor(i * stride)]);
    return [...new Set(thinned)];
  }
  return sorted;
}

/** Page-side bootstrap: build the real replay player and hold it at time 0. */
async function startPlayer(page, events) {
  await page.evaluate(({ events }) => {
    const player = new window.captureAnalyze.CapturePlayer({
      events,
      mount: document.getElementById('stage'),
      speed: 1,
      skipInactive: false,
    });
    // The constructor starts playing; stop it before the rAF loop can advance
    // past a seek target or skip ahead on its own.
    player.pause(0);
    window.__player = player;
  }, { events });
  await page.waitForTimeout(SETTLE_MS);
}

/**
 * Wait for images to decode, but never let a dead origin stall the run.
 *
 * Best effort by design: a failure here only means the next screenshot may
 * catch an image mid-decode, which the failed-asset count already reports.
 * The frame is still captured, so this is the one swallowed error that does
 * not need to reach the digest.
 */
async function settleImages(page, capMs = IMAGE_SETTLE_CAP_MS) {
  try {
    await page.evaluate(
      (cap) =>
        new Promise((resolve) => {
          const done = setTimeout(resolve, cap);
          const doc = document.querySelector('#stage iframe')?.contentDocument;
          if (!doc) return resolve(clearTimeout(done));
          const pending = [...doc.querySelectorAll('img')].filter((i) => !i.complete);
          if (!pending.length) return resolve(clearTimeout(done));
          let left = pending.length;
          const tick = () => {
            if (--left <= 0) {
              clearTimeout(done);
              resolve();
            }
          };
          for (const img of pending) {
            img.addEventListener('load', tick, { once: true });
            img.addEventListener('error', tick, { once: true });
          }
        }),
      capMs
    );
  } catch {}
}

/**
 * Screenshot the replay at each timestamp. Seeks strictly forward: rrweb
 * applies incremental mutations from the nearest preceding snapshot, so going
 * backwards would force a full DOM rebuild every frame.
 */
async function captureFrames(page, times, outDir) {
  const frames = [];
  let dropped = 0;
  for (let i = 0; i < times.length; i++) {
    const atMs = times[i];
    try {
      await page.evaluate((t) => window.__player.seekTo(t, { play: false }), atMs);
      // CapturePlayer re-applies the recorded scroll at 90ms and 400ms; a shot
      // taken before that lands shows the wrong offset.
      await page.waitForTimeout(SEEK_SETTLE_MS);
      await page.evaluate(
        () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))
      );
      await settleImages(page);

      const frame = await page.$('#stage iframe');
      if (!frame) {
        dropped++;
        continue;
      }
      const file = join(outDir, `f${String(i).padStart(4, '0')}.jpg`);
      await frame.screenshot({ path: file, type: 'jpeg', quality: JPEG_QUALITY });
      // An empty or truncated JPEG passes the is_file() check on the Python
      // side and is then sent to the model, where it fails the whole request
      // for this session with an opaque error. Cheaper to drop it here.
      if (statSync(file, { throwIfNoEntry: false })?.size > 0) {
        frames.push({ atMs, file });
      } else {
        dropped++;
      }
    } catch {
      dropped++;
    }
  }
  return { frames, dropped };
}

export async function extractSession({
  sessionDir,
  chunkNames,
  outDir,
  bundlePath,
  fps = 1,
  maxFrames = 0,
}) {
  const { chunks, failed: failedChunks } = readChunks(sessionDir, chunkNames);
  // Every chunk unreadable is a broken recording, not a thin one: analyzing
  // nothing would report "not enough events" and hide the real cause.
  if (!chunks.length && (chunkNames || []).length) {
    return {
      error:
        `none of the ${chunkNames.length} chunk(s) could be read — ` +
        `first failure: ${failedChunks[0]?.message || 'unknown error'}`,
    };
  }
  const events = collectEvents(chunks);
  if (events.length < 2) {
    return { error: 'not enough events to analyze', events: events.length };
  }

  const viewport = viewportOf(events) || { width: 1280, height: 800 };
  const idle = idleWindows(events);
  const scroll = scrollTimeline(events);
  const clicks = clicksOf(events);
  const startTs = events[0].timestamp;
  const endTs = events[events.length - 1].timestamp;

  const urls = [];
  for (const c of chunks) {
    if (c.url && (!urls.length || urls[urls.length - 1] !== c.url)) urls.push(c.url);
  }

  mkdirSync(outDir, { recursive: true });

  const times = pickFrameTimes(events, { fps, maxFrames });

  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  let frames = [];
  let droppedFrames = 0;
  let pageHeight = 0;
  let failedAssets = 0;
  // null means "could not be measured", which a plain 0 cannot express.
  let measuredHeight = null;
  try {
    const page = await browser.newPage({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: 1,
    });
    page.setDefaultTimeout(NAV_TIMEOUT_MS);
    await page.setContent(
      `<!doctype html><meta charset="utf-8"><style>${STAGE_CSS}
       #stage{width:${viewport.width}px;height:${viewport.height}px}
       #stage iframe{width:${viewport.width}px;height:${viewport.height}px}
       </style><div id="stage"></div>`
    );
    await page.addScriptTag({ path: bundlePath });
    await startPlayer(page, events);

    ({ frames, dropped: droppedFrames } = await captureFrames(page, times, outDir));

    measuredHeight = await page
      .evaluate(() => {
        const doc = document.querySelector('#stage iframe')?.contentDocument;
        if (!doc) return null;
        return Math.max(doc.body?.scrollHeight || 0, doc.documentElement?.scrollHeight || 0);
      })
      .catch(() => null);
    pageHeight = measuredHeight ?? 0;
    failedAssets = await page
      .evaluate(() => window.__player?.failedAssets?.size || 0)
      .catch(() => 0);
  } finally {
    await browser.close();
  }

  const totalMs = endTs - startTs;
  const idleMs = idle.reduce(
    (n, [s, e]) => n + Math.max(0, Math.min(e === Infinity ? endTs : e, endTs) - s),
    0
  );
  const maxScrollPx = scroll.reduce((n, p) => Math.max(n, p.y || 0), 0);

  const digest = {
    session: {
      durationMs: totalMs,
      activeMs: Math.max(0, totalMs - idleMs),
      idleMs,
      viewport,
      pageHeight,
      maxScrollPx,
      urls,
      eventCount: events.length,
      frameCount: frames.length,
      fps,
    },
    clicks: clicks.map((c) => ({
      atMs: c.t - startTs,
      text: c.text,
      selector: c.selector,
      tag: c.tag,
      image: c.image,
      rage: c.rage,
    })),
    warnings: [
      ...(failedAssets
        ? [`${failedAssets} asset(s) failed to load during replay — parts of the frames may be missing`]
        : []),
      // A partly-read recording still produces a plausible-looking digest, so
      // say so plainly: the model is drawing behavioural conclusions from it.
      ...(failedChunks.length
        ? [
            `${failedChunks.length} of ${chunkNames.length} recording chunk(s) could not be read ` +
              `(${failedChunks[0].name}: ${failedChunks[0].message}) — this session is incomplete ` +
              'and gaps may look like inactivity',
          ]
        : []),
      ...(droppedFrames
        ? [
            `${droppedFrames} of ${times.length} frame(s) failed to capture — ` +
              'the replay is sampled less densely than usual',
          ]
        : []),
      ...(measuredHeight === null
        ? ['page height could not be measured from the replay']
        : []),
      ...(frames.length === 0 ? ['no frames could be captured from the replay'] : []),
    ],
    frames,
  };

  const digestPath = join(outDir, 'digest.json');
  writeFileSync(digestPath, JSON.stringify(digest, null, 2));
  return digest;
}

/**
 * CLI args for the extractor subprocess.
 *
 * Positional layout used to be `sessionDir outDir bundlePath fps maxFrames
 * ...chunks`. Inserting one arg on either side made the first chunk name land
 * in `maxFrames`: Number('1700…json') is NaN, NaN || 0 is 0, so the frame cap
 * silently disabled and that chunk vanished. Named flags keep the variadic
 * chunk list from ever being read as numbers.
 */
export function parseFiniteNumber(raw, name) {
  const n = Number(raw);
  if (!Number.isFinite(n)) {
    throw new Error(`${name} must be a finite number, got ${JSON.stringify(raw)}`);
  }
  return n;
}

export function parseExtractorArgs(argv) {
  const positional = [];
  let fps = 1;
  let maxFrames = 0;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--fps') {
      const next = argv[++i];
      if (next == null) throw new Error('--fps requires a value');
      fps = parseFiniteNumber(next, '--fps');
      continue;
    }
    if (arg === '--max-frames') {
      const next = argv[++i];
      if (next == null) throw new Error('--max-frames requires a value');
      maxFrames = parseFiniteNumber(next, '--max-frames');
      continue;
    }
    if (typeof arg === 'string' && arg.startsWith('--')) {
      throw new Error(`unknown flag: ${arg}`);
    }
    positional.push(arg);
  }
  const [sessionDir, outDir, bundlePath, ...chunkNames] = positional;
  if (!sessionDir || !outDir || !bundlePath) {
    throw new Error(
      'usage: extract.mjs <sessionDir> <outDir> <bundlePath> [--fps N] [--max-frames N] [chunks...]'
    );
  }
  return { sessionDir, outDir, bundlePath, fps, maxFrames, chunkNames };
}

if (process.argv[1] && process.argv[1].endsWith('extract.mjs')) {
  let opts;
  try {
    opts = parseExtractorArgs(process.argv.slice(2));
  } catch (err) {
    process.stderr.write(String((err && err.message) || err) + '\n');
    process.exit(1);
  }
  extractSession(opts)
    .then((d) => {
      process.stdout.write(JSON.stringify(d));
    })
    .catch((err) => {
      process.stderr.write(String((err && err.stack) || err));
      process.exit(1);
    });
}
