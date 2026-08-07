import { currentProject, fetchJson, setProject } from './api.js';
import { writeHash } from './deeplink.js';
import { loadSession } from './session-player.js';

const POLL_MS = 2000;

const modeButtons = document.querySelectorAll('#mode-switch button');
const tabButtons = document.querySelectorAll('.insights-tabs button[data-insights-tab]');
const runBtn = document.getElementById('cohort-run');
const cohortSelectEl = document.getElementById('cohort-select');
const renameBtn = document.getElementById('cohort-rename');
const labelEl = document.getElementById('cohort-label');
const progressEl = document.getElementById('cohort-progress');
const cutLabelEl = document.getElementById('cut-label');
const cutProgressEl = document.getElementById('cut-progress');
const cutRunBtn = document.getElementById('cut-run');
const cutBodyEl = document.getElementById('cut-body');
const tableWrapEl = document.getElementById('cohort-table-wrap');
const pickerEl = document.getElementById('cut-picker');
const queuePanelEl = document.getElementById('insights-queue');
const cutPanelEl = document.getElementById('insights-cut');

let analysisEnabled = false;
let pollTimer = null;
let cutPollTimer = null;
let cohort = null;
let cohortId = null;
let activeTab = 'cut';
let autoSwitchedForRun = false;
let facets = null;
let slice = emptySlice();

export function setInsightsAnalysisEnabled(on) {
  analysisEnabled = !!on;
  syncRunButtons();
}

function cohortIsRunning() {
  return cohort && (cohort.status === 'running' || cohort.status === 'queued');
}

function syncRunButtons() {
  const running = cohortIsRunning();
  runBtn.disabled = !analysisEnabled || running;
  runBtn.title = analysisEnabled ? '' : 'Analysis is not configured on this server';
  runBtn.textContent = running ? 'Analyzing…' : 'Analyze 10 sessions';
  renameBtn.disabled = !cohortId;
  syncCutRunButton();
}

function analyzedCount() {
  if (!cohort) return 0;
  return (cohort.sessions || []).filter((s) => s.status === 'done' && s.text).length;
}

function syncCutRunButton() {
  if (!cutRunBtn) return;
  const ready = analysisEnabled && analyzedCount() > 0 && !cutPollTimer;
  cutRunBtn.disabled = !ready;
  if (!analysisEnabled) {
    cutRunBtn.title = 'Analysis is not configured on this server';
  } else if (!analyzedCount()) {
    cutRunBtn.title = 'Analyze a cohort first';
  } else if (sliceIsEmpty(slice)) {
    cutRunBtn.title = 'No filters set — this reads every analyzed session in the cohort';
  } else {
    cutRunBtn.title = '';
  }
}

function withProject(params) {
  if (currentProject) params.set('project', currentProject);
  return params;
}

function cohortsUrl() {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts${qs ? `?${qs}` : ''}`;
}

function cohortUrl(id) {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts/${encodeURIComponent(id)}${qs ? `?${qs}` : ''}`;
}

function createCohortUrl() {
  const params = withProject(new URLSearchParams());
  params.set('purchased', '5');
  params.set('all', '5');
  return `/api/cohorts?${params.toString()}`;
}

function retryUrl(id, sessionId) {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts/${encodeURIComponent(id)}/sessions/${encodeURIComponent(
    sessionId
  )}/retry${qs ? `?${qs}` : ''}`;
}

function facetsUrl(id) {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts/${encodeURIComponent(id)}/facets${qs ? `?${qs}` : ''}`;
}

/** One group's filter. Every field is a single value; '' means "any". */
const SLICE_FIELDS = ['outcome', 'device', 'utm_source', 'utm_campaign'];

function emptySlice() {
  return { outcome: '', device: '', utm_source: '', utm_campaign: '' };
}

function sliceIsEmpty(spec) {
  return SLICE_FIELDS.every((field) => !spec[field]);
}

function cutUrl(id, { run = false } = {}) {
  const params = withProject(new URLSearchParams());
  for (const field of SLICE_FIELDS) {
    if (slice[field]) params.set(field, slice[field]);
  }
  if (run) params.set('run', '1');
  return `/api/cohorts/${encodeURIComponent(id)}/cut?${params.toString()}`;
}

