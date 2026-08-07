import { currentProject, fetchJson, setProject } from './api.js';
import { writeHash } from './deeplink.js';
import { loadSession } from './session-player.js';
import { renderMarkdown } from './analysis.js';

const POLL_MS = 2000;

const modeButtons = document.querySelectorAll('#mode-switch button');
const tabButtons = document.querySelectorAll('.insights-tabs button[data-insights-tab]');
const runBtn = document.getElementById('cohort-run');
const runBtnCompare = document.getElementById('cohort-run-compare');
const labelEl = document.getElementById('cohort-label');
const progressEl = document.getElementById('cohort-progress');
const compareLabelEl = document.getElementById('compare-label');
const compareProgressEl = document.getElementById('compare-progress');
const tableWrapEl = document.getElementById('cohort-table-wrap');
const compareBodyEl = document.getElementById('compare-body');
const queuePanelEl = document.getElementById('insights-queue');
const comparePanelEl = document.getElementById('insights-compare');

let analysisEnabled = false;
let pollTimer = null;
let cohort = null;
let activeTab = 'compare';
let autoSwitchedForRun = false;

export function setInsightsAnalysisEnabled(on) {
  analysisEnabled = !!on;
  syncRunButtons();
}

function syncRunButtons() {
  const running = cohort && (cohort.status === 'running' || cohort.status === 'queued');
  for (const btn of [runBtn, runBtnCompare]) {
    btn.disabled = !analysisEnabled || running;
    if (!analysisEnabled) {
      btn.title = 'Analysis is not configured on this server';
    } else {
      btn.title = '';
    }
    if (running) {
      btn.textContent = 'Running…';
    } else if (cohort && cohort.sessions && cohort.sessions.length) {
      btn.textContent = 'Run again (5+5)';
    } else {
      btn.textContent = 'Run sample cohort (5+5)';
    }
  }
}

function cohortUrl({ start = false, force = false } = {}) {
  const params = new URLSearchParams();
  if (currentProject) params.set('project', currentProject);
  if (start) {
    params.set('purchased', '5');
    params.set('all', '5');
    if (force) params.set('force', '1');
  }
  const qs = params.toString();
  return `/api/cohort${qs ? `?${qs}` : ''}`;
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function schedulePoll() {
  stopPolling();
  if (!document.body.classList.contains('mode-insights')) return;
  if (!cohort || (cohort.status !== 'running' && cohort.status !== 'queued')) return;
  pollTimer = setTimeout(() => {
    refreshCohort().catch((err) => {
      progressEl.textContent = err.message;
      compareProgressEl.textContent = err.message;
    });
  }, POLL_MS);
}

function setMode(mode) {
  const next = mode === 'insights' ? 'insights' : 'replay';
  document.body.classList.toggle('mode-insights', next === 'insights');
  modeButtons.forEach((btn) => btn.classList.toggle('active', btn.dataset.mode === next));
  if (next === 'insights') {
    refreshCohort().catch((err) => {
      progressEl.textContent = err.message;
      compareProgressEl.textContent = err.message;
    });
  } else {
    stopPolling();
  }
}

function setTab(tab) {
  activeTab = tab === 'queue' ? 'queue' : 'compare';
  tabButtons.forEach((btn) => {
    if (btn.disabled) return;
    btn.classList.toggle('active', btn.dataset.insightsTab === activeTab);
  });
  comparePanelEl.hidden = activeTab !== 'compare';
  queuePanelEl.hidden = activeTab !== 'queue';
}

function openSession(sessionId) {
  setMode('replay');
  writeHash({ p: currentProject, s: sessionId });
  loadSession(sessionId);
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, '&#39;');
}

function progressText(body) {
  const prog = body.progress || {};
  return (
    `${prog.done || 0} / ${prog.total || 0} done · ${prog.running || 0} running · ` +
    `${prog.queued || 0} queued` +
    (prog.error ? ` · ${prog.error} failed` : '')
  );
}

function renderEmpty(message) {
  tableWrapEl.innerHTML = `<div id="cohort-empty">${message}</div>`;
}

