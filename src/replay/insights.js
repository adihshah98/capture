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
const compareSummaryEl = document.getElementById('compare-summary');
const compareExamplesEl = document.getElementById('compare-examples');
const splitEl = document.getElementById('compare-split');
const queuePanelEl = document.getElementById('insights-queue');
const comparePanelEl = document.getElementById('insights-compare');
const reviewPanelEl = document.getElementById('insights-review');
const reviewBodyEl = document.getElementById('review-body');
const reviewProgressEl = document.getElementById('review-progress');

let analysisEnabled = false;
let pollTimer = null;
let cohort = null;
let summary = null;
let activeTab = 'compare';
let autoSwitchedForRun = false;
let selectedTheme = null;
let reviewIndex = 0;
let reviewQueue = [];
let reviewVocab = [];
let reviewDraftThemes = [];

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
      btn.textContent = 'Analyzing…';
    } else if (cohort && cohort.sessions && cohort.sessions.length) {
      btn.textContent = 'Analyze 10 again';
    } else {
      btn.textContent = 'Analyze 10 sessions';
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

function summaryUrl() {
  const params = new URLSearchParams();
  if (currentProject) params.set('project', currentProject);
  params.set('group_by', 'none');
  params.set('contrast', (splitEl && splitEl.value) || 'purchased_vs_all');
  params.set('qa_mode', 'include_pending');
  return `/api/insights/summary?${params.toString()}`;
}

function qaUrl(sessionId) {
  const params = new URLSearchParams();
  if (currentProject) params.set('project', currentProject);
  const qs = params.toString();
  return `/api/sessions/${sessionId}/qa${qs ? `?${qs}` : ''}`;
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
  const allowed = new Set(['compare', 'queue', 'review']);
  activeTab = allowed.has(tab) ? tab : 'compare';
  tabButtons.forEach((btn) => {
    if (btn.disabled) return;
    btn.classList.toggle('active', btn.dataset.insightsTab === activeTab);
  });
  comparePanelEl.hidden = activeTab !== 'compare';
  queuePanelEl.hidden = activeTab !== 'queue';
  reviewPanelEl.hidden = activeTab !== 'review';
  if (activeTab === 'review') renderReview();
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

function qaBadge(status) {
  const s = status || 'pending';
  return `<span class="badge qa-${escapeAttr(s)}">${escapeHtml(s)}</span>`;
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
      const campaign =
        (entry.attribution && entry.attribution.utm_campaign) ||
        (entry.attribution && entry.attribution.utm_source) ||
        '—';
      const err = entry.error
        ? `<div style="color:#b91c1c;font-size:11px;margin-top:2px">${escapeHtml(entry.error)}</div>`
        : '';
      return `
        <tr class="openable" data-sid="${escapeAttr(entry.session_id)}">
          <td class="sid">${escapeHtml(entry.session_id.slice(0, 8))}…</td>
          <td><span class="badge ${filterClass}">${filterLabel}</span></td>
          <td>${escapeHtml(String(campaign))}</td>
          <td>
            <span class="badge ${escapeAttr(status)}">${escapeHtml(status)}</span>
            ${err}
          </td>
          <td>${qaBadge(entry.qa_status)}</td>
          <td>${
            (entry.themes || []).length
              ? `<div class="theme-chips">${entry.themes
                  .map((t) => `<span class="theme-chip">${escapeHtml(String(t))}</span>`)
                  .join('')}</div>`
              : '—'
          }</td>
        </tr>`;
    })
    .join('');

  tableWrapEl.innerHTML = `
    <table id="cohort-table">
      <thead>
        <tr>
          <th>Session</th>
          <th>Sample</th>
          <th>Campaign</th>
          <th>Analysis</th>
          <th>QA</th>
          <th>Themes</th>
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
  const campaign = entry.attribution && entry.attribution.utm_campaign;
  const attrBadge = campaign
    ? `<span class="attr-badge">${escapeHtml(campaign)}</span>`
    : entry.attribution && entry.attribution.fbclid
      ? `<span class="attr-badge">Meta</span>`
      : '';
  const head = `
    <div class="insight-card-head">
      <span class="sid">${escapeHtml(entry.session_id.slice(0, 8))}…</span>
      ${attrBadge}
      ${qaBadge(entry.qa_status)}
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
  const themes = (entry.themes || []).length
    ? `<div class="theme-chips">${entry.themes
        .map((t) => `<span class="theme-chip">${escapeHtml(String(t))}</span>`)
        .join('')}</div>`
    : '';
  return `<article class="insight-card">${head}
    ${themes}
    ${standout}
    <details>
      <summary>Full write-up</summary>
      <div class="md">${renderMarkdown(entry.text)}</div>
    </details>
  </article>`;
}