/** A cut miss is a 404 carrying {status:"absent"} — a normal state, not an error. */
async function fetchCut(id, opts) {
  const res = await fetch(cutUrl(id, opts));
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* fall through to the status check */
  }
  if (res.status === 404 && body && body.status === 'absent') return body;
  if (!res.ok) {
    throw new Error((body && body.error) || `Server returned ${res.status}.`);
  }
  return body || {};
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function stopCutPolling() {
  if (cutPollTimer) {
    clearTimeout(cutPollTimer);
    cutPollTimer = null;
  }
}

function schedulePoll() {
  stopPolling();
  if (!document.body.classList.contains('mode-insights')) return;
  if (!cohortIsRunning()) return;
  pollTimer = setTimeout(() => {
    refreshCohort().catch((err) => {
      progressEl.textContent = err.message;
    });
  }, POLL_MS);
}

function setMode(mode) {
  const next = mode === 'insights' ? 'insights' : 'replay';
  document.body.classList.toggle('mode-insights', next === 'insights');
  modeButtons.forEach((btn) => btn.classList.toggle('active', btn.dataset.mode === next));
  if (next === 'insights') {
    refreshCohorts().catch((err) => {
      progressEl.textContent = err.message;
    });
  } else {
    stopPolling();
    stopCutPolling();
  }
}

