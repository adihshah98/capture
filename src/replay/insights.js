import { currentProject, fetchJson, setProject } from './api.js';
import { parseFrames, splitSettled } from './chat-stream.js';
import { linkCitations } from './citations.js';
import { writeHash } from './deeplink.js';
import { loadSession, setPendingSeek } from './session-player.js';

const POLL_MS = 2000;

const modeButtons = document.querySelectorAll('#mode-switch button');
const tabButtons = document.querySelectorAll('.insights-tabs button[data-insights-tab]');
const runBtn = document.getElementById('cohort-run');
const analyzeBtn = document.getElementById('cohort-analyze');
const pauseBtn = document.getElementById('cohort-pause');
const cohortSelectEl = document.getElementById('cohort-select');
const renameBtn = document.getElementById('cohort-rename');
const selectionEl = document.getElementById('cohort-selection');
const labelEl = document.getElementById('cohort-label');
const progressEl = document.getElementById('cohort-progress');
const cutLabelEl = document.getElementById('cut-label');
const cutProgressEl = document.getElementById('cut-progress');
const cutRunBtn = document.getElementById('cut-run');
const cutRefreshBtn = document.getElementById('cut-refresh');
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
  const pending =
    (prog.queued || 0) + (prog.running || 0) + (prog.error || 0) > 0 ||
    cohort.status === 'idle' ||
    cohort.status === 'paused';
  return pending;
}

