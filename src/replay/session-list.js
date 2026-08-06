import { currentProject, fetchJson, projectQuery, setProject } from './api.js';
import { formatBytes, formatDuration, formatTime, pagePath } from './format.js';
import { clearPlayer, loadSession, loadedSessionId } from './session-player.js';

const projectSelectEl = document.getElementById('project-select');
const sessionListEl = document.getElementById('session-list');
const accountEl = document.getElementById('account');
const accountEmailEl = document.getElementById('account-email');

export let allSessions = [];
let sessionFilter = 'all';
const FILTERS = {
  all: () => true,
  checkout: (s) => Boolean(s.order),
  purchased: (s) => Boolean(s.order && s.order.purchased_at),
  rage: (s) => Boolean(s.interactions && s.interactions.rage),
};

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
  if (!projects.some((p) => p.token === currentProject)) setProject(projects[0].token);
  projectSelectEl.value = currentProject;
  return true;
}

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
    const rage = session.interactions && session.interactions.rage;
    if (rage) {
      const badge = document.createElement('span');
      badge.className = 'order rage';
      badge.textContent = `${rage} rage click${rage > 1 ? 's' : ''}`;
      li.querySelector('.info').appendChild(badge);
    }
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

export async function refreshAll() {
  if (await loadProjects()) loadSessions();
}

projectSelectEl.addEventListener('change', () => {
  setProject(projectSelectEl.value);
  clearPlayer();
  loadSessions();
});

const filterButtons = document.querySelectorAll('#session-filter button');
for (const btn of filterButtons) {
  btn.addEventListener('click', () => {
    sessionFilter = btn.dataset.filter;
    filterButtons.forEach((b) => b.classList.toggle('active', b === btn));
    renderSessionList();
  });
}
