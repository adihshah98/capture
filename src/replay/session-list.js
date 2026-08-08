import { currentProject, fetchJson, projectQuery, setProject } from './api.js';
import { parseHash, writeHash } from './deeplink.js';
import {
  formatBytes,
  formatDuration,
  formatTime,
  isLive,
  orderAmount,
  sessionDuration,
  sessionHeadline,
  sessionOutcome,
  sourceLabel,
} from './format.js';
import { clearPlayer, loadSession, loadedSessionId, setPendingSeek } from './session-player.js';
import { setAnalysisEnabled } from './analysis.js';
import { setInsightsAnalysisEnabled } from './insights.js';

const boot = parseHash();

const projectSelectEl = document.getElementById('project-select');
const sessionListEl = document.getElementById('session-list');
const accountEl = document.getElementById('account');
const accountEmailEl = document.getElementById('account-email');

let allSessions = [];
let sessionFilter = 'all';
let searchTerm = '';
let nextCursor = null;
let totalSessions = 0;
let loadSeq = 0;
let loadingMore = false;

function sessionsUrl(cursor) {
  const params = new URLSearchParams();
  if (currentProject) params.set('project', currentProject);
  if (sessionFilter !== 'all') params.set('filter', sessionFilter);
  if (searchTerm) params.set('q', searchTerm);
  if (cursor) params.set('cursor', cursor);
  return `/api/sessions?${params}`;
}

const escape = (s) => String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);

function sessionMeta(session) {
  const parts = [];
  if (isLive(session)) parts.push('<span class="live">live</span>');
  else if (session.windows > 1) parts.push(`${session.windows} tabs`);
  else parts.push(formatDuration(sessionDuration(session)));
  const order = session.order || {};
  if (order.purchased_at && !isLive(session)) {
    const amount = orderAmount(order);
    if (amount) parts.push(`<span class="amount">${escape(amount)}</span>`);
  }
  parts.push(`<span class="src">${escape(sourceLabel(session))}</span>`);
  return parts.join(' · ');
}

function updateSessionCard(session) {
  const li = sessionListEl.querySelector(`li[data-sid="${session.session_id}"]`);
  if (!li) return;
  li.querySelector('.headline').textContent = sessionHeadline(session);
  li.querySelector('.meta').innerHTML = sessionMeta(session);
  li.querySelector('.dot').className = `dot ${sessionOutcome(session)}`;
  li.title = rowTitle(session);
}

function rowTitle(session) {
  return (
    `${session.session_id}\n${formatTime(session.first_seen)}\n` +
    `${session.url || ''}\n${session.chunk_count} chunks · ${formatBytes(session.bytes)}`
  );
}

export async function findSession(sessionId) {
  const cached = allSessions.find((s) => s.session_id === sessionId);
  try {
    const fresh = await fetchJson(`/api/sessions/${sessionId}${projectQuery()}`);
    if (!cached) return fresh;
    Object.assign(cached, fresh);
    updateSessionCard(cached);
    return cached;
  } catch {
    return cached;
  }
}

function showListMessage(message) {
  sessionListEl.innerHTML = '';
  const li = document.createElement('li');
  li.className = 'empty';
  li.textContent = message;
  sessionListEl.appendChild(li);
}

async function loadProjects() {
  let body;
  try {
    body = await fetchJson('/api/projects');
  } catch (err) {
    showListMessage(`Could not load projects. ${err.message}`);
    return false;
  }
  const projects = body.projects || [];
  setAnalysisEnabled(body.analysis_enabled);
  setInsightsAnalysisEnabled(body.analysis_enabled);
  if (body.email) {
    accountEmailEl.textContent = body.email;
    accountEl.hidden = false;
  }
  projectSelectEl.innerHTML = '';
  projectSelectEl.hidden = projects.length === 0;
  if (projects.length === 0) {
    setProject('');
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
  if (boot.p) {
    setProject(boot.p);
    boot.p = '';
  }
  if (!projects.some((p) => p.token === currentProject)) setProject(projects[0].token);
  projectSelectEl.value = currentProject;
  return true;
}

function showSkeleton() {
  sessionListEl.innerHTML = '';
  for (let i = 0; i < 7; i++) {
    const li = document.createElement('li');
    li.className = 'skeleton';
    li.innerHTML = `
      <span class="dot"></span>
      <div class="info">
        <span class="sk" style="width:${58 + ((i * 13) % 34)}%"></span>
        <span class="sk" style="width:${32 + ((i * 7) % 20)}%;height:8px"></span>
      </div>`;
    li.style.animationDelay = `${i * 60}ms`;
    sessionListEl.appendChild(li);
  }
}

async function loadSessions({ append = false } = {}) {
  const seq = ++loadSeq;
  if (!append) {
    allSessions = [];
    nextCursor = null;
    showSkeleton();
  }
  let body;
  try {
    body = await fetchJson(sessionsUrl(append ? nextCursor : null));
  } catch (err) {
    if (seq !== loadSeq) return;
    allSessions = [];
    showListMessage(`Could not load sessions. ${err.message}`);
    return;
  }
  if (seq !== loadSeq) return;
  allSessions = allSessions.concat(body.sessions || []);
  nextCursor = body.cursor || null;
  totalSessions = body.total ?? allSessions.length;
  renderSessionList();
}

async function refreshLiveSessions() {
  if (document.hidden || loadingMore) return;
  const seq = loadSeq;
  let body;
  try {
    body = await fetchJson(sessionsUrl(null));
  } catch {
    return;
  }
  if (seq !== loadSeq) return;
  for (const fresh of body.sessions || []) {
    const row = allSessions.find((s) => s.session_id === fresh.session_id);
    if (!row) continue;
    Object.assign(row, fresh);
    updateSessionCard(row);
  }
  for (const row of allSessions) {
    if (!body.sessions.some((s) => s.session_id === row.session_id)) updateSessionCard(row);
  }
}

setInterval(refreshLiveSessions, 10000);

const loadMoreObserver = new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting)) loadMore();
});

