import { ACTIVE_SOURCES, CapturePlayer } from './player.js';

const projectSelectEl = document.getElementById('project-select');
const sessionListEl = document.getElementById('session-list');
const playerHostEl = document.getElementById('player-host');
const stageEl = document.getElementById('stage');
const controlsEl = document.getElementById('controls');
const playerMsgEl = document.getElementById('player-msg');
const skipNoteEl = document.getElementById('skip-note');
const assetErrorsEl = document.getElementById('asset-errors');
const sessionTitleEl = document.getElementById('session-title');
const loadProgressEl = document.getElementById('load-progress');
const windowTabsEl = document.getElementById('window-tabs');

const timelineListEl = document.getElementById('timeline-list');

let player = null;

addEventListener('keydown', (e) => {
  if (!player || e.target.closest('input, select, textarea, button')) return;
  if (e.code === 'Space') {
    e.preventDefault();
    player.toggle();
  } else if (e.code === 'ArrowLeft' || e.code === 'ArrowRight') {
    e.preventDefault();
    player.seekBy(e.code === 'ArrowRight' ? 5000 : -5000);
  }
});

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

function formatOffset(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

function pagePath(url) {
  try {
    const u = new URL(url);
    return u.pathname + u.search;
  } catch {
    return url || '';
  }
}

function hostPath(url) {
  try {
    const u = new URL(url);
    return u.host + u.pathname;
  } catch {
    return url || '';
  }
}

let currentProject = '';

function projectQuery() {
  return currentProject ? `?project=${encodeURIComponent(currentProject)}` : '';
}

const accountEl = document.getElementById('account');
const accountEmailEl = document.getElementById('account-email');

// Returns whether sessions are loadable: false means the empty state is
// already on screen and loadSessions must not overwrite it with a 403.
async function loadProjects() {
  let body;
  try {
    body = await fetchJson('/api/projects');
  } catch (err) {
    showListMessage(`Could not load projects. ${err.message}`);
    return false;
  }
  const projects = body.projects || [];
  if (body.email) {
    accountEmailEl.textContent = body.email;
    accountEl.hidden = false;
  }
  projectSelectEl.innerHTML = '';
  projectSelectEl.hidden = projects.length === 0;
  if (projects.length === 0) {
    currentProject = '';
    if (body.email) {
      showListMessage(
        `You're signed in as ${body.email}, but no projects have been shared ` +
          'with you yet. Ask your workspace admin to add you, then hit Refresh.'
      );
      return false;
    }
    return true;
  }
  for (const project of projects) {
    const option = document.createElement('option');
    option.value = project.token;
    option.textContent = project.name;
    projectSelectEl.appendChild(option);
  }
  if (!projects.some((p) => p.token === currentProject)) currentProject = projects[0].token;
  projectSelectEl.value = currentProject;
  return true;
}

projectSelectEl.addEventListener('change', () => {
  currentProject = projectSelectEl.value;
  clearPlayer();
  loadSessions();
});

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
        <span class="meta">${formatTime(session.first_seen)} · ${formatDuration(session.last_seen - session.first_seen)}</span>
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

  if (!loadedSessionId) {
    const first = sessionListEl.querySelector('li:not(.visitor-header)');
    if (first) first.click();
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

const pcToggleEl = document.getElementById('pc-toggle');
const pcCurEl = document.getElementById('pc-cur');
const pcDurEl = document.getElementById('pc-dur');
const pcBarEl = document.getElementById('pc-bar');
const pcGapsEl = document.getElementById('pc-gaps');
const pcHistEl = document.getElementById('pc-hist');
const pcFillEl = document.getElementById('pc-fill');
const pcDotEl = document.getElementById('pc-dot');
const pcSpeedsEl = document.getElementById('pc-speeds');
const pcSkipEl = document.querySelector('#pc-skip input');
const pcFsEl = document.getElementById('pc-fs');

function drawHistogram(events, duration) {
  const BUCKETS = 120;
  const counts = new Array(BUCKETS).fill(0);
  const start = events[0].timestamp;
  for (const e of events) {
    if (e.type === 3 && ACTIVE_SOURCES.has(e.data && e.data.source)) {
      const i = Math.min(BUCKETS - 1, Math.floor(((e.timestamp - start) / duration) * BUCKETS));
      counts[i] += 1;
    }
  }
  // Light smoothing so single events still read as a visible blip.
  const smooth = counts.map(
    (_, i) => (counts[i - 1] || 0) * 0.25 + counts[i] * 0.5 + (counts[i + 1] || 0) * 0.25
  );
  const max = Math.max(...smooth, 1);

  const width = Math.max(pcBarEl.clientWidth, 200);
  const height = 14;
  pcHistEl.width = width * devicePixelRatio;
  pcHistEl.height = height * devicePixelRatio;
  const ctx = pcHistEl.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = 'rgba(79, 70, 229, 0.4)';
  const bw = width / BUCKETS;
  for (let i = 0; i < BUCKETS; i++) {
    if (!smooth[i]) continue;
    const h = Math.max(2, (smooth[i] / max) * height);
    ctx.fillRect(i * bw, height - h, Math.max(1, bw - 0.5), h);
  }
}

function renderScrubberChrome(p) {
  pcDurEl.textContent = formatOffset(p.duration);
  pcGapsEl.innerHTML = '';
  for (const seg of p.segments) {
    if (seg.isActive || seg.end - seg.start < 2000) continue;
    const band = document.createElement('div');
    band.style.left = `${(seg.start / p.duration) * 100}%`;
    band.style.width = `${((seg.end - seg.start) / p.duration) * 100}%`;
    pcGapsEl.appendChild(band);
  }
  drawHistogram(p.events, p.duration);
}

function updateScrubber(ms) {
  if (!player) return;
  const pct = Math.min(100, (ms / player.duration) * 100);
  pcFillEl.style.width = `${pct}%`;
  pcDotEl.style.left = `${pct}%`;
  pcCurEl.textContent = formatOffset(ms);
}

function updatePlayState(state) {
  pcToggleEl.textContent = state === 'playing' ? '⏸' : '▶';
  if (state === 'ended') skipNoteEl.hidden = true;
}

let skipNoteTimer;
function showSkipNote(skippedMs) {
  skipNoteEl.textContent = `skipped ${formatDuration(skippedMs)} idle`;
  skipNoteEl.hidden = false;
  clearTimeout(skipNoteTimer);
  skipNoteTimer = setTimeout(() => {
    skipNoteEl.hidden = true;
  }, 900);
}

function showAssetErrors(count) {
  assetErrorsEl.textContent = `${count} asset${count > 1 ? 's' : ''} failed to load`;
  assetErrorsEl.hidden = false;
}

function barSeek(e) {
  if (!player) return;
  const rect = pcBarEl.getBoundingClientRect();
  const pct = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
  player.seekTo(pct * player.duration);
}

pcBarEl.addEventListener('pointerdown', (e) => {
  pcBarEl.setPointerCapture(e.pointerId);
  barSeek(e);
});
pcBarEl.addEventListener('pointermove', (e) => {
  if (pcBarEl.hasPointerCapture(e.pointerId) && e.buttons) barSeek(e);
});

pcToggleEl.addEventListener('click', () => player && player.toggle());
pcSpeedsEl.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-speed]');
  if (!btn || !player) return;
  player.setSpeed(Number(btn.dataset.speed));
  for (const b of pcSpeedsEl.querySelectorAll('button')) b.classList.toggle('active', b === btn);
});
pcSkipEl.addEventListener('change', () => player && player.setSkipInactive(pcSkipEl.checked));
pcFsEl.addEventListener('click', () => {
  (document.fullscreenElement
    ? document.exitFullscreen()
    : playerHostEl.requestFullscreen()
  ).catch(() => {});
});

