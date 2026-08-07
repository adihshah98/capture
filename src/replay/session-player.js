import { CapturePlayer } from '../player.js';
import { currentProject, fetchJson, projectQuery } from './api.js';
import { writeHash } from './deeplink.js';
import { formatBytes, hostPath } from './format.js';
import { clearSessionDetails, renderSessionDetails } from './session-details.js';
import { findSession } from './session-list.js';
import {
  currentSpeed,
  renderScrubberChrome,
  showAssetErrors,
  showSkipNote,
  skipEnabled,
  updatePlayState,
  updateScrubber,
} from './scrubber.js';
import { clearTimeline, renderTimeline, setSyncStart, syncTimeline } from './timeline.js';
import { resetAnalysis, showCachedAnalysis } from './analysis.js';

const stageEl = document.getElementById('stage');
const controlsEl = document.getElementById('controls');
const playerMsgEl = document.getElementById('player-msg');
const skipNoteEl = document.getElementById('skip-note');
const assetErrorsEl = document.getElementById('asset-errors');
const sessionTitleEl = document.getElementById('session-title');
const loadProgressEl = document.getElementById('load-progress');
const windowTabsEl = document.getElementById('window-tabs');

export let player = null;

let loadSeq = 0;
export let loadedSessionId = null;

let pendingSeekMs = null;
export function setPendingSeek(ms) {
  pendingSeekMs = ms;
}

export function clearPlayer() {
  loadSeq += 1;
  loadedSessionId = null;
  destroyPlayer();
  windowTabsEl.innerHTML = '';
  clearTimeline();
  resetAnalysis(null);
  clearSessionDetails();
  loadProgressEl.textContent = '';
  sessionTitleEl.textContent = 'Select a session';
  sessionTitleEl.title = '';
}

function showPlayerMessage(message) {
  destroyPlayer();
  playerMsgEl.textContent = message;
  playerMsgEl.hidden = false;
}

export async function loadSession(sessionId) {
  const seq = ++loadSeq;
  loadedSessionId = sessionId;
  writeHash({ p: currentProject, s: sessionId });
  sessionTitleEl.textContent = `Session ${sessionId}`;
  sessionTitleEl.title = '';
  showCachedAnalysis(sessionId);

  const session = await findSession(sessionId);
  if (seq !== loadSeq) return;
  renderSessionDetails(session);

  let manifest;
  try {
    manifest = await fetchJson(`/api/sessions/${sessionId}/chunks${projectQuery()}`);
  } catch (err) {
    if (seq !== loadSeq) return;
    showPlayerMessage(`Could not load this session. ${err.message}`);
    return;
  }
  if (seq !== loadSeq) return;
  if (manifest.url) {
    sessionTitleEl.textContent = `Session ${sessionId.slice(0, 8)}… — ${hostPath(manifest.url)}`;
    sessionTitleEl.title = manifest.url;
  }

  const byWindow = new Map();
  for (const chunk of Array.isArray(manifest.chunks) ? manifest.chunks : []) {
    const key = chunk.window ?? 'default';
    if (!byWindow.has(key)) byWindow.set(key, []);
    byWindow.get(key).push(chunk);
  }
  if (byWindow.size === 0) {
    showPlayerMessage('This session has no chunks to replay.');
    return;
  }
  const bytesOf = (chunks) => chunks.reduce((n, c) => n + c.bytes, 0);
  const order = session && session.order;
  const checkoutAt = order && (order.checkout_started_at || order.purchased_at);
  let checkoutWindow = null;
  if (checkoutAt) {
    let latest = -Infinity;
    for (const [key, chunks] of byWindow) {
      for (const chunk of chunks) {
        if (chunk.timestamp <= checkoutAt + 5000 && chunk.timestamp > latest) {
          latest = chunk.timestamp;
          checkoutWindow = key;
        }
      }
    }
  }
  const defaultWindow =
    checkoutWindow ??
    [...byWindow.entries()].sort((a, b) => bytesOf(b[1]) - bytesOf(a[1]))[0][0];

  windowTabsEl.innerHTML = '';
  if (byWindow.size > 1) {
    let n = 0;
    for (const [key, chunks] of byWindow) {
      n += 1;
      const btn = document.createElement('button');
      btn.textContent = `Tab ${n}`;
      btn.title =
        `${chunks.length} chunks · ${formatBytes(bytesOf(chunks))}` +
        (key === checkoutWindow ? ' · reached checkout from this tab' : '');
      btn.dataset.window = key;
      btn.addEventListener('click', () => {
        playWindow(sessionId, byWindow.get(key), key, ++loadSeq, session, key === defaultWindow);
      });
      windowTabsEl.appendChild(btn);
    }
  }

  playWindow(sessionId, byWindow.get(defaultWindow), defaultWindow, seq, session, true);
}