function themeCountChips(detailsOrMap, side) {
  const details = Array.isArray(detailsOrMap)
    ? detailsOrMap
    : Object.entries(detailsOrMap || {}).map(([theme, count]) => ({
        theme,
        count,
        one_off: count === 1,
      }));
  if (!details.length) return '<p class="status-note">No themes yet.</p>';
  return `<div class="theme-chips">${details
    .map((d) => {
      const active = selectedTheme === d.theme ? ' active' : '';
      const oneOff = d.one_off ? ' one-off' : '';
      return `<button type="button" class="theme-chip${active}${oneOff}" data-theme="${escapeAttr(
        d.theme
      )}" data-side="${escapeAttr(side || '')}">${escapeHtml(d.theme)}<span class="n">×${
        d.count
      }</span></button>`;
    })
    .join('')}</div>`;
}

function findThemeExamples(theme) {
  if (!summary) return [];
  const fromDetails = (summary.theme_details || []).find((d) => d.theme === theme);
  if (fromDetails && fromDetails.examples && fromDetails.examples.length) {
    return fromDetails.examples;
  }
  const contrast = summary.contrast || {};
  for (const list of [contrast.enriched_a, contrast.enriched_b, contrast.shared]) {
    const hit = (list || []).find((e) => e.theme === theme);
    if (hit && hit.examples && hit.examples.length) return hit.examples;
  }
  for (const side of [contrast.a, contrast.b]) {
    const hit = ((side && side.theme_details) || []).find((d) => d.theme === theme);
    if (hit && hit.examples && hit.examples.length) return hit.examples;
  }
  return [];
}

function renderThemeExamples(theme) {
  if (!theme || !compareExamplesEl) {
    if (compareExamplesEl) {
      compareExamplesEl.hidden = true;
      compareExamplesEl.innerHTML = '';
    }
    return;
  }
  const examples = findThemeExamples(theme);
  if (!examples.length) {
    compareExamplesEl.hidden = false;
    compareExamplesEl.innerHTML = `<h4>Examples · ${escapeHtml(theme)}</h4><p class="status-note">No example sessions for this theme in the current rollup.</p>`;
    return;
  }
  compareExamplesEl.hidden = false;
  compareExamplesEl.innerHTML = `
    <h4>Examples · ${escapeHtml(theme)}${
      examples.length === 1 ? ' <span class="meta">(one-off)</span>' : ''
    }</h4>
    <div class="example-cards">
      ${examples
        .map((ex) => {
          const badge = ex.campaign
            ? `<span class="attr-badge">${escapeHtml(ex.campaign)}</span>`
            : ex.meta
              ? `<span class="attr-badge">Meta</span>`
              : '';
          return `<button type="button" class="example-card" data-sid="${escapeAttr(ex.session_id)}">
            <span class="sid">${escapeHtml((ex.session_id || '').slice(0, 8))}…</span>
            ${badge}
            <span class="badge ${ex.outcome === 'purchased' ? 'purchased' : 'all'}">${
              ex.outcome === 'purchased' ? 'Purchased' : 'All'
            }</span>
            ${ex.standout ? `<p class="standout">${escapeHtml(ex.standout)}</p>` : ''}
          </button>`;
        })
        .join('')}
    </div>`;
  compareExamplesEl.querySelectorAll('.example-card').forEach((btn) => {
    btn.addEventListener('click', () => openSession(btn.dataset.sid));
  });
}

function bindThemeChipClicks(root) {
  root.querySelectorAll('button.theme-chip[data-theme]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const theme = btn.dataset.theme;
      selectedTheme = selectedTheme === theme ? null : theme;
      renderSummary(summary);
    });
  });
}

