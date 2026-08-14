import { currentProject, fetchJson, setProject } from '../api.js';
import { parseFrames, splitSettled } from '../chat-stream.js';
import { linkCitations } from '../citations.js';
import { writeHash } from '../deeplink.js';
import { loadSession, setPendingSeek } from '../session-player.js';
import { cutHtml } from './cutview.js';
import { chatInline, chatMarkdown } from './markdown.js';
import {
  SLICE_FIELDS,
  emptySlice,
  facetsFromRows,
  rowMatches,
  sliceIsEmpty,
  sliceNameBits,
  suggestCohortName,
} from './slices.js';
import {
  COHORT_MAX_SESSIONS,
  analyzedCount,
  analyzedSessions,
  clampGroupSize,
  escapeAttr,
  escapeHtml,
  formatViewClock,
  formatViewFacet,
  localDateTimeMs,
  progressText,
} from './text.js';

const POLL_MS = 2000;
const COHORT_MIN_DURATION_S = 5;

const modeButtons = document.querySelectorAll('#mode-switch button');
const tabButtons = document.querySelectorAll('.insights-tabs button[data-insights-tab]');
const runBtn = document.getElementById('cohort-run');
const analyzeBtn = document.getElementById('cohort-analyze');
const pauseBtn = document.getElementById('cohort-pause');
const cohortSelectEl = document.getElementById('cohort-select');
const renameBtn = document.getElementById('cohort-rename');
const deleteBtn = document.getElementById('cohort-delete');
const viewBtn = document.getElementById('cohort-view');
const viewDialog = document.getElementById('cohort-view-dialog');
const viewTitleEl = document.getElementById('cohort-view-title');
const viewSelectionEl = document.getElementById('cohort-view-selection');
const viewCloseBtn = document.getElementById('cohort-view-close');
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
const chatEl = document.getElementById('cut-chat');
const chatScopeEl = document.getElementById('cut-chat-scope');
const chatLogEl = document.getElementById('cut-chat-log');
const chatFormEl = document.getElementById('cut-chat-form');
const chatInputEl = document.getElementById('cut-chat-input');
const chatSendEl = document.getElementById('cut-chat-send');
const chatClearEl = document.getElementById('cut-chat-clear');
const createDialog = document.getElementById('cohort-create-dialog');
const createForm = document.getElementById('cohort-create-form');
const createCancelBtn = document.getElementById('cohort-create-cancel');
const createSubmitBtn = document.getElementById('cohort-create-submit');
const createErrorEl = document.getElementById('cohort-create-error');
const aNInput = document.getElementById('cohort-a-n');
const bNInput = document.getElementById('cohort-b-n');
const addBInput = document.getElementById('cohort-add-b');
const groupBEl = document.getElementById('cohort-group-b');
const splitHintEl = document.getElementById('cohort-split-hint');
const minSInput = document.getElementById('cohort-min-s');
const nameInput = document.getElementById('cohort-name');
const startInput = document.getElementById('cohort-start');
const endInput = document.getElementById('cohort-end');
const orderInput = document.getElementById('cohort-order');
const limitRangeInput = document.getElementById('cohort-limit-range');
const timeRangeEl = document.getElementById('cohort-time-range');
const createFacetsAEl = document.getElementById('cohort-create-facets-a');
const createFacetsBEl = document.getElementById('cohort-create-facets-b');
const timeHintEl = document.getElementById('cohort-time-hint');

let createFacets = null;
let createSliceA = { outcome: '', device: '', page: '', utm_source: '', utm_campaign: '' };
let createSliceB = { outcome: '', device: '', page: '', utm_source: '', utm_campaign: '' };
let samplingCohort = false;

let analysisEnabled = false;
let pollTimer = null;
let cutPollTimer = null;
let cohort = null;
let cohortId = null;
let activeTab = 'cut';
let autoSwitchedForRun = false;
let facets = null;
let slice = emptySlice();
// True while an already-computed cut is on screen — the only state where
// re-analyzing means anything.
let cutIsShowing = false;

export function setInsightsAnalysisEnabled(on) {
  analysisEnabled = !!on;
  syncRunButtons();
}

function cohortIsRunning() {
  return cohort && (cohort.status === 'running' || cohort.status === 'queued');
}

function cohortCanAnalyze() {
  if (!cohort || !analysisEnabled) return false;
  if (cohortIsRunning()) return false;
  if (cohort.status === 'done') return false;
  const prog = cohort.progress || {};
  // Stale rows count as pending: their write-ups are gone, so the cohort has
  // real work left even though nothing failed and nothing is queued.
  const pending =
    (prog.queued || 0) + (prog.running || 0) + (prog.error || 0) + (prog.stale || 0) > 0 ||
    cohort.status === 'idle' ||
    cohort.status === 'paused';
  return pending;
}

