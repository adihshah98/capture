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
const sessionTitleEl = document.getElementById('session-title');

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

function renderConsole(events) {
  const logs = events
    .filter((e) => e.type === 6 && e.data && e.data.plugin === 'rrweb/console@1')
    .map((e) => ({ timestamp: e.timestamp, ...e.data.payload }));

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
    consoleListEl.appendChild(li);
  }
}

async function loadSession(sessionId) {
  sessionTitleEl.textContent = `Session ${sessionId}`;
  const res = await fetch(`/api/sessions/${sessionId}/events`);
  const { events, url } = await res.json();
  if (url) sessionTitleEl.textContent = `Session ${sessionId.slice(0, 8)}… — ${url}`;

  if (events.length < 2) {
    playerHostEl.innerHTML = '<p class="empty">Not enough events to replay yet.</p>';
    return;
  }

  playerHostEl.innerHTML = '';
  player = new rrwebPlayer({
    target: playerHostEl,
    props: {
      events,
      autoPlay: true,
      skipInactive: true,
      showController: true,
      width: playerHostEl.clientWidth,
      height: Math.max(420, Math.round(playerHostEl.clientWidth * 0.56)),
    },
  });

  renderConsole(events);
}

document.getElementById('refresh').addEventListener('click', loadSessions);
loadSessions();