function renderTimeline(events, navigations, session) {
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
    entries.push({ t: nav.t, kind: 'page', css: 'kind-page', detail: pagePath(nav.url).split('?')[0], title: nav.url });
  }
  entries.sort((a, b) => a.t - b.t);

  const order = session && session.order;
  if (events.length && order && (order.purchased_at || order.checkout_started_at)) {
    const amount =
      typeof order.total === 'number'
        ? ` — purchased ${order.total.toFixed(2)}${order.currency ? ' ' + order.currency : ''}`
        : '';
    entries.push({
      t: events[events.length - 1].timestamp,
      kind: order.purchased_at ? 'purchased' : 'checkout',
      css: 'kind-order',
      detail: `continued to checkout${order.purchased_at ? amount : ''}`,
    });
  }

  const start = events.length ? events[0].timestamp : entries.length ? entries[0].t : 0;
  document.getElementById('timeline-count').textContent = entries.length ? `(${entries.length})` : '';
  timelineListEl.innerHTML = '';
  timelineRows = [];
  if (entries.length === 0) {
    timelineListEl.innerHTML = '<li class="empty">No triggers, idle gaps or navigations in this recording.</li>';
    return;
  }
  for (const entry of entries) {
    const li = document.createElement('li');
    li.className = entry.css;
    li.innerHTML = `<span class="ts"></span><span class="kind"></span><span class="detail"></span>`;
    const ts = li.querySelector('.ts');
    ts.textContent = formatOffset(entry.t - start);
    ts.title = new Date(entry.t).toLocaleTimeString();
    li.querySelector('.kind').textContent = entry.kind;
    li.querySelector('.detail').textContent = entry.detail;
    if (entry.title) li.title = entry.title;
    li.addEventListener('click', () => player && player.seekTo(entry.t - syncStart));
    timelineRows.push({ el: li, t: entry.t });
    timelineListEl.appendChild(li);
  }
}