function dedupe(events) {
  const seen = new Set();
  return events.filter((e) => {
    const key = `${e.timestamp}:${e.type}:${JSON.stringify(e.data)}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function dropUnrenderable(events) {
  const out = [];
  let dead = true;
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    if (e.type === 2) {
      dead = false;
    } else if (e.type === 4) {
      const next = events.slice(i + 1).find((x) => x.type === 4 || x.type === 2);
      dead = !(next && next.type === 2);
      if (dead) continue;
    } else if (dead && e.type === 3) {
      continue;
    }
    out.push(e);
  }
  return out;
}

function collectAssetUrls(events) {
  const urls = new Set();
  const add = (u) => {
    if (typeof u !== 'string') return;
    if (u.startsWith('//')) u = `https:${u}`;
    if (u.startsWith('http')) urls.add(u);
  };
  const visit = (node) => {
    if (!node) return;
    const attrs = node.attributes;
    if (attrs) {
      const tag = (node.tagName || '').toLowerCase();
      if (tag === 'img') {
        add(attrs.src);
        if (typeof attrs.srcset === 'string') {
          for (const part of attrs.srcset.split(',')) add(part.trim().split(/\s+/)[0]);
        }
      } else if (tag === 'video') {
        add(attrs.poster);
      }
    }
    if (node.childNodes) for (const child of node.childNodes) visit(child);
  };
  for (const e of events) {
    if (e.type === 2) visit(e.data && e.data.node);
    else if (e.type === 3 && e.data && e.data.source === 0 && e.data.adds) {
      for (const a of e.data.adds) visit(a.node);
    }
  }
  return [...urls];
}

function preloadAssets(events, seq) {
  const queue = collectAssetUrls(events).slice(0, 300);
  let active = 0;
  const pump = () => {
    if (seq !== loadSeq) return;
    while (active < 4 && queue.length) {
      active += 1;
      const img = new Image();
      img.onload = img.onerror = () => {
        active -= 1;
        pump();
      };
      img.src = queue.shift();
    }
  };
  pump();
}

function destroyPlayer() {
  if (player) player.destroy();
  player = null;
  stageEl.innerHTML = '';
  stageEl.appendChild(skipNoteEl);
  stageEl.appendChild(playerMsgEl);
  skipNoteEl.hidden = true;
  playerMsgEl.hidden = true;
  controlsEl.hidden = true;
  assetErrorsEl.hidden = true;
}

async function playWindow(sessionId, chunks, windowKey, seq, session, showOrder) {
  for (const btn of windowTabsEl.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.window === windowKey);
  }
  destroyPlayer();

  let loaded = 0;
  loadProgressEl.textContent = `loading 0/${chunks.length} chunks…`;
  const fetchChunk = async (name) => {
    for (let attempt = 0; ; attempt++) {
      try {
        return await fetchJson(`/api/sessions/${sessionId}/chunks/${name}${projectQuery()}`);
      } catch (err) {
        if (attempt >= 2) throw err;
        await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
      }
    }
  };
  const results = await Promise.all(
    chunks.map((c) =>
      fetchChunk(c.name)
        .then((chunk) => {
          loadProgressEl.textContent = `loading ${++loaded}/${chunks.length} chunks…`;
          return chunk;
        })
        .catch(() => null)
    )
  );
  if (seq !== loadSeq) return;
  const missing = results.filter((r) => r === null).length;
  loadProgressEl.textContent = missing
    ? `${missing} chunk${missing > 1 ? 's' : ''} failed to load — replay may be incomplete`
    : '';

  const allEvents = [];
  const navigations = [];
  for (const chunk of results.filter(Boolean)) {
    const events = chunk.events || [];
    const previous = navigations.length ? navigations[navigations.length - 1].url : null;
    if (chunk.url && (!previous || chunk.url.split('#')[0] !== previous.split('#')[0])) {
      navigations.push({ url: chunk.url, t: events.length ? events[0].timestamp : chunk.received_at });
    }
    allEvents.push(...events);
  }
  allEvents.sort((a, b) => a.timestamp - b.timestamp);
  const playable = dropUnrenderable(dedupe(allEvents));

  if (allEvents.length < 2) {
    showPlayerMessage(
      results.includes(null) ? 'Could not load this session.' : 'Not enough events to replay yet.'
    );
    renderTimeline(allEvents, navigations, session, showOrder);
    return;
  }

  setSyncStart(allEvents[0].timestamp);
  renderTimeline(allEvents, navigations, session, showOrder);
  let lastHashWrite = 0;
  try {
    player = new CapturePlayer({
      events: playable,
      mount: stageEl,
      speed: currentSpeed(),
      skipInactive: skipEnabled(),
      onTime: (ms) => {
        updateScrubber(ms);
        syncTimeline(ms);
        const now = Date.now();
        if (now - lastHashWrite > 2000) {
          lastHashWrite = now;
          writeHash({ p: currentProject, s: sessionId, t: Math.floor(ms / 1000) });
        }
      },
      onState: updatePlayState,
      onSkip: showSkipNote,
      onAssetErrors: showAssetErrors,
    });
  } catch (err) {
    console.error('capture: replayer failed to start', err);
    showPlayerMessage('This recording could not be rendered — its event data is malformed.');
    return;
  }
  controlsEl.hidden = false;
  renderScrubberChrome(player);
  updateScrubber(0);
  if ('requestIdleCallback' in window) requestIdleCallback(() => preloadAssets(playable, seq));
  else setTimeout(() => preloadAssets(playable, seq), 1000);
  if (pendingSeekMs != null) {
    const at = Math.min(pendingSeekMs, player.duration - 1);
    pendingSeekMs = null;
    player.seekTo(at);
  }
}