function syncRunButtons() {
  // Creating a cohort always adds a new one — never replaces the selected cohort.
  runBtn.disabled = samplingCohort;
  runBtn.title = samplingCohort ? 'Sampling…' : 'New cohort';
  runBtn.setAttribute('aria-label', 'New cohort');
  runBtn.textContent = samplingCohort ? '…' : '+';
  renameBtn.disabled = !cohortId || samplingCohort;
  renameBtn.title = cohortId ? 'Rename cohort' : 'Select a cohort to rename';
  renameBtn.textContent = '✎';
  if (viewBtn) {
    viewBtn.disabled = !cohortId || samplingCohort;
    viewBtn.title = cohortId ? 'View cohort settings' : 'Select a cohort to view';
  }
  if (deleteBtn) {
    deleteBtn.disabled = !cohortId || samplingCohort;
    deleteBtn.title = cohortId ? 'Delete cohort' : 'Select a cohort to delete';
    deleteBtn.textContent = '🗑';
  }

  if (analyzeBtn) {
    const can = cohortCanAnalyze();
    const running = cohortIsRunning();
    const done = cohort && cohort.status === 'done';
    analyzeBtn.disabled = !can;
    // Lives under the Sessions heading; hide while a run is in flight (Pause
    // takes over) or when every session already has a write-up.
    analyzeBtn.hidden = !cohortId || running || done;
    if (!analysisEnabled) {
      analyzeBtn.title = 'Analysis is not configured on this server';
      analyzeBtn.disabled = true;
      analyzeBtn.hidden = false;
      analyzeBtn.textContent = 'Start analysis';
    } else if (!cohortId) {
      analyzeBtn.title = 'Create or select a cohort first';
      analyzeBtn.textContent = 'Start analysis';
    } else if (cohort && cohort.status === 'paused') {
      analyzeBtn.title = 'Resume analysis for remaining sessions';
      analyzeBtn.textContent = 'Resume analysis';
    } else {
      analyzeBtn.title = 'Start Gemini analysis for this cohort’s sessions';
      analyzeBtn.textContent = 'Start analysis';
    }
  }

  if (pauseBtn) {
    const running = cohortIsRunning();
    pauseBtn.hidden = !running;
    pauseBtn.disabled = !running;
    pauseBtn.textContent = 'Pause';
    pauseBtn.title = running
      ? 'Stop after the current step; finished write-ups are kept'
      : '';
  }

  syncCutRunButton();
}