function loadMore() {
  if (!nextCursor || loadingMore) return;
  loadingMore = true;
  loadSessions({ append: true }).finally(() => {
    loadingMore = false;
  });
}

function renderSessionList() {
  loadMoreObserver.disconnect();
  sessionListEl.innerHTML = '';
  if (allSessions.length === 0) {
    showListMessage(
      sessionFilter !== 'all' || searchTerm
        ? 'No sessions match this filter.'
        : 'No sessions yet. Open the demo page and click around.'
    );
    return;
  }

  const appendSession = (session, grouped) => {
    const li = document.createElement('li');
    li.dataset.sid = session.session_id;
    if (grouped) li.className = 'grouped';
    li.innerHTML = `
      <span class="dot ${sessionOutcome(session)}"></span>
      <div class="info">
        <span class="headline"></span>
        <span class="meta">${sessionMeta(session)}</span>
      </div>
      <button class="del" title="Delete this recording" aria-label="Delete this recording">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 7h16M9 7V5h6v2M6 7l1 12h10l1-12" />
        </svg>
      </button>
    `;
    li.querySelector('.headline').textContent = sessionHeadline(session);
    li.title = rowTitle(session);
    li.addEventListener('click', () => {
      document.querySelectorAll('#session-list li').forEach((el) => el.classList.remove('active'));
      li.classList.add('active');
      loadSession(session.session_id);
    });
    li.querySelector('.del').addEventListener('click', (event) => {
      event.stopPropagation();
      deleteSession(session.session_id, event.currentTarget);
    });
    sessionListEl.appendChild(li);
  };

  const appendVisitorHeader = (group) => {
    const li = document.createElement('li');
    li.className = 'visitor-header';
    const first = Math.min(...group.map((s) => s.first_seen));
    const purchases = group.filter((s) => s.order && s.order.purchased_at).length;
    const since = new Date(first).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    li.textContent =
      `Returning visitor · ${group.length} visits since ${since}` +
      (purchases ? ` · ${purchases} purchase${purchases > 1 ? 's' : ''}` : '');
    li.title = `Visitor ${group[0].visitor_id}`;
    sessionListEl.appendChild(li);
  };

  const byVisitor = new Map();
  for (const session of allSessions) {
    if (!session.visitor_id) continue;
    if (!byVisitor.has(session.visitor_id)) byVisitor.set(session.visitor_id, []);
    byVisitor.get(session.visitor_id).push(session);
  }
  const rendered = new Set();
  for (const session of allSessions) {
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

  if (nextCursor) {
    const li = document.createElement('li');
    li.className = 'load-more';
    li.textContent = `Load more (${totalSessions - allSessions.length} remaining)`;
    li.addEventListener('click', () => {
      li.textContent = 'Loading…';
      loadMore();
    });
    sessionListEl.appendChild(li);
    loadMoreObserver.observe(li);
  }

  if (!loadedSessionId) {
    if (boot.s) {
      const li = sessionListEl.querySelector(`li[data-sid="${CSS.escape(boot.s)}"]`);
      if (boot.t) setPendingSeek(boot.t * 1000);
      const sid = boot.s;
      boot.s = '';
      if (li) li.click();
      else loadSession(sid);
      return;
    }
    const target = sessionListEl.querySelector('li:not(.visitor-header):not(.load-more)');
    if (target) target.click();
  }
}

async function deleteSession(sessionId, button) {
  if (button.dataset.armed !== 'yes') {
    button.dataset.armed = 'yes';
    button.dataset.icon = button.innerHTML;
    button.textContent = 'Confirm?';
    button.classList.add('armed');
    setTimeout(() => {
      if (!button.isConnected || button.dataset.armed !== 'yes') return;
      delete button.dataset.armed;
      button.innerHTML = button.dataset.icon;
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
    allSessions = allSessions.filter((s) => s.session_id !== sessionId);
    totalSessions = Math.max(0, totalSessions - 1);
    renderSessionList();
  } catch (err) {
    button.disabled = false;
    button.textContent = 'Failed';
    console.error('capture: delete failed', err);
  }
}

export async function refreshAll() {
  if (await loadProjects()) loadSessions();
}

projectSelectEl.addEventListener('change', () => {
  setProject(projectSelectEl.value);
  writeHash({ p: currentProject });
  clearPlayer();
  loadSessions();
});

const searchEl = document.getElementById('session-search');
let searchDebounce;
searchEl.addEventListener('input', () => {
  searchTerm = searchEl.value.trim().toLowerCase();
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => loadSessions(), 250);
});

const filterButtons = document.querySelectorAll('#session-filter button');
for (const btn of filterButtons) {
  btn.addEventListener('click', () => {
    sessionFilter = btn.dataset.filter;
    filterButtons.forEach((b) => b.classList.toggle('active', b === btn));
    loadSessions();
  });
}