function renderSummary(body) {
  summary = body;
  if (!body || !body.n) {
    compareSummaryEl.innerHTML =
      '<div class="insights-placeholder">Analyze 10 sessions, then pick a split to see what differs between the two groups.</div>';
    renderThemeExamples(null);
    return;
  }

  const contrast = body.contrast || {};
  const sideA = contrast.a || { n: 0, themes: {}, label: 'A' };
  const sideB = contrast.b || { n: 0, themes: {}, label: 'B' };
  const narrative = body.narrative || contrast.narrative || [];
  compareLabelEl.textContent = `${sideA.label} vs ${sideB.label}`;

  const narrativeHtml = narrative.length
    ? `<div class="summary-narrative"><h4>What differs</h4><ul>${narrative
        .map((line) => `<li>${escapeHtml(line)}</li>`)
        .join('')}</ul></div>`
    : '';

  const enrichedA = (contrast.enriched_a || [])
    .slice(0, 5)
    .map((e) => {
      const delta = e.ratio ? `${e.ratio}×` : 'more';
      return (
        `<li><button type="button" class="theme-chip${selectedTheme === e.theme ? ' active' : ''}${
          e.one_off ? ' one-off' : ''
        }" data-theme="${escapeAttr(e.theme)}">${escapeHtml(e.theme)}</button> ` +
        `<span class="delta">${escapeHtml(String(delta))}</span> ` +
        `<span class="meta">(${e.a_count}/${sideA.n} vs ${e.b_count}/${sideB.n})</span></li>`
      );
    })
    .join('');
  const enrichedB = (contrast.enriched_b || [])
    .slice(0, 5)
    .map((e) => {
      const delta = e.ratio ? `${e.ratio}×` : 'more';
      return (
        `<li><button type="button" class="theme-chip${selectedTheme === e.theme ? ' active' : ''}${
          e.one_off ? ' one-off' : ''
        }" data-theme="${escapeAttr(e.theme)}">${escapeHtml(e.theme)}</button> ` +
        `<span class="delta">${escapeHtml(String(delta))}</span> ` +
        `<span class="meta">(${e.b_count}/${sideB.n} vs ${e.a_count}/${sideA.n})</span></li>`
      );
    })
    .join('');

  compareSummaryEl.innerHTML = `
    ${narrativeHtml}
    <div class="summary-grid">
      <div class="summary-col">
        <h4>${escapeHtml(sideA.label)} <span class="n">(n=${sideA.n})</span></h4>
        ${themeCountChips(sideA.theme_details || sideA.themes, 'a')}
        ${enrichedA ? `<ul class="enriched"><li><strong>More common here</strong></li>${enrichedA}</ul>` : ''}
      </div>
      <div class="summary-col">
        <h4>${escapeHtml(sideB.label)} <span class="n">(n=${sideB.n})</span></h4>
        ${themeCountChips(sideB.theme_details || sideB.themes, 'b')}
        ${enrichedB ? `<ul class="enriched"><li><strong>More common here</strong></li>${enrichedB}</ul>` : ''}
      </div>
    </div>
    <p class="status-note">Click a theme for example sessions. Individual write-ups are under Sessions.</p>`;
  bindThemeChipClicks(compareSummaryEl);
  renderThemeExamples(selectedTheme);
}

async function refreshSummary() {
  try {
    const body = await fetchJson(summaryUrl());
    renderSummary(body);
  } catch (err) {
    if (/no cohort/i.test(err.message || '')) {
      compareSummaryEl.innerHTML =
        '<div class="insights-placeholder">Analyze 10 sessions, then pick a split to see what differs between the two groups.</div>';
      renderThemeExamples(null);
      return;
    }
    compareSummaryEl.innerHTML = `<p class="status-note error">${escapeHtml(err.message)}</p>`;
  }
}

function renderCompare(_body) {
  // Per-session write-ups live under Sessions; Split stays aggregate-only.
  compareBodyEl.hidden = true;
  compareBodyEl.innerHTML = '';
}