function setTab(tab) {
  activeTab = tab === 'queue' ? 'queue' : 'cut';
  tabButtons.forEach((btn) => {
    if (btn.disabled) return;
    btn.classList.toggle('active', btn.dataset.insightsTab === activeTab);
  });
  cutPanelEl.hidden = activeTab !== 'cut';
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
      const campaign =
        (entry.attribution && entry.attribution.utm_campaign) ||
        (entry.attribution && entry.attribution.utm_source) ||
        '—';
      const err = entry.error
        ? `<div style="color:#b91c1c;font-size:11px;margin-top:2px">${escapeHtml(entry.error)}</div>`
        : '';
      const retry =
        status === 'error'
          ? `<button type="button" class="row-retry" data-retry="${escapeAttr(
              entry.session_id
            )}">Retry</button>`
          : '';
      return `
        <tr class="openable" data-sid="${escapeAttr(entry.session_id)}">
          <td class="sid">${escapeHtml(entry.session_id.slice(0, 8))}…</td>
          <td><span class="badge ${filterClass}">${filterLabel}</span></td>
          <td>${escapeHtml(String(campaign))}</td>
          <td>
            <span class="badge ${escapeAttr(status)}">${escapeHtml(status)}</span>
            ${retry}
            ${err}
          </td>
          <td class="standout">${entry.standout ? escapeHtml(entry.standout) : '—'}</td>
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
          <th>What stands out</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  tableWrapEl.querySelectorAll('tr.openable').forEach((tr) => {
    tr.addEventListener('click', (e) => {
      if (e.target.closest('[data-retry]')) return;
      openSession(tr.dataset.sid);
    });
  });
  tableWrapEl.querySelectorAll('[data-retry]').forEach((btn) => {
    btn.addEventListener('click', () => retrySession(btn.dataset.retry, btn));
  });
}

async function retrySession(sessionId, btn) {
  if (!analysisEnabled || !sessionId || !cohortId) return;
  btn.disabled = true;
  btn.textContent = 'Retrying…';
  try {
    const body = await fetchJson(retryUrl(cohortId, sessionId), { method: 'POST' });
    renderCohort(body);
  } catch (err) {
    progressEl.textContent = err.message;
    btn.disabled = false;
    btn.textContent = 'Retry';
  }
}

const HINT_TEXT = 'Leave every filter on "any" to read the whole cohort.';

const FACET_LABELS = {
  outcome: 'outcome',
  device: 'device',
  utm_source: 'source',
  utm_campaign: 'campaign',
};

function facetRow(field, items) {
  if (!items || !items.length) return '';
  return `<label class="facet">
    <span>${escapeHtml(FACET_LABELS[field] || field)}</span>
    <select data-field="${field}">
      <option value="">any</option>
      ${items
        .map(
          (i) =>
            `<option value="${escapeAttr(i.value)}"${
              slice[field] === i.value ? ' selected' : ''
            }>${escapeHtml(i.label || i.value)} (${i.count})</option>`
        )
        .join('')}
    </select>
  </label>`;
}

function renderPicker() {
  if (!pickerEl) return;
  pickerEl.style.display = facets ? '' : 'none';
  if (!facets) return;

  const host = pickerEl.querySelector('.facets');
  if (host) {
    host.innerHTML = SLICE_FIELDS.map((field) => facetRow(field, facets[field])).join('');
  }

  pickerEl.querySelectorAll('select[data-field]').forEach((sel) => {
    sel.addEventListener('change', () => {
      slice[sel.dataset.field] = sel.value;
      onSliceChanged();
    });
  });
  updatePickerCounts();
}

/** Live n=, so an empty group is obvious before spending a call. */
function updatePickerCounts() {
  if (!pickerEl || !cohort) return;
  const rows = (cohort.sessions || []).filter((s) => s.status === 'done' && s.text);
  const n = rows.filter((r) => rowMatches(r, slice)).length;
  const hintEl = pickerEl.querySelector('[data-hint-text]');
  const countEl = pickerEl.querySelector('[data-count="n"]');
  // The "leave it on any" nudge only helps before a filter is set.
  if (hintEl) hintEl.textContent = sliceIsEmpty(slice) ? HINT_TEXT : '';
  if (countEl) {
    countEl.textContent = `${n} of ${rows.length} analyzed session(s) selected`;
  }
}

/** Client-side mirror of row_matches_slice, for the live counts only. */
function rowMatches(row, spec) {
  if (sliceIsEmpty(spec)) return true;
  if (spec.outcome && (row.outcome || 'rest') !== spec.outcome) return false;
  if (spec.device && (row.device || '') !== spec.device) return false;
  const attrs = row.attribution || {};
  const norm = (v) => String(v || '').trim().toLowerCase();
  if (spec.utm_source && norm(attrs.utm_source) !== spec.utm_source) return false;
  if (spec.utm_campaign && norm(attrs.utm_campaign) !== spec.utm_campaign) return false;
  return true;
}

function onSliceChanged() {
  renderPicker();
  stopCutPolling();
  loadCut().catch(() => {});
}

async function loadFacets() {
  if (!cohortId) return;
  try {
    facets = await fetchJson(facetsUrl(cohortId));
    renderPicker();
  } catch {
    facets = null;
  }
}

function renderCutPlaceholder(message) {
  cutBodyEl.innerHTML = `<div class="insights-placeholder">${escapeHtml(message)}</div>`;
}

/** Section order mirrors the prompt: journey, attention, intent, blockers. */
const CUT_SECTIONS = [
  { key: 'journey', title: 'How they moved through the site', empty: 'The write-ups do not describe a common route.' },
  { key: 'attention', title: 'What held their attention', empty: 'Nothing held attention consistently across these sessions.' },
  { key: 'intent', title: 'Signals of intent', empty: 'No consistent buying signals stood out.' },
  { key: 'blockers', title: 'What stopped them', cls: 'friction', empty: 'No blockers were reported in these write-ups.' },
  { key: 'caveats', title: 'Worth checking', cls: 'caveats' },
];

/** An insight item: observation, its share of the group, and what to do. */
function insightItem(item) {
  if (typeof item === 'string') return `<li>${escapeHtml(item)}</li>`;
  const share = item.share
    ? `<span class="cut-share">${escapeHtml(item.share)}</span>`
    : '';
  const action = item.action
    ? `<p class="cut-action">${escapeHtml(item.action)}</p>`
    : '';
  return `<li>${share}${escapeHtml(item.observation || '')}${action}</li>`;
}

function renderCut(body) {
  const analysis = body.analysis || {};
  const n = body.n || 0;
  cutLabelEl.textContent = body.label || 'Sessions';

  // Small groups are the norm here; say so rather than letting counts imply rigour.
  const thin = n > 0 && n < 3 ? '<span class="cut-thin">thin evidence</span>' : '';

  const headline = analysis.headline
    ? `<div class="cut-headline">${escapeHtml(analysis.headline)}</div>`
    : '';

  const sections = CUT_SECTIONS.map(({ key, title, cls, empty }) => {
    const items = analysis[key] || [];
    // Caveats have no empty copy: an absent caveat list is not worth a block.
    if (!items.length && !empty) return '';
    const count = key === 'journey' ? `<span class="n">(n=${n})</span>${thin}` : '';
    return `<div class="cut-block ${cls || ''}">
      <h4>${escapeHtml(title)}${count}</h4>
      ${
        items.length
          ? `<ul>${items.map(insightItem).join('')}</ul>`
          : `<p class="cut-empty">${escapeHtml(empty)}</p>`
      }
    </div>`;
  }).join('');

  const meta = `<p class="cut-meta">
    Read from ${n} analyzed session write-up(s) in this cohort.
    Written by ${escapeHtml(body.model || 'the model')}.
    Cohort membership is fixed, so this cut is cached and will not be recomputed.
  </p>`;

  cutBodyEl.innerHTML = headline + sections + meta;
}

async function pollCut() {
  if (!cohortId) return;
  try {
    const body = await fetchCut(cohortId);
    if (body.status === 'running') {
      cutPollTimer = setTimeout(pollCut, POLL_MS);
      return;
    }
    stopCutPolling();
    if (body.status === 'error') {
      cutProgressEl.textContent = body.error || 'Cut analysis failed.';
      renderCutPlaceholder(body.error || 'Cut analysis failed.');
    } else if (body.status === 'absent') {
      showCutOffer();
    } else {
      cutProgressEl.textContent = 'Cached';
      renderCut(body);
    }
  } catch (err) {
    stopCutPolling();
    cutProgressEl.textContent = err.message;
  } finally {
    syncCutRunButton();
  }
}

function showCutOffer() {
  const n = analyzedCount();
  cutProgressEl.textContent = n ? 'Not analyzed yet' : 'No analyzed sessions';
  renderCutPlaceholder(
    n
      ? 'This group has not been analyzed yet. Click "Analyze this group" to have the model read these write-ups.'
      : 'Analyze a cohort first, then pick a group.'
  );
}

/** Show a cached cut when one exists; never spends a model call on its own. */
async function loadCut() {
  stopCutPolling();
  if (!cohortId) {
    renderCutPlaceholder('Analyze a cohort, then pick a group.');
    return;
  }
  cutProgressEl.textContent = 'Checking…';
  try {
    const body = await fetchCut(cohortId);
    if (body.status === 'running') {
      cutProgressEl.textContent = 'Analyzing…';
      renderCutPlaceholder('Reading the write-ups in this group. This takes a few seconds.');
      cutPollTimer = setTimeout(pollCut, POLL_MS);
      return;
    }
    if (body.status === 'absent') {
      showCutOffer();
      return;
    }
    if (body.status === 'error') {
      cutProgressEl.textContent = body.error || 'Cut analysis failed.';
      renderCutPlaceholder(body.error || 'Cut analysis failed.');
      return;
    }
    cutProgressEl.textContent = body.cached ? 'Cached' : 'Ready';
    renderCut(body);
  } catch (err) {
    cutProgressEl.textContent = err.message;
    renderCutPlaceholder(err.message);
  } finally {
    syncCutRunButton();
  }
}

async function runCut() {
  if (!cohortId || !analysisEnabled) return;
  stopCutPolling();
  cutRunBtn.disabled = true;
  cutProgressEl.textContent = 'Analyzing…';
  renderCutPlaceholder('Reading the write-ups in this group. This takes a few seconds.');
  try {
    const body = await fetchCut(cohortId, { run: true });
    if (body.status === 'running') {
      cutPollTimer = setTimeout(pollCut, POLL_MS);
      return;
    }
    cutProgressEl.textContent = 'Ready';
    renderCut(body);
  } catch (err) {
    cutProgressEl.textContent = err.message;
    renderCutPlaceholder(err.message);
  } finally {
    syncCutRunButton();
  }
}

function renderCohortOptions(cohorts) {
  if (!cohorts.length) {
    cohortSelectEl.innerHTML = '<option value="">No cohorts yet</option>';
    return;
  }
  cohortSelectEl.innerHTML = cohorts
    .map((c) => {
      const prog = c.progress || {};
      const suffix =
        c.status === 'running' || c.status === 'queued'
          ? ` — analyzing ${prog.done || 0}/${prog.total || 0}`
          : '';
      return `<option value="${escapeAttr(c.id)}">${escapeHtml(
        c.label || c.id
      )}${escapeHtml(suffix)}</option>`;
    })
    .join('');
  if (cohortId) cohortSelectEl.value = cohortId;
}

function renderCohort(body) {
  cohort = body;
  cohortId = body.id || cohortId;
  const text = progressText(body);
  labelEl.textContent = body.label || 'Sessions';
  progressEl.textContent = text;
  syncRunButtons();
  renderQueue(body);
  updatePickerCounts();

  const running = cohortIsRunning();
  if (running && !autoSwitchedForRun) {
    setTab('queue');
    autoSwitchedForRun = true;
  } else if (!running && autoSwitchedForRun) {
    setTab('cut');
    autoSwitchedForRun = false;
  }

  schedulePoll();
}

function resetEmptyState() {
  cohort = null;
  cohortId = null;
  autoSwitchedForRun = false;
  facets = null;
  slice = emptySlice();
  if (pickerEl) pickerEl.style.display = 'none';
  labelEl.textContent = 'Sessions';
  cutLabelEl.textContent = 'Sessions';
  progressEl.textContent = 'No cohort yet';
  cutProgressEl.textContent = 'No cohort yet';
  syncRunButtons();
  renderCutPlaceholder(
    'Analyze a cohort, then pick a group to see what those sessions have in common.'
  );
  renderEmpty(
    'Run analysis on 5 purchased and 5 other sessions. Open any row to watch it in Replay.'
  );
}

/** Reload the cohort list, then the selected cohort and its cut. */
export async function refreshCohorts() {
  const body = await fetchJson(cohortsUrl());
  const cohorts = body.cohorts || [];
  if (!cohorts.length) {
    renderCohortOptions([]);
    resetEmptyState();
    return;
  }
  if (!cohortId || !cohorts.some((c) => c.id === cohortId)) {
    cohortId = cohorts[0].id;
  }
  renderCohortOptions(cohorts);
  await refreshCohort();
  await loadFacets();
  await loadCut();
}

export async function refreshCohort() {
  if (!cohortId) {
    resetEmptyState();
    return;
  }
  try {
    const body = await fetchJson(cohortUrl(cohortId));
    renderCohort(body);
  } catch (err) {
    stopPolling();
    if (/no cohort|no such cohort/i.test(err.message || '')) {
      resetEmptyState();
      return;
    }
    progressEl.textContent = err.message;
    throw err;
  }
}

async function runCohort() {
  if (!analysisEnabled || runBtn.disabled) return;
  autoSwitchedForRun = false;
  runBtn.disabled = true;
  runBtn.textContent = 'Starting…';
  progressEl.textContent = 'Sampling sessions…';
  try {
    const body = await fetchJson(createCohortUrl(), { method: 'POST' });
    cohortId = body.id;
    renderCohort(body);
    await refreshCohorts();
  } catch (err) {
    progressEl.textContent = err.message;
    syncRunButtons();
  }
}

async function renameCohort() {
  if (!cohortId || !cohort) return;
  const next = window.prompt('Name this cohort', cohort.label || '');
  if (next === null) return;
  const label = next.trim();
  if (!label) return;
  try {
    const body = await fetchJson(cohortUrl(cohortId), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    });
    renderCohort(body);
    await refreshCohorts();
  } catch (err) {
    progressEl.textContent = err.message;
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
  renameBtn.addEventListener('click', renameCohort);
  if (cutRunBtn) cutRunBtn.addEventListener('click', runCut);
  cohortSelectEl.addEventListener('change', () => {
    cohortId = cohortSelectEl.value || null;
    stopCutPolling();
    // A different cohort has different facet values, so reset the picker.
    slice = emptySlice();
    refreshCohort()
      .then(loadFacets)
      .then(loadCut)
      .catch(() => {});
  });
  setTab('cut');

  const projectSelect = document.getElementById('project-select');
  projectSelect.addEventListener('change', () => {
    setProject(projectSelect.value);
    cohortId = null;
    if (document.body.classList.contains('mode-insights')) {
      refreshCohorts().catch(() => {});
    }
  });
}