function syncCutRunButton() {
  if (!cutRunBtn) return;
  const ready = analysisEnabled && analyzedCount(cohort) > 0 && !cutPollTimer;
  cutRunBtn.disabled = !ready;
  cutRunBtn.hidden = false;
  if (cutIsShowing) {
    cutRunBtn.textContent = 'Run analysis again';
    cutRunBtn.title = ready
      ? 'Discard this cached answer and ask the model again'
      : !analyzedCount(cohort)
        ? 'Analyze sessions on the Sessions tab first'
        : cutRunBtn.title;
  } else {
    cutRunBtn.textContent = 'Analyze';
    if (!analysisEnabled) {
      cutRunBtn.title = 'Analysis is not configured on this server';
    } else if (!analyzedCount(cohort)) {
      cutRunBtn.title = 'Start analysis on the Sessions tab first';
    } else if (sliceIsEmpty(slice)) {
      cutRunBtn.title = 'No filters set — this reads every analyzed session in the cohort';
    } else {
      cutRunBtn.title = 'Ask the model what this filtered group has in common';
    }
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

function appendSliceParams(params, sampleSlice, prefix = '') {
  for (const field of SLICE_FIELDS) {
    if (sampleSlice && sampleSlice[field]) {
      params.set(`${prefix}${field}`, sampleSlice[field]);
    }
  }
}

function createCohortUrl({
  a,
  b,
  minDurationMs,
  sliceA,
  sliceB,
  startMs,
  endMs,
  order,
  label,
}) {
  const params = withProject(new URLSearchParams());
  params.set('a', String(a));
  if (b > 0) params.set('b', String(b));
  params.set('min_duration_ms', String(minDurationMs));
  params.set('order', order || 'latest');
  if (label) params.set('label', label);
  if (startMs != null) params.set('start_ms', String(startMs));
  if (endMs != null) params.set('end_ms', String(endMs));
  appendSliceParams(params, sliceA);
  if (b > 0) appendSliceParams(params, sliceB, 'b_');
  return `/api/cohorts?${params.toString()}`;
}

function poolFacetsUrl(minDurationMs) {
  const params = withProject(new URLSearchParams());
  params.set('min_duration_ms', String(minDurationMs));
  return `/api/facets?${params.toString()}`;
}



function readCreateForm() {
  const addB = !!(addBInput && addBInput.checked);
  let a = clampGroupSize(aNInput?.value, 100);
  let b = addB ? clampGroupSize(bNInput?.value, 100) : 0;
  if (a + b > COHORT_MAX_SESSIONS) {
    if (addB) {
      const half = Math.floor(COHORT_MAX_SESSIONS / 2);
      a = half;
      b = COHORT_MAX_SESSIONS - half;
    } else {
      a = COHORT_MAX_SESSIONS;
    }
  }
  const minS = Math.max(
    COHORT_MIN_DURATION_S,
    Math.round(Number(minSInput?.value) || COHORT_MIN_DURATION_S)
  );
  let startMs = null;
  let endMs = null;
  if (limitRangeInput && limitRangeInput.checked) {
    startMs = localDateTimeMs(startInput?.value);
    endMs = localDateTimeMs(endInput?.value);
    if (startMs != null && endMs != null && startMs > endMs) {
      const swap = startMs;
      startMs = endMs;
      endMs = swap;
    }
  }
  const order = orderInput?.value === 'earliest' ? 'earliest' : 'latest';
  const label = (nameInput?.value || '').trim().slice(0, 120);
  return {
    a,
    b,
    addB,
    total: a + b,
    minDurationMs: minS * 1000,
    startMs,
    endMs,
    order,
    label,
    sliceA: { ...createSliceA },
    sliceB: { ...createSliceB },
  };
}

function createFacetRow(field, items, selected, dataAttr) {
  if (!items || !items.length) {
    if (field !== 'outcome') return '';
  }
  const extras =
    field === 'outcome'
      ? `<option value="checkout,rest"${
          selected === 'checkout,rest' ? ' selected' : ''
        }>did not purchase</option>`
      : '';
  const options = (items || [])
    .map(
      (i) =>
        `<option value="${escapeAttr(i.value)}"${
          selected === i.value ? ' selected' : ''
        }>${escapeHtml(i.label || i.value)} (${i.count})</option>`
    )
    .join('');
  return `<label class="facet">
    <span>${escapeHtml(FACET_LABELS[field] || field)}</span>
    <select data-${dataAttr}="${field}">
      <option value="">any</option>
      ${extras}
      ${options}
    </select>
  </label>`;
}

function renderCreateFacetHost(host, sliceState, dataAttr) {
  if (!host) return;
  if (!createFacets) {
    host.innerHTML =
      '<span class="time-hint">Loading filters from recorded sessions…</span>';
    return;
  }
  host.innerHTML = SLICE_FIELDS.map((field) =>
    createFacetRow(field, createFacets[field], sliceState[field], dataAttr)
  ).join('');
  host.querySelectorAll(`select[data-${dataAttr}]`).forEach((sel) => {
    sel.addEventListener('change', () => {
      sliceState[sel.getAttribute(`data-${dataAttr}`)] = sel.value;
      syncCreateFormHints();
    });
  });
}

function renderCreateFacets() {
  renderCreateFacetHost(createFacetsAEl, createSliceA, 'create-a');
  if (addBInput && addBInput.checked) {
    renderCreateFacetHost(createFacetsBEl, createSliceB, 'create-b');
  }
}


function syncCreateFormHints() {
  if (!splitHintEl || !timeHintEl) return;
  if (groupBEl) groupBEl.hidden = !(addBInput && addBInput.checked);
  if (timeRangeEl) {
    timeRangeEl.hidden = !(limitRangeInput && limitRangeInput.checked);
  }
  const opts = readCreateForm();
  if (nameInput && nameInput.dataset.autofill !== '0') {
    const suggestion = suggestCohortName(opts);
    if (nameInput.value !== suggestion) {
      nameInput.value = suggestion;
    }
  }
  if (opts.addB) {
    splitHintEl.textContent = `${opts.a} in A + ${opts.b} in B (${opts.total} total, max ${COHORT_MAX_SESSIONS})`;
  } else {
    splitHintEl.textContent = `${opts.a} session(s) in group A`;
  }
  renderCreateFacets();
  const coldHoursLow = Math.max(1, Math.round((opts.total * 1.5) / 60));
  const coldHoursHigh = Math.max(coldHoursLow + 1, Math.round((opts.total * 3) / 60));
  timeHintEl.textContent =
    `Sessions under ${COHORT_MIN_DURATION_S}s are never included. Sampling is instant; ` +
    `analysis reconstructs each page first (often ~1–3 min/session when cold), so ` +
    `${opts.total} new sessions can take roughly ${coldHoursLow}–${coldHoursHigh} hours ` +
    `before Gemini finishes. Already-analyzed sessions are skipped.`;
}

async function loadCreateFacets() {
  const minS = Math.max(
    COHORT_MIN_DURATION_S,
    Math.round(Number(minSInput?.value) || COHORT_MIN_DURATION_S)
  );
  try {
    createFacets = await fetchJson(poolFacetsUrl(minS * 1000));
  } catch {
    createFacets = {
      n: 0,
      outcome: [],
      device: [],
      page: [],
      utm_source: [],
      utm_campaign: [],
    };
  }
  syncCreateFormHints();
}

function setCreateError(message) {
  if (createErrorEl) createErrorEl.textContent = message || '';
}

function setCreateBusy(busy) {
  samplingCohort = !!busy;
  if (createSubmitBtn) {
    createSubmitBtn.disabled = samplingCohort;
    createSubmitBtn.textContent = samplingCohort ? 'Sampling…' : 'Sample cohort';
  }
  if (createCancelBtn) createCancelBtn.disabled = samplingCohort;
  syncRunButtons();
}

function openCreateDialog() {
  if (!createDialog || typeof createDialog.showModal !== 'function') {
    runCohort({
      a: 100,
      b: 0,
      minDurationMs: COHORT_MIN_DURATION_S * 1000,
      order: 'latest',
      startMs: null,
      endMs: null,
      label: 'New cohort',
      sliceA: emptySlice(),
      sliceB: emptySlice(),
    });
    return;
  }
  if (samplingCohort) return;
  if (aNInput) aNInput.value = aNInput.value || '100';
  if (bNInput) bNInput.value = bNInput.value || '100';
  if (addBInput) addBInput.checked = false;
  if (orderInput) orderInput.value = 'latest';
  if (limitRangeInput) limitRangeInput.checked = false;
  if (timeRangeEl) timeRangeEl.hidden = true;
  if (startInput) startInput.value = '';
  if (endInput) endInput.value = '';
  if (nameInput) {
    nameInput.dataset.autofill = '1';
    nameInput.value = '';
  }
  if (minSInput && Number(minSInput.value) < COHORT_MIN_DURATION_S) {
    minSInput.value = String(COHORT_MIN_DURATION_S);
  }
  createSliceA = emptySlice();
  createSliceB = emptySlice();
  setCreateError('');
  setCreateBusy(false);
  syncCreateFormHints();
  createDialog.showModal();
  if (nameInput) {
    nameInput.focus();
    nameInput.select();
  }
  loadCreateFacets();
}

function analyzeCohortUrl(id) {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts/${encodeURIComponent(id)}/analyze${qs ? `?${qs}` : ''}`;
}

function pauseCohortUrl(id) {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts/${encodeURIComponent(id)}/pause${qs ? `?${qs}` : ''}`;
}

function retryUrl(id, sessionId) {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts/${encodeURIComponent(id)}/sessions/${encodeURIComponent(
    sessionId
  )}/retry${qs ? `?${qs}` : ''}`;
}

function cutUrl(id, { run = false, refresh = false } = {}) {
  const params = withProject(new URLSearchParams());
  for (const field of SLICE_FIELDS) {
    if (slice[field]) params.set(field, slice[field]);
  }
  if (run) params.set('run', '1');
  // Busts the cached answer for this cut and asks the model again.
  if (refresh) params.set('refresh', '1');
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

function openSession(sessionId, atMs = 0) {
  if (!sessionId) return;
  setMode('replay');
  // setPendingSeek is consumed once the replay has loaded and knows its
  // duration; a citation past the end is clamped there, not here.
  if (atMs > 0) setPendingSeek(atMs);
  writeHash({ p: currentProject, s: sessionId, t: atMs > 0 ? Math.round(atMs / 1000) : 0 });
  loadSession(sessionId);
}





function viewSettingRows(body) {
  const rows = [];
  const push = (label, value) => {
    if (value == null || value === '') return;
    rows.push({ label, value: String(value) });
  };
  const pushGroup = (title) => rows.push({ group: title });

  push('Name', body.label || body.id || '');

  const aN = Number(body.a_n || 0);
  const bN = Number(body.b_n || 0);
  const minMs = Number(
    body.min_duration_ms != null ? body.min_duration_ms : COHORT_MIN_DURATION_S * 1000
  );
  const minS = Math.max(0, Math.round(minMs / 1000));
  const order = body.order === 'earliest' ? 'Earliest sessions' : 'Latest sessions';
  const sliceA = body.slice && typeof body.slice === 'object' ? body.slice : {};
  const sliceB = body.slice_b && typeof body.slice_b === 'object' ? body.slice_b : {};

  if (aN > 0 || bN > 0) {
    push('Group A', `${aN} session${aN === 1 ? '' : 's'}`);
    if (bN > 0) push('Group B', `${bN} session${bN === 1 ? '' : 's'}`);
  }

  const sample =
    body.sample && typeof body.sample === 'object' ? body.sample : null;
  if (sample) {
    const requested = Number(sample.requested || 0);
    const matched = Number(sample.matched || 0);
    if (requested > 0 && matched < requested) {
      push(
        'Sampled',
        sample.capped
          ? `${matched} of ${requested} (scan hit candidate cap)`
          : `${matched} of ${requested} (pool had fewer matches)`
      );
    }
  }

  push('Min duration', `≥${minS}s`);
  push('Choose from', order);

  if (body.start_ms != null || body.end_ms != null) {
    const from = body.start_ms != null ? formatViewClock(body.start_ms) : '—';
    const to = body.end_ms != null ? formatViewClock(body.end_ms) : '—';
    push('Date range', `${from} → ${to}`);
  }

  const hasAFacets = SLICE_FIELDS.some((f) => sliceA[f]);
  const hasBFacets = bN > 0 && SLICE_FIELDS.some((f) => sliceB[f]);
  const urlFallback = (body.url_contains || '').trim();

  if (aN > 0 || bN > 0) {
    if (hasAFacets || urlFallback) {
      pushGroup(bN > 0 ? 'Group A filters' : 'Filters');
      for (const field of SLICE_FIELDS) {
        const label = FACET_LABELS[field] || field;
        push(
          label.charAt(0).toUpperCase() + label.slice(1),
          formatViewFacet(field, sliceA[field])
        );
      }
      if (!sliceA.page && urlFallback) push('Page', urlFallback);
    }
    if (hasBFacets) {
      pushGroup('Group B filters');
      for (const field of SLICE_FIELDS) {
        const label = FACET_LABELS[field] || field;
        push(
          label.charAt(0).toUpperCase() + label.slice(1),
          formatViewFacet(field, sliceB[field])
        );
      }
    }
  } else {
    const hasFacets = SLICE_FIELDS.some((f) => sliceA[f]) || urlFallback;
    if (hasFacets) {
      pushGroup('Filters');
      for (const field of SLICE_FIELDS) {
        const label = FACET_LABELS[field] || field;
        push(
          label.charAt(0).toUpperCase() + label.slice(1),
          formatViewFacet(field, sliceA[field])
        );
      }
      if (!sliceA.page && urlFallback) push('Page', urlFallback);
    }
  }

  return rows;
}

function renderViewSettings(body) {
  if (!viewSelectionEl) return;
  const rows = viewSettingRows(body || {});
  if (!rows.length) {
    viewSelectionEl.innerHTML =
      '<div class="view-group">No sampling details saved for this cohort.</div>';
    return;
  }
  viewSelectionEl.innerHTML = rows
    .map((row) => {
      if (row.group) {
        return `<div class="view-group">${escapeHtml(row.group)}</div>`;
      }
      return `<div class="view-row"><span class="k">${escapeHtml(
        row.label
      )}</span><span class="v">${escapeHtml(row.value)}</span></div>`;
    })
    .join('');
}

function openViewCohort() {
  if (!cohortId || !cohort || !viewDialog) return;
  if (viewTitleEl) {
    viewTitleEl.textContent = cohort.label || 'Cohort settings';
  }
  renderViewSettings(cohort);
  viewDialog.showModal();
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
      const filter =
        entry.filter === 'b'
          ? { className: 'b', label: 'B' }
          : { className: 'a', label: 'A' };
      const status = entry.status || 'queued';
      const campaign =
        (entry.attribution && entry.attribution.utm_campaign) ||
        (entry.attribution && entry.attribution.utm_source) ||
        '—';
      const err = entry.error
        ? `<div style="color:#b91c1c;font-size:11px;margin-top:2px">${escapeHtml(entry.error)}</div>`
        : '';
      // A stale row lost its write-up, so it needs the same re-run an errored
      // one does — the only difference is that nothing actually failed.
      const retry =
        status === 'error' || status === 'stale'
          ? `<button type="button" class="row-retry" data-retry="${escapeAttr(
              entry.session_id
            )}">${status === 'stale' ? 'Re-analyze' : 'Retry'}</button>`
          : '';
      return `
        <tr class="openable" data-sid="${escapeAttr(entry.session_id)}">
          <td class="sid">${escapeHtml(entry.session_id.slice(0, 8))}…</td>
          <td><span class="badge ${filter.className}">${escapeHtml(filter.label)}</span></td>
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
  page: 'page',
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
  const rows = analyzedSessions(cohort);
  const n = rows.filter((r) => rowMatches(r, slice)).length;
  const hintEl = pickerEl.querySelector('[data-hint-text]');
  const countEl = pickerEl.querySelector('[data-count="n"]');
  // The "leave it on any" nudge only helps before a filter is set.
  if (hintEl) hintEl.textContent = sliceIsEmpty(slice) ? HINT_TEXT : '';
  if (countEl) {
    countEl.textContent = `${n} of ${rows.length} analyzed session(s) selected`;
  }
}


function onSliceChanged() {
  renderPicker();
  stopCutPolling();
  // A thread about one group answers nothing about the next one.
  clearChat();
  loadCut().catch(() => {});
}

/** Rebuild the Analysis picker from this cohort's analyzed rows.

Each option is a value present on those rows (landing page, campaign, …),
stamped from the class columns when the cohort was fetched.
*/
function syncPicker() {
  if (!cohort) {
    facets = null;
    renderPicker();
    return;
  }
  facets = facetsFromRows(analyzedSessions(cohort));
  renderPicker();
}

function renderCutPlaceholder(message) {
  cutIsShowing = false;
  cutBodyEl.innerHTML = `<div class="insights-placeholder">${escapeHtml(message)}</div>`;
  syncCutRunButton();
  // No cut on screen means no group to ask about.
  if (chatEl) chatEl.hidden = true;
}

function scrollToInsight(id) {
  const el = cutBodyEl.querySelector(`#${CSS.escape(id)}`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.remove('cut-insight-flash');
  // Retrigger the flash when the same rec is clicked again.
  void el.offsetWidth;
  el.classList.add('cut-insight-flash');
  const clear = () => el.classList.remove('cut-insight-flash');
  el.addEventListener('animationend', clear, { once: true });
}