function syncRunButtons() {
  // Creating a cohort always adds a new one — never replaces the selected cohort.
  runBtn.disabled = false;
  runBtn.title =
    'Sample latest 50 purchased + 50 non-purchase ashwamag-01 sessions (≥5s) into a new cohort. Does not start analysis. Existing cohorts stay in the list.';
  runBtn.textContent = 'New cohort';
  renameBtn.disabled = !cohortId;

  if (analyzeBtn) {
    const can = cohortCanAnalyze();
    analyzeBtn.disabled = !can;
    analyzeBtn.hidden = false;
    if (!analysisEnabled) {
      analyzeBtn.title = 'Analysis is not configured on this server';
      analyzeBtn.disabled = true;
    } else if (!cohortId) {
      analyzeBtn.title = 'Create or select a cohort first';
    } else if (cohortIsRunning()) {
      analyzeBtn.title = 'Analysis is already running';
    } else if (cohort && cohort.status === 'done') {
      analyzeBtn.title = 'This cohort is fully analyzed';
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
    pauseBtn.title = running
      ? 'Stop after the current step; finished write-ups are kept'
      : '';
  }

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
  if (!cutRefreshBtn) return;
  // Offered only over a result: with nothing cached, "Analyze this group"
  // already does the same work under a clearer name.
  cutRefreshBtn.hidden = !cutIsShowing;
  cutRefreshBtn.disabled = !ready;
  cutRefreshBtn.title = ready
    ? 'Discard this cached answer and ask the model again'
    : cutRunBtn.title;
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
  params.set('purchased', '50');
  params.set('all', '50');
  params.set('min_duration_ms', '5000');
  params.set('url_contains', '/pages/ashwamag-01');
  return `/api/cohorts?${params.toString()}`;
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

function facetsUrl(id) {
  const qs = withProject(new URLSearchParams()).toString();
  return `/api/cohorts/${encodeURIComponent(id)}/facets${qs ? `?${qs}` : ''}`;
}

/** One group's filter. Every field is a single value; '' means "any". */
const SLICE_FIELDS = ['outcome', 'device', 'page', 'utm_source', 'utm_campaign'];

function emptySlice() {
  return { outcome: '', device: '', page: '', utm_source: '', utm_campaign: '' };
}

function sliceIsEmpty(spec) {
  return SLICE_FIELDS.every((field) => !spec[field]);
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
  const batch = body.batch ? ' · Gemini batch queued' : '';
  const phase =
    body.status === 'idle'
      ? 'Sampled — not analyzed yet · '
      : body.status === 'paused'
        ? 'Paused · '
        : '';
  return (
    phase +
    `${prog.done || 0} / ${prog.total || 0} done · ${prog.running || 0} running · ` +
    `${prog.queued || 0} queued` +
    (prog.error ? ` · ${prog.error} failed` : '') +
    batch
  );
}

function renderSelection(body) {
  if (!selectionEl) return;
  selectionEl.textContent = (body && body.selection) || '';
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
  if (spec.page && (row.page || '') !== spec.page) return false;
  const attrs = row.attribution || {};
  const norm = (v) => String(v || '').trim().toLowerCase();
  if (spec.utm_source && norm(attrs.utm_source) !== spec.utm_source) return false;
  if (spec.utm_campaign && norm(attrs.utm_campaign) !== spec.utm_campaign) return false;
  return true;
}

function onSliceChanged() {
  renderPicker();
  stopCutPolling();
  // A thread about one group answers nothing about the next one.
  clearChat();
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
  cutIsShowing = false;
  cutBodyEl.innerHTML = `<div class="insights-placeholder">${escapeHtml(message)}</div>`;
  syncCutRunButton();
  // No cut on screen means no group to ask about.
  if (chatEl) chatEl.hidden = true;
}

/** Section order mirrors the prompt: journey, attention, intent, blockers. */
const CUT_SECTIONS = [
  { key: 'journey', title: 'How they moved through the site', empty: 'The write-ups do not describe a common route.' },
  { key: 'attention', title: 'What held their attention', empty: 'Nothing held attention consistently across these sessions.' },
  { key: 'intent', title: 'Signals of intent', empty: 'No consistent buying signals stood out.' },
  { key: 'blockers', title: 'What stopped them', cls: 'friction', empty: 'No blockers were reported in these write-ups.' },
  { key: 'caveats', title: 'Worth checking', cls: 'caveats' },
];

/** Escaped observation text with each "S4 [01:09]" turned into a seek link. */
function withCitations(text, sessionIds, citations) {
  return linkCitations(escapeHtml(text || ''), sessionIds, escapeAttr, citations);
}

/** An insight item: observation, its share of the group, and what to do. */
function insightItem(item, id, sessionIds, citations) {
  if (typeof item === 'string') {
    return `<li>${withCitations(item, sessionIds, citations)}</li>`;
  }
  const share = item.share
    ? `<span class="cut-share">${escapeHtml(item.share)}</span>`
    : '';
  const action = item.action
    ? `<p class="cut-action">${escapeHtml(item.action)}</p>`
    : '';
  const idAttr = id ? ` id="${escapeAttr(id)}"` : '';
  const observation = withCitations(item.observation, sessionIds, citations);
  return `<li${idAttr} class="cut-insight">${share}${observation}${action}</li>`;
}

/** Map normalized action text → insight element id for recommendation jump links. */
function actionTargets(analysis) {
  const targets = new Map();
  for (const { key } of CUT_SECTIONS) {
    if (key === 'caveats') continue;
    const items = analysis[key] || [];
    items.forEach((item, i) => {
      if (!item || typeof item !== 'object') return;
      const action = typeof item.action === 'string' ? item.action.trim().toLowerCase() : '';
      if (!action || targets.has(action)) return;
      targets.set(action, `cut-insight-${key}-${i}`);
    });
  }
  return targets;
}

/** Prefer an exact action match; otherwise the longest action that overlaps the line. */
function targetForRecommendation(line, targets) {
  const key = String(line || '').trim().toLowerCase();
  if (!key) return null;
  if (targets.has(key)) return targets.get(key);
  let best = null;
  let bestLen = 0;
  for (const [action, id] of targets) {
    if (action.length <= bestLen) continue;
    if (key.includes(action) || action.includes(key)) {
      best = id;
      bestLen = action.length;
    }
  }
  return best;
}

function recommendationItem(line, targetId) {
  const text = escapeHtml(line);
  if (!targetId) return `<li>${text}</li>`;
  return `<li>
    <button type="button" class="cut-rec-link" data-scroll-to="${escapeAttr(targetId)}">${text}</button>
  </li>`;
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
  const analysis = body.analysis || {};
  const n = body.n || 0;
  cutLabelEl.textContent = body.label || 'Sessions';

  // Small groups are the norm here; say so rather than letting counts imply rigour.
  const thin = n > 0 && n < 3 ? '<span class="cut-thin">thin evidence</span>' : '';

  const headline = analysis.headline
    ? `<div class="cut-headline">${escapeHtml(analysis.headline)}</div>`
    : '';

  const targets = actionTargets(analysis);
  // The stored map is what the model was shown; session_ids is the positional
  // fallback for cuts written before the map was recorded.
  const sessionIds = body.session_ids || [];
  const citations = body.citations || null;

  const sections = CUT_SECTIONS.map(({ key, title, cls, empty }) => {
    const items = analysis[key] || [];
    // Caveats have no empty copy: an absent caveat list is not worth a block.
    if (!items.length && !empty) return '';
    const count = key === 'journey' ? `<span class="n">(n=${n})</span>${thin}` : '';
    return `<div class="cut-block ${cls || ''}" id="cut-section-${escapeAttr(key)}">
      <h4>${escapeHtml(title)}${count}</h4>
      ${
        items.length
          ? `<ul>${items
              .map((item, i) =>
                insightItem(
                  item,
                  key === 'caveats' ? null : `cut-insight-${key}-${i}`,
                  sessionIds,
                  citations
                )
              )
              .join('')}</ul>`
          : `<p class="cut-empty">${escapeHtml(empty)}</p>`
      }
    </div>`;
  }).join('');

  // Punch list after the evidence so recommendations close the cut. Absent on
  // thin/empty cuts — no empty block.
  const recs = analysis.recommendations || [];
  const recommendations = recs.length
    ? `<div class="cut-block recommendations">
        <h4>Recommendations</h4>
        <ul>${recs
          .map((line) => recommendationItem(line, targetForRecommendation(line, targets)))
          .join('')}</ul>
      </div>`
    : '';

  const meta = `<p class="cut-meta">
    Read from ${n} analyzed session write-up(s) in this cohort.
    Written by ${escapeHtml(body.model || 'the model')}.
    Cohort membership is fixed, so this cut is cached and served without another
    model call — use Re-analyze to ask again.
  </p>`;

  cutIsShowing = true;
  cutBodyEl.innerHTML = headline + sections + recommendations + meta;
  syncCutRunButton();
  // The cut and the chat under it describe the same group, so seed the chat's
  // scope from the cut until the first answer's meta frame refines it.
  showChat({
    label: body.label || 'Sessions',
    n,
    session_ids: sessionIds,
    citations,
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

async function runCut({ refresh = false } = {}) {
  if (!cohortId || !analysisEnabled) return;
  stopCutPolling();
  cutRunBtn.disabled = true;
  if (cutRefreshBtn) cutRefreshBtn.disabled = true;
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

/** Minimal markdown: paragraphs, `-` bullets, inline code. */
function chatMarkdown(html) {
  const blocks = html.split(/\n{2,}/);
  return blocks
    .map((block) => {
      const lines = block.split('\n').filter((l) => l.trim());
      if (!lines.length) return '';
      if (lines.every((l) => /^\s*[-*]\s+/.test(l))) {
        const items = lines
          .map((l) => `<li>${l.replace(/^\s*[-*]\s+/, '')}</li>`)
          .join('');
        return `<ul>${items}</ul>`;
      }
      return `<p>${lines.join('<br>')}</p>`;
    })
    .join('');
}

function chatInline(html) {
  return html.replace(/`([^`]+)`/g, '<code>$1</code>');
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
  renderSelection(body);
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
  renderSelection(null);
  syncRunButtons();
  renderCutPlaceholder(
    'Analyze a cohort, then pick a group to see what those sessions have in common.'
  );
  renderEmpty(
    'Create a new cohort to sample sessions, review the selection, then start analysis. Prior cohorts stay in the dropdown. Open any row to watch it in Replay.'
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
  if (runBtn.disabled) return;
  autoSwitchedForRun = false;
  runBtn.disabled = true;
  runBtn.textContent = 'Sampling…';
  progressEl.textContent = 'Sampling a new cohort (existing cohorts kept)…';
  try {
    const body = await fetchJson(createCohortUrl(), { method: 'POST' });
    cohortId = body.id;
    renderCohort(body);
    setTab('queue');
    await refreshCohorts();
  } catch (err) {
    progressEl.textContent = err.message;
    syncRunButtons();
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
    if (pauseBtn) pauseBtn.textContent = 'Pause analysis';
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
  if (analyzeBtn) analyzeBtn.addEventListener('click', startCohortAnalysis);
  if (pauseBtn) pauseBtn.addEventListener('click', pauseCohortAnalysis);
  renameBtn.addEventListener('click', renameCohort);
  if (cutRunBtn) cutRunBtn.addEventListener('click', () => runCut());
  if (cutRefreshBtn) {
    cutRefreshBtn.addEventListener('click', () => runCut({ refresh: true }));
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