// Guards the async chunk loop: bumped whenever the user picks a session,
// so a stale load loop stops instead of feeding events to the wrong player.
let loadSeq = 0;
let loadedSessionId = null;

function clearPlayer() {
  loadSeq += 1;
  loadedSessionId = null;
  destroyPlayer();
  windowTabsEl.innerHTML = '';
  timelineListEl.innerHTML = '';
  timelineRows = [];
  loadProgressEl.textContent = '';
  sessionTitleEl.textContent = 'Select a session';
  sessionTitleEl.title = '';
}

function showPlayerMessage(message) {
  destroyPlayer();
  playerMsgEl.textContent = message;
  playerMsgEl.hidden = false;
}

async function loadSession(sessionId) {
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
        .catch(() => null) // skip corrupt chunk, same policy as the server
    )
  );
  if (seq !== loadSeq) return;
  loadProgressEl.textContent = '';

  const allEvents = [];
  const navigations = [];
  for (const chunk of results.filter(Boolean)) {
    const events = chunk.events || [];
    // Each chunk records the URL it was flushed from, so a change between
    // chunks is a navigation the event stream alone would not show.
    // Hash-only changes are scroll anchors, not navigations.
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

  syncStart = allEvents[0].timestamp;
  renderTimeline(allEvents, navigations, session);
  player = new CapturePlayer({
    events: allEvents,
    mount: stageEl,
    speed: Number(pcSpeedsEl.querySelector('.active').dataset.speed),
    skipInactive: pcSkipEl.checked,
    onTime: (ms) => {
      updateScrubber(ms);
      syncList(timelineRows, ms);
    },
    onState: updatePlayState,
    onSkip: showSkipNote,
    onAssetErrors: showAssetErrors,
  });
  controlsEl.hidden = false;
  renderScrubberChrome(player);
  updateScrubber(0);
}

const filterButtons = document.querySelectorAll('#session-filter button');
for (const btn of filterButtons) {
  btn.addEventListener('click', () => {
    sessionFilter = btn.dataset.filter;
    filterButtons.forEach((b) => b.classList.toggle('active', b === btn));
    renderSessionList();
  });
}

async function refreshAll() {
  if (await loadProjects()) loadSessions();
}

document.getElementById('refresh').addEventListener('click', refreshAll);
refreshAll();