function renderCut(body) {
  cutLabelEl.textContent = body.label || 'Analysis';
  cutIsShowing = true;
  cutBodyEl.innerHTML = cutHtml(body);
  syncCutRunButton();
  // The cut and the chat under it describe the same group, so seed the chat's
  // scope from the cut until the first answer's meta frame refines it.
  showChat({
    label: body.label || 'Sessions',
    n: body.n || 0,
    session_ids: body.session_ids || [],
    citations: body.citations || null,
  });
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
  const n = analyzedCount(cohort);
  cutProgressEl.textContent = n ? 'Not analyzed yet' : 'No analyzed sessions';
  renderCutPlaceholder(
    n
      ? 'This group has not been analyzed yet. Click Analyze to have the model read these write-ups.'
      : 'Start analysis on the Sessions tab first, then pick a group.'
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

async function runCut({ refresh = false } = {}) {
  if (!cohortId || !analysisEnabled) return;
  stopCutPolling();
  cutRunBtn.disabled = true;
  cutProgressEl.textContent = refresh ? 'Re-analyzing…' : 'Analyzing…';
  renderCutPlaceholder('Reading the write-ups in this group. This takes a few seconds.');
  try {
    const body = await fetchCut(cohortId, { run: true, refresh });
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

// ---- Chat -------------------------------------------------------------
//
// Scoped to the cut on screen: the same filter, the same group. History lives
// in this tab only — a thread about mobile buyers means nothing once the
// filter moves, so changing the cut clears it.

let chatTurns = [];
let chatMeta = null;
let chatBusy = false;
let chatAbort = null;

function chatUrl(id) {
  const params = withProject(new URLSearchParams());
  for (const field of SLICE_FIELDS) {
    if (slice[field]) params.set(field, slice[field]);
  }
  const qs = params.toString();
  return `/api/cohorts/${encodeURIComponent(id)}/chat${qs ? `?${qs}` : ''}`;
}

/** Answers linkify S<N> against the map the model was actually shown. */
function chatCitations(text) {
  return linkCitations(
    escapeHtml(text || ''),
    (chatMeta && chatMeta.session_ids) || [],
    escapeAttr,
    (chatMeta && chatMeta.citations) || null
  );
}



/**
 * Render one model turn.
 *
 * `settled` is linkified; `pending` is the tail that could still grow into a
 * citation, so it stays plain text until the next chunk (or the end of the
 * stream) resolves it.
 */
function renderChatAnswer(el, text, { done = false } = {}) {
  const [settled, pending] = done ? [text, ''] : splitSettled(text);
  const body = chatInline(chatMarkdown(chatCitations(settled)));
  el.innerHTML = pending ? body + escapeHtml(pending) : body;
  el.classList.toggle('streaming', !done);
}

function appendChatTurn(role, text) {
  const el = document.createElement('div');
  el.className = `chat-turn ${role}`;
  if (role === 'user') el.textContent = text;
  chatLogEl.appendChild(el);
  chatLogEl.scrollTop = chatLogEl.scrollHeight;
  return el;
}

function syncChatControls() {
  const ready = Boolean(cohortId) && analysisEnabled;
  chatInputEl.disabled = !ready || chatBusy;
  chatSendEl.disabled = !ready || chatBusy;
  chatSendEl.textContent = chatBusy ? 'Asking…' : 'Ask';
  chatClearEl.hidden = chatTurns.length === 0;
}

/** Chat is only meaningful once a cut is on screen — same group, same filter. */
function showChat(meta) {
  chatEl.hidden = !cutIsShowing;
  if (meta) {
    chatMeta = meta;
    chatScopeEl.textContent = `${meta.label} · ${meta.n} session${
      meta.n === 1 ? '' : 's'
    }`;
  }
  syncChatControls();
}

function clearChat() {
  if (chatAbort) chatAbort.abort();
  chatAbort = null;
  chatBusy = false;
  chatTurns = [];
  chatMeta = null;
  chatLogEl.innerHTML = '';
  chatScopeEl.textContent = '';
  syncChatControls();
}

async function askChat(question) {
  if (!cohortId || chatBusy) return;
  chatBusy = true;
  chatTurns.push({ role: 'user', content: question });
  appendChatTurn('user', question);
  const answerEl = appendChatTurn('model', '');
  answerEl.classList.add('streaming');
  syncChatControls();

  chatAbort = new AbortController();
  let answer = '';
  try {
    const res = await fetch(chatUrl(cohortId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: chatTurns }),
      signal: chatAbort.signal,
    });
    if (!res.ok) {
      let message = `Server returned ${res.status}.`;
      try {
        const body = await res.json();
        if (body && body.error) message = body.error;
      } catch {
        /* keep the status message */
      }
      throw new Error(message);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let failed = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const [frames, rest] = parseFrames(buffer);
      buffer = rest;
      for (const { event, data } of frames) {
        if (event === 'meta') {
          showChat(data);
        } else if (event === 'delta') {
          answer += data.text || '';
          renderChatAnswer(answerEl, answer);
          chatLogEl.scrollTop = chatLogEl.scrollHeight;
        } else if (event === 'error') {
          failed = data.error || 'Chat failed.';
        }
      }
      if (failed) break;
    }
    if (failed) throw new Error(failed);

    renderChatAnswer(answerEl, answer, { done: true });
    // Only a completed answer joins the history the next turn is built on.
    chatTurns.push({ role: 'assistant', content: answer });
  } catch (err) {
    if (err.name === 'AbortError') {
      answerEl.remove();
      chatTurns.pop();
    } else if (answer) {
      // Keep what arrived, and say the rest did not.
      renderChatAnswer(answerEl, answer, { done: true });
      const note = appendChatTurn('error', '');
      note.textContent = err.message;
      chatTurns.pop();
    } else {
      answerEl.className = 'chat-turn error';
      answerEl.textContent = err.message;
      chatTurns.pop();
    }
  } finally {
    chatBusy = false;
    chatAbort = null;
    syncChatControls();
    chatLogEl.scrollTop = chatLogEl.scrollHeight;
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
          : c.status === 'idle'
            ? ' — not started'
            : c.status === 'paused'
              ? ' — paused'
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
  syncPicker();

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
  cutLabelEl.textContent = 'Analysis';
  progressEl.textContent = 'No cohort yet';
  cutProgressEl.textContent = 'No cohort yet';
  syncRunButtons();
  renderCutPlaceholder(
    'Analyze a cohort, then pick a group to see what those sessions have in common.'
  );
  renderEmpty(
    'Create a new cohort to sample sessions, then start analysis. Prior cohorts stay in the dropdown. Open any row to watch it in Replay.'
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

async function runCohort(opts) {
  if (samplingCohort) return;
  const sample = opts || readCreateForm();
  if (!sample.label) {
    setCreateError('Name this cohort before sampling.');
    return;
  }
  autoSwitchedForRun = false;
  setCreateError('');
  setCreateBusy(true);
  progressEl.textContent = 'Sampling a new cohort (existing cohorts kept)…';
  try {
    const body = await fetchJson(createCohortUrl(sample), { method: 'POST' });
    if (createDialog && createDialog.open) createDialog.close();
    cohortId = body.id;
    renderCohort(body);
    setTab('queue');
    await refreshCohorts();
  } catch (err) {
    const message = err.message || 'Could not sample this cohort.';
    setCreateError(message);
    progressEl.textContent = message;
    // Keep the dialog open so the filters/error stay visible.
    if (createDialog && !createDialog.open && typeof createDialog.showModal === 'function') {
      createDialog.showModal();
    }
  } finally {
    setCreateBusy(false);
  }
}

async function startCohortAnalysis() {
  if (!cohortId || !analyzeBtn || analyzeBtn.disabled) return;
  autoSwitchedForRun = false;
  analyzeBtn.disabled = true;
  analyzeBtn.textContent = 'Starting…';
  try {
    const body = await fetchJson(analyzeCohortUrl(cohortId), { method: 'POST' });
    renderCohort(body);
    await refreshCohorts();
  } catch (err) {
    progressEl.textContent = err.message;
    syncRunButtons();
  }
}

async function pauseCohortAnalysis() {
  if (!cohortId || !pauseBtn || pauseBtn.disabled) return;
  pauseBtn.disabled = true;
  pauseBtn.textContent = 'Pausing…';
  try {
    const body = await fetchJson(pauseCohortUrl(cohortId), { method: 'POST' });
    renderCohort(body);
    await refreshCohorts();
  } catch (err) {
    progressEl.textContent = err.message;
  } finally {
    if (pauseBtn) pauseBtn.textContent = 'Pause';
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

async function deleteCohort() {
  if (!cohortId || !cohort || !deleteBtn) return;
  const name = cohort.label || cohortId;
  if (!window.confirm(`Delete cohort “${name}”? Session write-ups are kept.`)) {
    return;
  }
  deleteBtn.disabled = true;
  try {
    await fetchJson(cohortUrl(cohortId), { method: 'DELETE' });
    cohortId = null;
    cohort = null;
    await refreshCohorts();
  } catch (err) {
    progressEl.textContent = err.message;
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
  runBtn.addEventListener('click', openCreateDialog);
  if (createCancelBtn && createDialog) {
    createCancelBtn.addEventListener('click', () => createDialog.close());
  }
  if (createForm) {
    createForm.addEventListener('submit', (event) => {
      event.preventDefault();
      if (samplingCohort) return;
      if (minSInput && Number(minSInput.value) < COHORT_MIN_DURATION_S) {
        minSInput.value = String(COHORT_MIN_DURATION_S);
      }
      const opts = readCreateForm();
      if (!opts.label) {
        setCreateError('Name this cohort before sampling.');
        if (nameInput) {
          nameInput.focus();
          nameInput.reportValidity?.();
        }
        return;
      }
      if (aNInput) aNInput.value = String(opts.a);
      if (bNInput && opts.addB) bNInput.value = String(opts.b);
      runCohort(opts);
    });
  }
  if (nameInput) {
    nameInput.addEventListener('input', () => {
      nameInput.dataset.autofill = '0';
    });
  }
  [aNInput, bNInput, startInput, endInput, orderInput].forEach((el) => {
    if (!el) return;
    el.addEventListener('input', syncCreateFormHints);
    el.addEventListener('change', syncCreateFormHints);
  });
  if (addBInput) {
    addBInput.addEventListener('change', () => {
      syncCreateFormHints();
    });
  }
  if (limitRangeInput) {
    limitRangeInput.addEventListener('change', () => {
      if (!limitRangeInput.checked) {
        if (startInput) startInput.value = '';
        if (endInput) endInput.value = '';
      }
      syncCreateFormHints();
    });
  }
  if (minSInput) {
    minSInput.addEventListener('input', syncCreateFormHints);
    minSInput.addEventListener('change', () => {
      syncCreateFormHints();
      if (createDialog && createDialog.open) loadCreateFacets();
    });
  }
  if (analyzeBtn) analyzeBtn.addEventListener('click', startCohortAnalysis);
  if (pauseBtn) pauseBtn.addEventListener('click', pauseCohortAnalysis);
  renameBtn.addEventListener('click', renameCohort);
  if (viewBtn) viewBtn.addEventListener('click', openViewCohort);
  if (viewCloseBtn && viewDialog) {
    viewCloseBtn.addEventListener('click', () => viewDialog.close());
  }
  if (deleteBtn) deleteBtn.addEventListener('click', deleteCohort);
  if (cutRunBtn) {
    cutRunBtn.addEventListener('click', () => runCut({ refresh: cutIsShowing }));
  }
  cutBodyEl.addEventListener('click', (e) => {
    const cite = e.target.closest('.cut-cite');
    if (cite && cutBodyEl.contains(cite)) {
      openSession(cite.dataset.citeSid, Number(cite.dataset.citeT) || 0);
      return;
    }
    const btn = e.target.closest('.cut-rec-link');
    if (!btn || !cutBodyEl.contains(btn)) return;
    const id = btn.dataset.scrollTo;
    if (id) scrollToInsight(id);
  });
  chatFormEl.addEventListener('submit', (e) => {
    e.preventDefault();
    const question = chatInputEl.value.trim();
    if (!question || chatBusy) return;
    chatInputEl.value = '';
    askChat(question).catch(() => {});
  });
  chatClearEl.addEventListener('click', () => {
    clearChat();
    // Cleared by hand, so the cut is still on screen — keep the scope header.
    if (cutIsShowing) showChat(null);
  });
  chatLogEl.addEventListener('click', (e) => {
    const cite = e.target.closest('.cut-cite');
    if (cite && chatLogEl.contains(cite)) {
      openSession(cite.dataset.citeSid, Number(cite.dataset.citeT) || 0);
    }
  });
  cohortSelectEl.addEventListener('change', () => {
    cohortId = cohortSelectEl.value || null;
    stopCutPolling();
    clearChat();
    // A different cohort has different facet values, so reset the picker.
    slice = emptySlice();
    refreshCohort()
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