function buildReviewQueue(body) {
  return (body.sessions || []).filter(
    (s) => s.status === 'done' && ((s.themes && s.themes.length) || s.text || s.insights)
  );
}

function renderReview() {
  reviewQueue = cohort ? buildReviewQueue(cohort) : [];
  if (!reviewQueue.length) {
    reviewProgressEl.textContent = 'No sessions to review';
    reviewBodyEl.innerHTML =
      '<div class="insights-placeholder">Analyze a cohort first, then accept or reject themes here.</div>';
    return;
  }
  if (reviewIndex >= reviewQueue.length) reviewIndex = 0;
  const entry = reviewQueue[reviewIndex];
  reviewProgressEl.textContent = `${reviewIndex + 1} / ${reviewQueue.length} · ${
    entry.qa_status || 'pending'
  }`;
  reviewDraftThemes = [...(entry.themes || [])];
  const campaign = entry.attribution && entry.attribution.utm_campaign;
  const source = entry.attribution && entry.attribution.utm_source;
  const filterLabel = entry.filter === 'purchased' ? 'Purchased' : 'All';

  const themeEdit = `
    <div class="theme-edit" id="review-themes">
      ${reviewDraftThemes
        .map(
          (t) =>
            `<span class="theme-chip">${escapeHtml(t)}<button type="button" class="remove" data-theme="${escapeAttr(
              t
            )}" title="Remove">×</button></span>`
        )
        .join('')}
      <select class="add-theme" id="review-add-theme">
        <option value="">+ add theme</option>
        ${(reviewVocab.length ? reviewVocab : reviewDraftThemes)
          .filter((t) => !reviewDraftThemes.includes(t))
          .map((t) => `<option value="${escapeAttr(t)}">${escapeHtml(t)}</option>`)
          .join('')}
      </select>
    </div>`;

  reviewBodyEl.innerHTML = `
    <article class="review-card" data-sid="${escapeAttr(entry.session_id)}">
      <div class="seg">
        <span class="sid">${escapeHtml(entry.session_id.slice(0, 8))}…</span>
        <span class="badge ${entry.filter === 'purchased' ? 'purchased' : 'all'}">${filterLabel}</span>
        ${campaign ? `<span class="attr-badge">${escapeHtml(campaign)}</span>` : ''}
        ${source ? `<span class="attr-badge">${escapeHtml(source)}</span>` : ''}
        ${entry.attribution && entry.attribution.fbclid ? `<span class="attr-badge">Meta</span>` : ''}
        ${qaBadge(entry.qa_status)}
        <button type="button" class="open" data-sid="${escapeAttr(entry.session_id)}">Open in Replay</button>
      </div>
      <div><strong>Themes</strong></div>
      ${themeEdit}
      ${
        entry.standout
          ? `<p class="insight-standout">${escapeHtml(entry.standout)}</p>`
          : ''
      }
      <details>
        <summary>Full write-up</summary>
        <div class="md">${renderMarkdown(entry.text || '')}</div>
      </details>
      <div class="qa-actions">
        <button type="button" class="primary" data-qa="accepted">Accept into rollups (A)</button>
        <button type="button" class="danger" data-qa="rejected">Reject (R)</button>
        <button type="button" data-qa="skip">Skip (S / N)</button>
      </div>
    </article>`;

  ensureReviewVocab(entry.session_id);

  reviewBodyEl.querySelector('.open')?.addEventListener('click', () => openSession(entry.session_id));
  reviewBodyEl.querySelectorAll('button.remove').forEach((btn) => {
    btn.addEventListener('click', () => {
      reviewDraftThemes = reviewDraftThemes.filter((t) => t !== btn.dataset.theme);
      entry.themes = [...reviewDraftThemes];
      renderReview();
    });
  });
  const addEl = reviewBodyEl.querySelector('#review-add-theme');
  addEl?.addEventListener('change', () => {
    if (!addEl.value) return;
    if (!reviewDraftThemes.includes(addEl.value)) reviewDraftThemes.push(addEl.value);
    entry.themes = [...reviewDraftThemes];
    renderReview();
  });
  reviewBodyEl.querySelectorAll('[data-qa]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.qa;
      if (action === 'skip') {
        reviewIndex = (reviewIndex + 1) % reviewQueue.length;
        renderReview();
        return;
      }
      submitReview(entry.session_id, action, reviewDraftThemes);
    });
  });
}

async function ensureReviewVocab(sessionId) {
  if (reviewVocab.length) return;
  try {
    const body = await fetchJson(qaUrl(sessionId));
    reviewVocab = body.vocab || [];
    if (reviewVocab.length) renderReview();
  } catch {
    /* leave draft themes only */
  }
}

async function submitReview(sessionId, status, themes) {
  try {
    await fetchJson(qaUrl(sessionId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, themes }),
    });
    const entry = (cohort.sessions || []).find((s) => s.session_id === sessionId);
    if (entry) {
      entry.qa_status = status;
      entry.themes = [...themes];
      entry.qa = { status, themes };
    }
    reviewIndex = (reviewIndex + 1) % Math.max(reviewQueue.length, 1);
    renderQueue(cohort);
    renderCompare(cohort);
    renderReview();
    refreshSummary().catch(() => {});
  } catch (err) {
    reviewProgressEl.textContent = err.message;
  }
}

function onReviewKeydown(e) {
  if (activeTab !== 'review' || !document.body.classList.contains('mode-insights')) return;
  if (e.target.closest('input, select, textarea')) return;
  const key = e.key.toLowerCase();
  if (!reviewQueue.length) return;
  const entry = reviewQueue[reviewIndex];
  if (!entry) return;
  if (key === 'a') {
    e.preventDefault();
    submitReview(entry.session_id, 'accepted', reviewDraftThemes);
  } else if (key === 'r') {
    e.preventDefault();
    submitReview(entry.session_id, 'rejected', reviewDraftThemes);
  } else if (key === 'n' || key === 's') {
    e.preventDefault();
    reviewIndex = (reviewIndex + 1) % reviewQueue.length;
    renderReview();
  }
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
  if (activeTab === 'review') renderReview();
  const hasInsights = (body.sessions || []).some(
    (s) => s.status === 'done' && ((s.themes && s.themes.length) || s.text)
  );
  if (hasInsights) {
    refreshSummary().catch(() => {});
  } else {
    compareSummaryEl.innerHTML =
      body.status === 'running' || body.status === 'queued'
        ? '<div class="insights-placeholder">Analyzing sessions… Split results will appear here when themes are ready.</div>'
        : '<div class="insights-placeholder">Analyze 10 sessions, then pick a split to see what differs between the two groups.</div>';
    renderThemeExamples(null);
  }

  const running = body.status === 'running' || body.status === 'queued';
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
    setTab('compare');
  }

  schedulePoll();
}

function resetEmptyState() {
  cohort = null;
  summary = null;
  selectedTheme = null;
  autoSwitchedForRun = false;
  labelEl.textContent = 'Sessions';
  compareLabelEl.textContent = 'Sample of 10';
  progressEl.textContent = 'No cohort yet';
  compareProgressEl.textContent = 'No cohort yet';
  reviewProgressEl.textContent = 'No sessions to review';
  syncRunButtons();
  compareSummaryEl.innerHTML =
    '<div class="insights-placeholder">Analyze 10 sessions, then pick a split to see what differs between the two groups.</div>';
  renderThemeExamples(null);
  renderEmpty(
    'Run analysis on 5 purchased and 5 other sessions. Open any row to watch it in Replay.'
  );
  compareBodyEl.hidden = true;
  compareBodyEl.innerHTML = '';
  reviewBodyEl.innerHTML =
    '<div class="insights-placeholder">Optional: fix bad theme tags before you trust a split.</div>';
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
  if (splitEl) {
    splitEl.addEventListener('change', () => {
      selectedTheme = null;
      if (cohort) refreshSummary().catch(() => {});
    });
  }
  document.addEventListener('keydown', onReviewKeydown);
  setTab('compare');

  const projectSelect = document.getElementById('project-select');
  projectSelect.addEventListener('change', () => {
    setProject(projectSelect.value);
    if (document.body.classList.contains('mode-insights')) {
      refreshCohort().catch(() => {});
    }
  });
}