function renderQueue(body) {
  const sessions = body.sessions || [];
  if (!sessions.length) {
    renderEmpty('No sessions matched this sample.');
    return;
  }

  const rows = sessions
    .map((entry) => {
      const filterClass = entry.filter === 'purchased' ? 'purchased' : 'all';
      const filterLabel = entry.filter === 'purchased' ? 'Purchased' : 'All';
      const status = entry.status || 'queued';
      const err = entry.error
        ? `<div style="color:#b91c1c;font-size:11px;margin-top:2px">${escapeHtml(entry.error)}</div>`
        : '';
      return `
        <tr class="openable" data-sid="${escapeAttr(entry.session_id)}">
          <td class="sid">${escapeHtml(entry.session_id.slice(0, 8))}…</td>
          <td><span class="badge ${filterClass}">${filterLabel}</span></td>
          <td>
            <span class="badge ${escapeAttr(status)}">${escapeHtml(status)}</span>
            ${err}
          </td>
          <td>${entry.analysis ? escapeHtml(entry.analysis) : '—'}</td>
        </tr>`;
    })
    .join('');

  tableWrapEl.innerHTML = `
    <table id="cohort-table">
      <thead>
        <tr>
          <th>Session</th>
          <th>Sample</th>
          <th>Analysis</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  tableWrapEl.querySelectorAll('tr.openable').forEach((tr) => {
    tr.addEventListener('click', () => openSession(tr.dataset.sid));
  });
}

function insightCard(entry) {
  const status = entry.status || 'queued';
  const pending = status !== 'done' || !entry.text;
  const head = `
    <div class="insight-card-head">
      <span class="sid">${escapeHtml(entry.session_id.slice(0, 8))}…</span>
      <span class="badge ${escapeAttr(status)}">${escapeHtml(status)}</span>
      <button type="button" class="open" data-sid="${escapeAttr(entry.session_id)}">Open</button>
    </div>`;

  if (status === 'error') {
    return `<article class="insight-card pending">${head}
      <p class="status-note error">${escapeHtml(entry.error || 'Analysis failed.')}</p>
    </article>`;
  }
  if (pending) {
    const note =
      status === 'running'
        ? 'Analyzing… write-up will appear here when ready.'
        : 'Queued — waiting for analysis.';
    return `<article class="insight-card pending">${head}
      <p class="status-note">${note}</p>
    </article>`;
  }

  const standout = entry.standout
    ? `<p class="insight-standout">${escapeHtml(entry.standout)}</p>`
    : '';
  return `<article class="insight-card">${head}
    ${standout}
    <details>
      <summary>Full write-up</summary>
      <div class="md">${renderMarkdown(entry.text)}</div>
    </details>
  </article>`;
}

function renderCompare(body) {
  const sessions = body.sessions || [];
  if (!sessions.length) {
    compareBodyEl.innerHTML =
      '<div class="insights-placeholder">No sessions matched this sample.</div>';
    return;
  }

  const purchased = sessions.filter((s) => s.filter === 'purchased');
  const all = sessions.filter((s) => s.filter !== 'purchased');
  const donePurchased = purchased.filter((s) => s.status === 'done' && s.text).length;
  const doneAll = all.filter((s) => s.status === 'done' && s.text).length;

  compareLabelEl.textContent = body.label || 'Purchased vs all';
  compareBodyEl.innerHTML = `
    <div class="compare-grid">
      <div class="compare-col">
        <h3>Purchased <span class="n">(${donePurchased}/${purchased.length})</span></h3>
        ${purchased.map(insightCard).join('') || '<p class="status-note">No purchased sessions in this sample.</p>'}
      </div>
      <div class="compare-col">
        <h3>All / non-purchase <span class="n">(${doneAll}/${all.length})</span></h3>
        ${all.map(insightCard).join('') || '<p class="status-note">No other sessions in this sample.</p>'}
      </div>
    </div>`;

  compareBodyEl.querySelectorAll('button.open').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openSession(btn.dataset.sid);
    });
  });
}

function renderCohort(body) {
  const prevStatus = cohort && cohort.status;
  cohort = body;
  const text = progressText(body);
  labelEl.textContent = body.label || 'Sample cohort';
  progressEl.textContent = text;
  compareProgressEl.textContent = text;
  syncRunButtons();
  renderQueue(body);
  renderCompare(body);

  // While a run is in flight, show Queue; once write-ups land, jump to Compare.
  const running = body.status === 'running' || body.status === 'queued';
  const hasInsights = (body.sessions || []).some((s) => s.status === 'done' && s.text);
  if (running && !autoSwitchedForRun) {
    setTab('queue');
    autoSwitchedForRun = true;
  } else if (
    hasInsights &&
    autoSwitchedForRun &&
    prevStatus &&
    (prevStatus === 'running' || prevStatus === 'queued') &&
    !running
  ) {
    setTab('compare');
    autoSwitchedForRun = false;
  } else if (hasInsights && !running && activeTab === 'queue' && !autoSwitchedForRun) {
    // Fresh load with a finished cohort — Compare is the main answer.
    setTab('compare');
  }

  schedulePoll();
}

function resetEmptyState() {
  cohort = null;
  autoSwitchedForRun = false;
  labelEl.textContent = 'Sample cohort';
  compareLabelEl.textContent = 'Purchased vs all';
  progressEl.textContent = 'No cohort yet';
  compareProgressEl.textContent = 'No cohort yet';
  syncRunButtons();
  renderEmpty(
    'Run a sample cohort to analyze 5 purchased and 5 other sessions. Open any row to watch it in Replay.'
  );
  compareBodyEl.innerHTML =
    '<div class="insights-placeholder">Run a sample cohort to compare write-ups for purchased vs other sessions side by side.</div>';
}

export async function refreshCohort() {
  try {
    const body = await fetchJson(cohortUrl());
    renderCohort(body);
  } catch (err) {
    stopPolling();
    if (/no cohort yet/i.test(err.message || '')) {
      resetEmptyState();
      return;
    }
    progressEl.textContent = err.message;
    compareProgressEl.textContent = err.message;
    throw err;
  }
}

async function runCohort() {
  if (!analysisEnabled || runBtn.disabled) return;
  autoSwitchedForRun = false;
  for (const btn of [runBtn, runBtnCompare]) {
    btn.disabled = true;
    btn.textContent = 'Starting…';
  }
  progressEl.textContent = 'Sampling sessions…';
  compareProgressEl.textContent = 'Sampling sessions…';
  try {
    const body = await fetchJson(cohortUrl({ start: true }), { method: 'POST' });
    renderCohort(body);
  } catch (err) {
    progressEl.textContent = err.message;
    compareProgressEl.textContent = err.message;
    syncRunButtons();
  }
}

export function initInsights() {
  modeButtons.forEach((btn) => {
    btn.addEventListener('click', () => setMode(btn.dataset.mode));
  });
  tabButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      autoSwitchedForRun = false;
      setTab(btn.dataset.insightsTab);
    });
  });
  runBtn.addEventListener('click', runCohort);
  runBtnCompare.addEventListener('click', runCohort);
  setTab('compare');

  const projectSelect = document.getElementById('project-select');
  projectSelect.addEventListener('change', () => {
    setProject(projectSelect.value);
    if (document.body.classList.contains('mode-insights')) {
      refreshCohort().catch(() => {});
    }
  });
}
