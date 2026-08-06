import { CapturePlayer } from '../player.js';
import { fetchJson, projectQuery } from './api.js';
import { formatBytes, hostPath } from './format.js';
import { allSessions } from './session-list.js';
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

export function clearPlayer() {
  loadSeq += 1;
  loadedSessionId = null;
  destroyPlayer();
  windowTabsEl.innerHTML = '';
  clearTimeline();
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
  sessionTitleEl.textContent = `Session ${sessionId}`;
  sessionTitleEl.title = '';

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
  const defaultWindow = [...byWindow.entries()].sort(
    (a, b) => bytesOf(b[1]) - bytesOf(a[1])
  )[0][0];

  windowTabsEl.innerHTML = '';
  if (byWindow.size > 1) {
    let n = 0;
    for (const [key, chunks] of byWindow) {
      n += 1;
      const btn = document.createElement('button');
      btn.textContent = `Tab ${n}`;
      btn.title = `${chunks.length} chunks · ${formatBytes(bytesOf(chunks))}`;
      btn.dataset.window = key;
      btn.addEventListener('click', () => {
        playWindow(sessionId, byWindow.get(key), key, ++loadSeq);
      });
      windowTabsEl.appendChild(btn);
    }
  }

  playWindow(sessionId, byWindow.get(defaultWindow), defaultWindow, seq);
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

async function playWindow(sessionId, chunks, windowKey, seq) {
  for (const btn of windowTabsEl.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.window === windowKey);
  }
  destroyPlayer();

  let loaded = 0;
  loadProgressEl.textContent = `loading 0/${chunks.length} chunks…`;
  const results = await Promise.all(
    chunks.map((c) =>
      fetchJson(`/api/sessions/${sessionId}/chunks/${c.name}${projectQuery()}`)
        .then((chunk) => {
          loadProgressEl.textContent = `loading ${++loaded}/${chunks.length} chunks…`;
          return chunk;
        })
        .catch(() => null)
    )
  );
  if (seq !== loadSeq) return;
  loadProgressEl.textContent = '';

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

  const session = allSessions.find((s) => s.session_id === sessionId);
  if (allEvents.length < 2) {
    showPlayerMessage(
      results.includes(null) ? 'Could not load this session.' : 'Not enough events to replay yet.'
    );
    renderTimeline(allEvents, navigations, session);
    return;
  }

  setSyncStart(allEvents[0].timestamp);
  renderTimeline(allEvents, navigations, session);
  player = new CapturePlayer({
    events: allEvents,
    mount: stageEl,
    speed: currentSpeed(),
    skipInactive: skipEnabled(),
    onTime: (ms) => {
      updateScrubber(ms);
      syncTimeline(ms);
    },
    onState: updatePlayState,
    onSkip: showSkipNote,
    onAssetErrors: showAssetErrors,
  });
  controlsEl.hidden = false;
  renderScrubberChrome(player);
  updateScrubber(0);
}
