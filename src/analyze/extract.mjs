import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { chromium } from 'playwright';

const SECTION_SELECTOR = 'h1,h2,h3,section,article';
const TRUST_HINT = /review|rating|verified|guarantee|certified|doctor|clinical|study|award|money.?back|refund|secure/i;
const SCROLL_SOURCE = 3;
const MIN_REGION_DWELL_MS = 2000;
const MAX_SHOTS = 6;
const NAV_TIMEOUT_MS = 30000;
const SETTLE_MS = 1200;

const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

function readChunks(sessionDir, names) {
  const chunks = [];
  for (const name of names) {
    try {
      chunks.push(JSON.parse(readFileSync(join(sessionDir, name), 'utf8')));
    } catch {}
  }
  return chunks;
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

export function dwellBySection(scroll, sections, viewport, idle, endTs) {
  if (!sections.length || !viewport) return [];
  const totals = new Map();
  for (let i = 0; i < scroll.length; i++) {
    const from = scroll[i].t;
    const to = i + 1 < scroll.length ? scroll[i + 1].t : endTs;
    const ms = activeMs(from, to, idle);
    if (ms <= 0) continue;
    const top = scroll[i].y;
    const bottom = top + viewport.height;
    for (const s of sections) {
      const visible = Math.min(bottom, s.bottom) - Math.max(top, s.top);
      if (visible > 0) totals.set(s.id, (totals.get(s.id) || 0) + ms);
    }
  }
  return sections
    .map((s) => ({ ...s, dwellMs: Math.round(totals.get(s.id) || 0) }))
    .filter((s) => s.dwellMs > 0)
    .sort((a, b) => b.dwellMs - a.dwellMs);
}

function attributeClicks(clicks, sections, scroll, viewport) {
  if (!viewport) return;
  const scrollAt = (t) => {
    let y = 0;
    for (const p of scroll) {
      if (p.t > t) break;
      y = p.y;
    }
    return y;
  };
  for (const c of clicks) {
    if (typeof c.y !== 'number') continue;
    const pageY = scrollAt(c.t) + c.y;
    const hit = sections.find((s) => pageY >= s.top && pageY < s.bottom);
    if (hit) c.section = hit.label;
  }
}

async function measure(page, events) {
  return page.evaluate(
    ({ events, selector, trustSource }) =>
      new Promise((resolve) => {
        const trust = new RegExp(trustSource, 'i');
        const mount = document.getElementById('stage');
        const replayer = new window.rrweb.Replayer(events, {
          root: mount,
          speed: 1,
          skipInactive: false,
          useVirtualDom: false,
          triggerFocus: false,
          mouseTail: false,
        });
        replayer.pause(Math.max(0, events[events.length - 1].timestamp - events[0].timestamp));

        setTimeout(() => {
          const doc = replayer.iframe.contentDocument;
          if (!doc || !doc.body) return resolve({ sections: [], assetErrors: 0, docHeight: 0 });

          // Headings name a band; container elements (section/article) only
          // contribute when no heading sits inside them, otherwise the
          // container and its own heading both become bands covering the
          // same pixels and dwell gets double counted.
          const nodes = [...doc.querySelectorAll(selector)];
          const raw = [];
          for (const el of nodes) {
            const tag = el.tagName.toLowerCase();
            const isHeading = tag === 'h1' || tag === 'h2' || tag === 'h3';
            if (!isHeading && el.querySelector('h1,h2,h3')) continue;
            const r = el.getBoundingClientRect();
            if (r.height <= 0) continue;
            const top = r.top + (doc.documentElement.scrollTop || doc.body.scrollTop || 0);
            const source = isHeading ? el : el.querySelector('h1,h2,h3,[class*=title],[class*=heading]') || el;
            const label = (source.innerText || source.textContent || '')
              .replace(/\s+/g, ' ')
              // rrweb replaces masked input values with runs of asterisks;
              // they carry no meaning and must not reach the model.
              .replace(/\*{2,}/g, ' ')
              .replace(/\s+/g, ' ')
              .trim()
              .slice(0, 90);
            if (!label) continue;
            raw.push({ top: Math.round(top), height: Math.round(r.height), label, tag });
          }
          raw.sort((a, b) => a.top - b.top);

          const seen = new Set();
          const sections = [];
          for (const s of raw) {
            if (seen.has(s.label)) continue;
            seen.add(s.label);
            sections.push(s);
          }

          const docHeight = Math.max(
            doc.body.scrollHeight || 0,
            doc.documentElement.scrollHeight || 0
          );

          const out = sections.map((s, i) => {
            const next = sections[i + 1];
            const bottom = next ? Math.max(next.top, s.top + s.height) : s.top + s.height;
            const band = { top: s.top, bottom: Math.max(bottom, s.top + s.height) };
            const images = [];
            for (const img of doc.querySelectorAll('img')) {
              const r = img.getBoundingClientRect();
              const y = r.top + (doc.documentElement.scrollTop || doc.body.scrollTop || 0);
              if (y >= band.top && y < band.bottom && r.width > 40 && r.height > 40) {
                images.push({
                  src: (img.currentSrc || img.src || '').slice(0, 300),
                  alt: (img.getAttribute('alt') || '').slice(0, 120),
                  loaded: img.complete && img.naturalWidth > 0,
                });
              }
            }
            return {
              id: `s${i}`,
              label: s.label,
              tag: s.tag,
              top: band.top,
              bottom: band.bottom,
              images: images.slice(0, 4),
              trustSignal: trust.test(s.label),
            };
          });

          let assetErrors = 0;
          for (const img of doc.querySelectorAll('img')) {
            if (img.complete && img.naturalWidth === 0) assetErrors += 1;
          }
          resolve({ sections: out, assetErrors, docHeight });
        }, 1200);
      }),
    { events, selector: SECTION_SELECTOR, trustSource: TRUST_HINT.source }
  );
}

async function shootRegions(page, regions, viewport, outDir) {
  const shots = [];
  for (const region of regions) {
    const height = clamp(region.bottom - region.top, 80, viewport.height * 2);
    try {
      await page.evaluate(
        ({ top }) => {
          const f = document.querySelector('#stage iframe');
          if (f && f.contentWindow) f.contentWindow.scrollTo({ top, behavior: 'auto' });
        },
        { top: region.top }
      );
      await page.waitForTimeout(350);
      const frame = await page.$('#stage iframe');
      if (!frame) continue;
      const file = join(outDir, `${region.id}.jpg`);
      await frame.screenshot({
        path: file,
        type: 'jpeg',
        quality: 72,
        clip: { x: 0, y: 0, width: viewport.width, height: Math.min(height, viewport.height) },
      });
      shots.push({ id: region.id, label: region.label, file });
    } catch {}
  }
  return shots;
}

export async function extractSession({ sessionDir, chunkNames, outDir, bundlePath }) {
  const chunks = readChunks(sessionDir, chunkNames);
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

  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  let measured = { sections: [], assetErrors: 0, docHeight: 0 };
  let shots = [];
  try {
    const page = await browser.newPage({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: 1,
    });
    page.setDefaultTimeout(NAV_TIMEOUT_MS);
    await page.setContent(
      `<!doctype html><meta charset="utf-8"><style>html,body{margin:0;padding:0}
       #stage{position:relative;width:${viewport.width}px;height:${viewport.height}px;overflow:hidden}
       #stage iframe{border:0;width:${viewport.width}px;height:${viewport.height}px}
       .replayer-wrapper{position:static!important;transform:none!important}
       .replayer-mouse,.replayer-mouse-tail{display:none!important}</style><div id="stage"></div>`
    );
    await page.addScriptTag({ path: bundlePath });
    await page.waitForTimeout(SETTLE_MS);
    measured = await measure(page, events);

    const dwell = dwellBySection(scroll, measured.sections, viewport, idle, endTs);
    const top = dwell.filter((s) => s.dwellMs >= MIN_REGION_DWELL_MS).slice(0, MAX_SHOTS);
    if (top.length) shots = await shootRegions(page, top, viewport, outDir);
  } finally {
    await browser.close();
  }

  const sections = dwellBySection(scroll, measured.sections, viewport, idle, endTs);
  attributeClicks(clicks, measured.sections, scroll, viewport);

  const totalMs = endTs - startTs;
  const idleMs = idle.reduce(
    (n, [s, e]) => n + Math.max(0, Math.min(e === Infinity ? endTs : e, endTs) - s),
    0
  );

  const digest = {
    session: {
      durationMs: totalMs,
      activeMs: Math.max(0, totalMs - idleMs),
      idleMs,
      viewport,
      pageHeight: measured.docHeight,
      urls,
      eventCount: events.length,
    },
    sections: sections.map((s) => ({
      label: s.label,
      dwellMs: s.dwellMs,
      top: s.top,
      bottom: s.bottom,
      trustSignal: s.trustSignal,
      images: s.images,
    })),
    clicks: clicks.map((c) => ({
      atMs: c.t - startTs,
      text: c.text,
      selector: c.selector,
      tag: c.tag,
      section: c.section || null,
      image: c.image,
      rage: c.rage,
    })),
    warnings: [
      ...(measured.assetErrors
        ? [`${measured.assetErrors} image(s) failed to load during reconstruction — visual descriptions for those regions are unreliable`]
        : []),
      ...(measured.sections.length === 0
        ? ['no headings/sections were found in the reconstructed DOM — dwell could not be attributed to named sections']
        : []),
    ],
    screenshots: shots,
  };

  const digestPath = join(outDir, 'digest.json');
  writeFileSync(digestPath, JSON.stringify(digest, null, 2));
  return digest;
}

if (process.argv[1] && process.argv[1].endsWith('extract.mjs')) {
  const [, , sessionDir, outDir, bundlePath, ...chunkNames] = process.argv;
  extractSession({ sessionDir, outDir, bundlePath, chunkNames })
    .then((d) => {
      process.stdout.write(JSON.stringify(d));
    })
    .catch((err) => {
      process.stderr.write(String((err && err.stack) || err));
      process.exit(1);
    });
}
