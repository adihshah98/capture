/**
 * replay.bundle.js — the replay UI.
 *
 * Left: recorded sessions. Center: rrweb-player. Bottom: console logs pulled
 * from the rrweb console plugin events (type 6 / plugin "rrweb/console@1"),
 * the lightweight version of PostHog's synced DevTools panel.
 */
import rrwebPlayer from 'rrweb-player';
import 'rrweb-player/dist/style.css';

const sessionListEl = document.getElementById('session-list');
const playerHostEl = document.getElementById('player-host');
const consoleListEl = document.getElementById('console-list');
const networkListEl = document.getElementById('network-list');
const sessionTitleEl = document.getElementById('session-title');
const loadProgressEl = document.getElementById('load-progress');
const windowTabsEl = document.getElementById('window-tabs');

// Console/Network tab switching
const tabs = [
  { btn: document.getElementById('tab-console'), list: consoleListEl },
  { btn: document.getElementById('tab-network'), list: networkListEl },
];
for (const tab of tabs) {
  tab.btn.addEventListener('click', () => {
    for (const t of tabs) {
      t.btn.classList.toggle('active', t === tab);
      t.list.hidden = t !== tab;
    }
  });
}

let player = null;

function formatTime(ms) {
  return new Date(ms).toLocaleString();
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function loadSessions() {
  const res = await fetch('/api/sessions');
  const { sessions } = await res.json();

  sessionListEl.innerHTML = '';
  if (sessions.length === 0) {
    sessionListEl.innerHTML =
      '<li class="empty">No sessions yet. Open the demo page and click around.</li>';
    return;
  }

  for (const session of sessions) {
    const li = document.createElement('li');
    li.innerHTML = `
      <span class="sid">${session.session_id.slice(0, 8)}…</span>
      <span class="meta">${formatTime(session.last_seen)} · ${session.chunk_count} chunks · ${formatBytes(session.bytes)}</span>
    `;
    li.addEventListener('click', () => {
      document.querySelectorAll('#session-list li').forEach((el) => el.classList.remove('active'));
      li.classList.add('active');
      loadSession(session.session_id);
    });
    sessionListEl.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Playhead sync: panel rows track the replay position — past rows normal,
// the latest-fired row highlighted, future rows dimmed. Click a row to seek.
// ---------------------------------------------------------------------------

let consoleRows = [];
let networkRows = [];
let syncStart = 0; // absolute timestamp of the session's first event

function seekTo(timestamp) {
  if (player) player.goto(Math.max(0, timestamp - syncStart));
}

function syncList(rows, currentMs) {
  let currentIdx = -1;
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].t - syncStart <= currentMs) currentIdx = i;
    else break;
  }
  if (rows.lastIdx === currentIdx) return; // nothing moved since last frame
  rows.lastIdx = currentIdx;
  rows.forEach((row, i) => {
    row.el.classList.toggle('current', i === currentIdx);
    row.el.classList.toggle('future', i > currentIdx);
  });
  if (currentIdx >= 0 && !rows[currentIdx].el.parentElement.hidden) {
    rows[currentIdx].el.scrollIntoView({ block: 'nearest' });
  }
}

function attachTimeSync() {
  if (!player) return;
  syncStart = player.getMetaData().startTime;
  syncList(consoleRows, 0);
  syncList(networkRows, 0);
  player.addEventListener('ui-update-current-time', ({ payload }) => {
    syncList(consoleRows, payload);
    syncList(networkRows, payload);
  });
}

function renderConsole(events) {
  const logs = events
    .filter((e) => e.type === 6 && e.data && e.data.plugin === 'rrweb/console@1')
    .map((e) => ({ timestamp: e.timestamp, ...e.data.payload }));

  document.getElementById('console-count').textContent = logs.length ? `(${logs.length})` : '';
  consoleRows = [];
  consoleListEl.innerHTML = '';
  if (logs.length === 0) {
    consoleListEl.innerHTML = '<li class="empty">No console output captured.</li>';
    return;
  }
  for (const log of logs) {
    const li = document.createElement('li');
    li.className = `level-${log.level}`;
    const args = (log.payload || [])
      .map((p) => {
        try {
          return typeof p === 'string' ? p.replace(/^"|"$/g, '') : JSON.stringify(p);
        } catch {
          return String(p);
        }
      })
      .join(' ');
    li.innerHTML = `<span class="ts">${new Date(log.timestamp).toLocaleTimeString()}</span><span class="lvl">${log.level}</span><span class="msg"></span>`;
    li.querySelector('.msg').textContent = args;
    li.addEventListener('click', () => seekTo(log.timestamp));
    consoleRows.push({ el: li, t: log.timestamp });
    consoleListEl.appendChild(li);
  }
}

