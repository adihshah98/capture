import rrwebPlayer from 'rrweb-player';
import 'rrweb-player/dist/style.css';

const projectSelectEl = document.getElementById('project-select');
const sessionListEl = document.getElementById('session-list');
const playerHostEl = document.getElementById('player-host');
const sessionTitleEl = document.getElementById('session-title');
const loadProgressEl = document.getElementById('load-progress');
const windowTabsEl = document.getElementById('window-tabs');

const timelineListEl = document.getElementById('timeline-list');

let player = null;

function formatTime(ms) {
  return new Date(ms).toLocaleString();
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDuration(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function pagePath(url) {
  try {
    const u = new URL(url);
    return u.pathname + u.search;
  } catch {
    return url || '';
  }
}

let currentProject = '';

function projectQuery() {
  return currentProject ? `?project=${encodeURIComponent(currentProject)}` : '';
}

async function loadProjects() {
  let projects = [];
  try {
    projects = (await (await fetch('/api/projects')).json()).projects || [];
  } catch {
    /* older server without /api/projects — single-tenant behavior */
  }
  if (projects.length === 0) return;
  projectSelectEl.hidden = false;
  for (const project of projects) {
    const option = document.createElement('option');
    option.value = project.token;
    option.textContent = project.name;
    projectSelectEl.appendChild(option);
  }
  currentProject = projects[0].token;
  projectSelectEl.addEventListener('change', () => {
    currentProject = projectSelectEl.value;
    loadSessions();
  });
}

async function fetchJson(url, options) {
  let res;
  try {
    res = await fetch(url, options);
  } catch {
    // A 401 carrying WWW-Authenticate never reaches this code: the browser
    // intercepts it and prompts for credentials itself, and fetch rejects if
    // that is dismissed. So a rejection means "down" or "needs the password",
    // and the message can't claim to know which.
    throw new Error('Server unreachable, or it needs the password again — try reloading.');
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* empty or non-JSON body — fall back to the status below */
  }
  if (!res.ok) {
    if (res.status === 401) {
      // Session expiry mid-visit lands here: go log in rather than erroring.
      if (body && body.login) {
        window.location = body.login;
        throw new Error('Signing in…');
      }
      throw new Error('Not authorized — reload the page to re-enter the replay password.');
    }
    throw new Error((body && body.error) || `Server returned ${res.status}.`);
  }
  return body || {};
}

// Server-supplied text goes in via textContent, never innerHTML.
function showListMessage(message) {
  sessionListEl.innerHTML = '';
  const li = document.createElement('li');
  li.className = 'empty';
  li.textContent = message;
  sessionListEl.appendChild(li);
}

let allSessions = [];
let sessionFilter = 'all';
const FILTERS = {
  all: () => true,
  checkout: (s) => Boolean(s.order),
  purchased: (s) => Boolean(s.order && s.order.purchased_at),
};

async function loadSessions() {
  try {
    const body = await fetchJson(`/api/sessions${projectQuery()}`);
    allSessions = Array.isArray(body.sessions) ? body.sessions : [];
  } catch (err) {
    allSessions = [];
    showListMessage(`Could not load sessions. ${err.message}`);
    return;
  }
  renderSessionList();
}

function renderSessionList() {
  sessionListEl.innerHTML = '';
  if (allSessions.length === 0) {
    showListMessage('No sessions yet. Open the demo page and click around.');
    return;
  }
  const sessions = allSessions.filter(FILTERS[sessionFilter]);
  if (sessions.length === 0) {
    showListMessage('No sessions match this filter.');
    return;
  }

  const appendSession = (session, grouped) => {
    const li = document.createElement('li');
    if (grouped) li.className = 'grouped';
    li.innerHTML = `
      <div class="info">
        <span class="sid">${session.session_id.slice(0, 8)}…</span>
        <span class="meta">${formatTime(session.last_seen)} · ${formatDuration(session.last_seen - session.first_seen)}</span>
        <span class="page"></span>
      </div>
      <button class="del" title="Delete this recording">Delete</button>
    `;
    const page = li.querySelector('.page');
    page.textContent = pagePath(session.url);
    page.title = session.url || '';
    li.title = `${session.chunk_count} chunks · ${formatBytes(session.bytes)}`;
    // Order fields originate from checkout POSTs, so they are untrusted
    // input: textContent, never the innerHTML template above.
    const order = session.order;
    if (order && (order.purchased_at || order.checkout_started_at)) {
      const badge = document.createElement('span');
      const purchased = Boolean(order.purchased_at);
      badge.className = `order ${purchased ? 'purchased' : 'started'}`;
      const amount =
        typeof order.total === 'number'
          ? ` · ${order.total.toFixed(2)}${order.currency ? ' ' + order.currency : ''}`
          : '';
      badge.textContent = purchased ? `Purchased${amount}` : 'Reached checkout';
      if (order.order_id) badge.title = `Order ${order.order_id}`;
      li.querySelector('.info').appendChild(badge);
    }
    li.addEventListener('click', () => {
      document.querySelectorAll('#session-list li').forEach((el) => el.classList.remove('active'));
      li.classList.add('active');
      loadSession(session.session_id);
    });
    li.querySelector('.del').addEventListener('click', (event) => {
      event.stopPropagation(); // don't load the session we're deleting
      deleteSession(session.session_id, event.currentTarget);
    });
    sessionListEl.appendChild(li);
  };

  const appendVisitorHeader = (group) => {
    const li = document.createElement('li');
    li.className = 'visitor-header';
    const first = Math.min(...group.map((s) => s.first_seen));
    const purchases = group.filter((s) => s.order && s.order.purchased_at).length;
    li.textContent =
      `Visitor ${String(group[0].visitor_id).slice(0, 8)} · ${group.length} visits` +
      ` · since ${new Date(first).toLocaleDateString()}` +
      (purchases ? ` · ${purchases} purchase${purchases > 1 ? 's' : ''}` : '');
    sessionListEl.appendChild(li);
  };

  const byVisitor = new Map();
  for (const session of sessions) {
    if (!session.visitor_id) continue;
    if (!byVisitor.has(session.visitor_id)) byVisitor.set(session.visitor_id, []);
    byVisitor.get(session.visitor_id).push(session);
  }
  const rendered = new Set();
  for (const session of sessions) {
    if (rendered.has(session)) continue;
    const group = session.visitor_id ? byVisitor.get(session.visitor_id) : null;
    if (group && group.length > 1) {
      appendVisitorHeader(group);
      for (const s of group) {
        appendSession(s, true);
        rendered.add(s);
      }
    } else {
      appendSession(session, false);
    }
  }
}

// Two clicks to delete, inline rather than confirm(): the page never blocks
// on a modal, and this is irreversible.
async function deleteSession(sessionId, button) {
  if (button.dataset.armed !== 'yes') {
    button.dataset.armed = 'yes';
    button.textContent = 'Confirm?';
    button.classList.add('armed');
    setTimeout(() => {
      if (!button.isConnected || button.dataset.armed !== 'yes') return;
      delete button.dataset.armed;
      button.textContent = 'Delete';
      button.classList.remove('armed');
    }, 4000);
    return;
  }

  button.disabled = true;
  button.textContent = 'Deleting…';
  try {
    const res = await fetch(`/api/sessions/${sessionId}${projectQuery()}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`server returned ${res.status}`);
    if (sessionId === loadedSessionId) clearPlayer();
    await loadSessions();
  } catch (err) {
    button.disabled = false;
    button.textContent = 'Failed';
    console.error('capture: delete failed', err);
  }
}

let timelineRows = [];
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
  if (rows.lastIdx === currentIdx) return;
  rows.lastIdx = currentIdx;
  rows.forEach((row, i) => {
    row.el.classList.toggle('current', i === currentIdx);
    row.el.classList.toggle('future', i > currentIdx);
  });
  if (currentIdx >= 0 && !rows[currentIdx].el.parentElement.hidden) {
    rows[currentIdx].el.scrollIntoView({ block: 'nearest' });
  }
}

// rrweb IncrementalSource values for real user interaction: MouseMove(1),
// MouseInteraction(2), Scroll(3), Input(5), TouchMove(6).
const INTERACTIVE_SOURCES = new Set([1, 2, 3, 5, 6]);

// pointer-events: none keeps the canvas click-through, so seeking on the
// progress bar underneath works untouched.
function attachActivityBar(events) {
  if (!player) return;
  const progressEl = playerHostEl.querySelector('.rr-progress');
  if (!progressEl) return;
  const meta = player.getMetaData();
  if (!meta.totalTime) return;

  const BUCKETS = 120;
  const counts = new Array(BUCKETS).fill(0);
  for (const e of events) {
    if (e.type === 3 && INTERACTIVE_SOURCES.has(e.data && e.data.source)) {
      const i = Math.min(
        BUCKETS - 1,
        Math.floor(((e.timestamp - meta.startTime) / meta.totalTime) * BUCKETS)
      );
      counts[i] += 1;
    }
  }
  // Light smoothing so single events still read as a visible blip.
  const smooth = counts.map(
    (_, i) => (counts[i - 1] || 0) * 0.25 + counts[i] * 0.5 + (counts[i + 1] || 0) * 0.25
  );
  const max = Math.max(...smooth, 1);

  const canvas = document.createElement('canvas');
  canvas.className = 'activity-bar';
  const width = Math.max(progressEl.clientWidth, 200);
  const height = 16;
  canvas.width = width * devicePixelRatio;
  canvas.height = height * devicePixelRatio;
  const ctx = canvas.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.fillStyle = 'rgba(79, 70, 229, 0.45)';
  const bw = width / BUCKETS;
  for (let i = 0; i < BUCKETS; i++) {
    if (!smooth[i]) continue;
    const h = Math.max(2, (smooth[i] / max) * height);
    ctx.fillRect(i * bw, height - h, Math.max(1, bw - 0.5), h);
  }
  progressEl.style.position = 'relative';
  progressEl.appendChild(canvas);
}

function attachTimeSync() {
  if (!player) return;
  syncStart = player.getMetaData().startTime;
  syncList(timelineRows, 0);
  player.addEventListener('ui-update-current-time', ({ payload }) => {
    syncList(timelineRows, payload);
  });
}

function renderPanel(listEl, countId, items, emptyText, buildRow) {
  document.getElementById(countId).textContent = items.length ? `(${items.length})` : '';
  listEl.innerHTML = '';
  if (items.length === 0) {
    listEl.innerHTML = `<li class="empty">${emptyText}</li>`;
    return [];
  }
  const rows = [];
  for (const item of items) {
    const li = buildRow(item);
    li.addEventListener('click', () => seekTo(item.t));
    rows.push({ el: li, t: item.t });
    listEl.appendChild(li);
  }
  return rows;
}

function renderTimeline(events, navigations) {
  const marker = (e) => e.type === 5 && e.data && e.data.tag;
  const entries = [];

  for (const event of events.filter(marker)) {
    const tag = event.data.tag;
    if (tag === 'capture-trigger') {
      const reason = (event.data.payload && event.data.payload.reason) || 'unknown';
      entries.push({ t: event.timestamp, kind: 'trigger', css: 'kind-trigger', detail: `kept because: ${reason}` });
    } else if (tag === 'capture-idle') {
      entries.push({ t: event.timestamp, kind: 'idle', css: 'kind-idle', detail: 'no interaction — recording paused' });
    } else if (tag === 'capture-idle-end') {
      entries.push({ t: event.timestamp, kind: 'resumed', css: 'kind-idle', detail: 'activity resumed' });
    }
  }
  for (const nav of navigations) {
    entries.push({ t: nav.t, kind: 'page', css: 'kind-page', detail: nav.url });
  }
  entries.sort((a, b) => a.t - b.t);

  timelineRows = renderPanel(timelineListEl, 'timeline-count', entries, 'No triggers, idle gaps or navigations in this recording.', (entry) => {
    const li = document.createElement('li');
    li.className = entry.css;
    li.innerHTML = `<span class="ts">${new Date(entry.t).toLocaleTimeString()}</span><span class="kind"></span><span class="detail"></span>`;
    li.querySelector('.kind').textContent = entry.kind;
    li.querySelector('.detail').textContent = entry.detail;
    return li;
  });
}

// Guards the async chunk loop: bumped whenever the user picks a session,
// so a stale load loop stops instead of feeding events to the wrong player.
let loadSeq = 0;
let loadedSessionId = null;

function resetPlayerPanes() {
  player = null;
  playerHostEl.innerHTML = '';
  windowTabsEl.innerHTML = '';
  timelineListEl.innerHTML = '';
  timelineRows = [];
  loadProgressEl.textContent = '';
}

function clearPlayer() {
  loadSeq += 1;
  loadedSessionId = null;
  resetPlayerPanes();
  sessionTitleEl.textContent = 'Select a session';
}

function showPlayerMessage(message) {
  resetPlayerPanes();
  const p = document.createElement('p');
  p.className = 'empty';
  p.textContent = message;
  playerHostEl.appendChild(p);
}

async function loadSession(sessionId) {
  const seq = ++loadSeq;
  loadedSessionId = sessionId;
  sessionTitleEl.textContent = `Session ${sessionId}`;

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
    sessionTitleEl.textContent = `Session ${sessionId.slice(0, 8)}… — ${manifest.url}`;
  }

  // A multi-tab session is several independent rrweb streams (one full
  // snapshot + incrementals per tab); interleaving them would garble the
  // DOM, so each window plays separately behind a tab picker.
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

async function playWindow(sessionId, chunks, windowKey, seq) {
  for (const btn of windowTabsEl.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.window === windowKey);
  }
  playerHostEl.innerHTML = '';
  player = null;
  const allEvents = [];

  // Start the player on the first chunk (it always begins with a full
  // snapshot) and stream the rest in via addEvent() — playback starts
  // without waiting for the whole session.
  let lastError = null;
  const navigations = [];
  for (let i = 0; i < chunks.length; i++) {
    let events;
    try {
      const chunk = await fetchJson(
        `/api/sessions/${sessionId}/chunks/${chunks[i].name}${projectQuery()}`
      );
      events = chunk.events || [];
      // Each chunk records the URL it was flushed from, so a change between
      // chunks is a navigation the event stream alone would not show.
      const previous = navigations.length ? navigations[navigations.length - 1].url : null;
      if (chunk.url && chunk.url !== previous) {
        navigations.push({ url: chunk.url, t: events.length ? events[0].timestamp : chunk.received_at });
      }
    } catch (err) {
      lastError = err; // skip corrupt chunk, same policy as the server
      continue;
    }
    if (seq !== loadSeq) return;
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
    showPlayerMessage(
      lastError
        ? `Could not load this session. ${lastError.message}`
        : 'Not enough events to replay yet.'
    );
  }
  renderTimeline(allEvents, navigations);
  attachTimeSync();
  attachActivityBar(allEvents);

  // Final safety net for the drained-timer wedge (see above): if the whole
  // session is loaded and the playhead never moved, restart from the top.
  if (player && player.getReplayer().getCurrentTime() <= 0) {
    player.goto(0, true);
  }
}

const filterButtons = document.querySelectorAll('#session-filter button');
for (const btn of filterButtons) {
  btn.addEventListener('click', () => {
    sessionFilter = btn.dataset.filter;
    filterButtons.forEach((b) => b.classList.toggle('active', b === btn));
    renderSessionList();
  });
}

document.getElementById('refresh').addEventListener('click', loadSessions);
loadProjects().then(loadSessions);