function renderNetwork(events) {
  const t0 = events.length ? events[0].timestamp : 0;
  const requests = events
    .filter((e) => e.type === 6 && e.data && e.data.plugin === 'capture/network@1')
    .map((e) => ({ ...e.data.payload, startMs: e.data.payload.startMs || e.timestamp }))
    .sort((a, b) => a.startMs - b.startMs);

  document.getElementById('network-count').textContent = requests.length
    ? `(${requests.length})`
    : '';
  networkRows = [];
  networkListEl.innerHTML = '';
  if (requests.length === 0) {
    networkListEl.innerHTML = '<li class="empty">No network requests captured.</li>';
    return;
  }
  for (const req of requests) {
    const li = document.createElement('li');
    const failed = req.status === 0 || req.status >= 400;
    if (failed) li.className = 'net-error';
    const rel = Math.max(0, (req.startMs || t0) - t0) / 1000;
    li.innerHTML = `
      <span class="ts"></span>
      <span class="method"></span>
      <span class="st"></span>
      <span class="url"></span>
      <span class="kind"></span>
      <span class="dur"></span>
    `;
    li.querySelector('.ts').textContent = `+${rel.toFixed(1)}s`;
    li.querySelector('.method').textContent = req.method || req.initiatorType || '—';
    li.querySelector('.st').textContent = req.status != null ? req.status : '·';
    const url = li.querySelector('.url');
    url.textContent = req.url;
    url.title = req.url + (req.error ? ` — ${req.error}` : '');
    li.querySelector('.kind').textContent = req.type === 'resource' ? req.initiatorType : req.type;
    li.querySelector('.dur').textContent =
      req.durationMs != null ? `${req.durationMs} ms` : '';
    li.addEventListener('click', () => seekTo(req.startMs));
    networkRows.push({ el: li, t: req.startMs });
    networkListEl.appendChild(li);
  }
}

// Guards the async chunk loop: bumped whenever the user picks a session,
// so a stale load loop stops instead of feeding events to the wrong player.
let loadSeq = 0;

async function loadSession(sessionId) {
  const seq = ++loadSeq;
  sessionTitleEl.textContent = `Session ${sessionId}`;

  const res = await fetch(`/api/sessions/${sessionId}/chunks`);
  if (seq !== loadSeq) return;
  const manifest = await res.json();
  if (manifest.url) {
    sessionTitleEl.textContent = `Session ${sessionId.slice(0, 8)}… — ${manifest.url}`;
  }

  // A multi-tab session is several independent rrweb streams (one full
  // snapshot + incrementals per tab); interleaving them would garble the
  // DOM, so each window plays separately behind a tab picker.
  const byWindow = new Map();
  for (const chunk of manifest.chunks) {
    const key = chunk.window ?? 'default';
    if (!byWindow.has(key)) byWindow.set(key, []);
    byWindow.get(key).push(chunk);
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

async function playWindow(sessionId, chunks, windowKey, seq) {
  for (const btn of windowTabsEl.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.window === windowKey);
  }
  playerHostEl.innerHTML = '';
  player = null;
  const allEvents = [];

  // Lazy loading: start the player on the first chunk (it always begins
  // with a full snapshot), then stream the rest in via player.addEvent() —
  // playback starts without waiting for, or the server ever assembling,
  // the whole stream.
  for (let i = 0; i < chunks.length; i++) {
    const chunkRes = await fetch(`/api/sessions/${sessionId}/chunks/${chunks[i].name}`);
    if (seq !== loadSeq) return;
    let events;
    try {
      events = (await chunkRes.json()).events || [];
    } catch {
      continue; // skip corrupt chunk, same policy as the server
    }
    events.sort((a, b) => a.timestamp - b.timestamp);
    allEvents.push(...events);

    if (!player && allEvents.length >= 2) {
      player = new rrwebPlayer({
        target: playerHostEl,
        props: {
          events: allEvents.slice(),
          autoPlay: true,
          skipInactive: true,
          showController: true,
          width: playerHostEl.clientWidth,
          height: Math.max(420, Math.round(playerHostEl.clientWidth * 0.56)),
        },
      });
      window.__player = player; // debug handle
    } else if (player) {
      for (const event of events) player.addEvent(event);
      // Autoplay can drain the tiny initial timeline before these events
      // arrive: the state machine stays 'playing' but the internal timer has
      // stopped, so appended events never play and the play button is inert.
      // A goto() rebuilds the timer over the full timeline.
      if (player.getReplayer().getCurrentTime() <= 0) player.goto(0, true);
    }

    loadProgressEl.textContent =
      i + 1 < chunks.length ? `loading ${i + 1}/${chunks.length} chunks…` : '';
  }

  if (seq !== loadSeq) return;
  if (!player) {
    playerHostEl.innerHTML = '<p class="empty">Not enough events to replay yet.</p>';
  }
  renderConsole(allEvents);
  renderNetwork(allEvents);
  attachTimeSync();

  // Final safety net for the drained-timer wedge (see above): if the whole
  // session is loaded and the playhead never moved, restart from the top.
  if (player && player.getReplayer().getCurrentTime() <= 0) {
    player.goto(0, true);
  }
}

document.getElementById('refresh').addEventListener('click', loadSessions);
loadSessions();
