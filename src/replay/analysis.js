import { fetchJson, currentProject } from './api.js';
import { loadedSessionId, player, setPendingSeek } from './session-player.js';

const panelEl = document.getElementById('analysis-panel');
const buttonEl = document.getElementById('analyze-btn');
const bodyEl = document.getElementById('analysis-body');

const POLL_MS = 2000;

let enabled = false;
let running = false;
let pollTimer = null;
let currentResult = null;

export function setAnalysisEnabled(on) {
  enabled = !!on;
  panelEl.hidden = !enabled;
}

const escapeHtml = (s) =>
  String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

const escapeAttr = (s) => escapeHtml(s).replace(/'/g, '&#39;');

/** Minimal renderer for the headings, bullets and bold the prompt asks for. */
export function renderMarkdown(md) {
  const html = [];
  let inList = false;
  for (const raw of (md || '').split('\n')) {
    const line = raw.trim();
    if (!line) {
      if (inList) {
        html.push('</ul>');
        inList = false;
      }
      continue;
    }
    const inline = (t) =>
      escapeHtml(t).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    const bullet = line.match(/^[-*]\s+(.*)$/);
    if (heading) {
      if (inList) {
        html.push('</ul>');
        inList = false;
      }
      html.push(`<h4>${inline(heading[2])}</h4>`);
    } else if (bullet) {
      if (!inList) {
        html.push('<ul>');
        inList = true;
      }
      html.push(`<li>${inline(bullet[1])}</li>`);
    } else {
      if (inList) {
        html.push('</ul>');
        inList = false;
      }
      html.push(`<p>${inline(line)}</p>`);
    }
  }
  if (inList) html.push('</ul>');
  return html.join('');
}

function analysisUrl(sessionId, { force = false } = {}) {
  const params = new URLSearchParams();
  if (currentProject) params.set('project', currentProject);
  if (force) params.set('force', '1');
  const qs = params.toString();
  return `/api/sessions/${sessionId}/analysis${qs ? `?${qs}` : ''}`;
}

function qaUrl(sessionId) {
  const params = new URLSearchParams();
  if (currentProject) params.set('project', currentProject);
  const qs = params.toString();
  return `/api/sessions/${sessionId}/qa${qs ? `?${qs}` : ''}`;
}

function clock(ms) {
  const total = Math.max(0, Math.floor((ms || 0) / 1000));
  const m = String(Math.floor(total / 60)).padStart(2, '0');
  const s = String(total % 60).padStart(2, '0');
  return `${m}:${s}`;
}

function renderSegmentBadges(result) {
  const badges = [];
  const order = result.order || {};
  if (order.purchased_at) badges.push('<span class="badge purchased">Purchased</span>');
  else if (order.checkout_started_at) badges.push('<span class="badge started">Reached checkout</span>');
  const attr = result.attribution || {};
  if (attr.utm_source) {
    badges.push(`<span class="attr-badge">utm_source=${escapeHtml(attr.utm_source)}</span>`);
  }
  if (attr.utm_campaign) {
    badges.push(`<span class="attr-badge">campaign=${escapeHtml(attr.utm_campaign)}</span>`);
  }
  if (attr.fbclid) badges.push('<span class="attr-badge">Meta</span>');
  const qa = result.qa;
  if (qa && qa.status) {
    badges.push(`<span class="badge qa-${escapeAttr(qa.status)}">${escapeHtml(qa.status)}</span>`);
  }
  if (!badges.length) return '';
  return `<div class="seg-badges">${badges.join('')}</div>`;
}

function renderThemeChips(themes) {
  if (!themes || !themes.length) return '';
  return (
    '<div class="theme-chips">' +
    themes
      .map((t) => `<span class="theme-chip">${escapeHtml(String(t))}</span>`)
      .join('') +
    '</div>'
  );
}

function renderFriction(insights) {
  const friction = (insights && insights.friction) || [];
  if (!friction.length) return '';
  const items = friction
    .map((f) => {
      const label = escapeHtml(f.label || 'friction');
      if (typeof f.atMs === 'number') {
        return `<li><button type="button" class="friction-link" data-ms="${f.atMs}">[${clock(
          f.atMs
        )}] ${label}</button></li>`;
      }
      return `<li>${label}</li>`;
    })
    .join('');
  return `<h4>Friction</h4><ul>${items}</ul>`;
}

function renderQaControls(result) {
  const qa = result.qa || { status: 'pending', themes: (result.insights && result.insights.themes) || [] };
  return `<div class="qa-actions" id="analysis-qa-actions">
    <button type="button" class="primary" data-qa="accepted">Accept</button>
    <button type="button" class="danger" data-qa="rejected">Reject</button>
    <span class="note" style="border:0;padding:0;margin:0">QA: ${escapeHtml(qa.status || 'pending')}</span>
  </div>`;
}

function renderResult(result) {
  currentResult = result;
  const insights = result.insights || {};
  const themes = insights.themes || result.themes || [];
  const parts = [
    renderSegmentBadges(result),
    renderThemeChips(themes),
    renderFriction(insights),
    renderMarkdown(result.text || ''),
  ];

  for (const warning of result.warnings || []) {
    parts.push(`<p class="warn">${escapeHtml(warning)}</p>`);
  }
  parts.push(renderQaControls(result));
  parts.push(
    '<p class="note">' +
      (result.frame_count
        ? `Read from ${result.frame_count} frames sampled about once per second ` +
          'through the active parts of the session. '
        : '') +
      'Frames show what was displayed, not where the visitor looked. ' +
      `Written by ${escapeHtml(result.model || 'the model')}.</p>`
  );
  bodyEl.innerHTML = parts.join('');

  bodyEl.querySelectorAll('.friction-link').forEach((btn) => {
    btn.addEventListener('click', () => {
      const ms = Number(btn.dataset.ms);
      if (!Number.isFinite(ms)) return;
      if (player) player.seekTo(ms);
      else setPendingSeek(ms);
    });
  });
  bodyEl.querySelectorAll('#analysis-qa-actions [data-qa]').forEach((btn) => {
    btn.addEventListener('click', () => submitQa(btn.dataset.qa));
  });
}

async function submitQa(status) {
  const sessionId = loadedSessionId;
  if (!sessionId || !currentResult) return;
  const themes =
    (currentResult.qa && currentResult.qa.themes) ||
    (currentResult.insights && currentResult.insights.themes) ||
    [];
  try {
    const qa = await fetchJson(qaUrl(sessionId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, themes }),
    });
    currentResult = { ...currentResult, qa };
    if (currentResult.insights) {
      currentResult.insights = { ...currentResult.insights, themes: qa.themes || themes };
    }
    renderResult(currentResult);
  } catch (err) {
    renderError(err.message);
  }
}

function renderProgress() {
  bodyEl.innerHTML =
    '<p class="empty">Replaying the session and sampling frames. This can take a few minutes — you can leave this page and come back.</p>';
}

function renderError(message) {
  bodyEl.innerHTML = `<p class="warn">${escapeHtml(message)}</p>`;
}

function setButton(label, disabled) {
  buttonEl.textContent = label;
  buttonEl.disabled = disabled;
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function sleep(ms) {
  return new Promise((resolve) => {
    pollTimer = setTimeout(resolve, ms);
  });
}

async function pollUntilSettled(sessionId) {
  while (sessionId === loadedSessionId && running) {
    await sleep(POLL_MS);
    if (sessionId !== loadedSessionId || !running) return null;
    let body;
    try {
      body = await fetchJson(analysisUrl(sessionId));
    } catch (err) {
      if (/not analyzed yet/i.test(err.message || '')) continue;
      throw err;
    }
    if (body.status === 'running') continue;
    if (body.status === 'error') {
      throw new Error(body.error || 'Analysis failed.');
    }
    return body;
  }
  return null;
}

export function resetAnalysis(sessionId = loadedSessionId) {
  if (!enabled) return;
  stopPolling();
  running = false;
  currentResult = null;
  bodyEl.innerHTML =
    '<p class="empty">Analyze this session to see where attention went.</p>';
  setButton('Analyze with AI', !sessionId);
}

export async function showCachedAnalysis(sessionId) {
  if (!enabled) return;
  resetAnalysis(sessionId);
  try {
    const body = await fetchJson(analysisUrl(sessionId));
    if (sessionId !== loadedSessionId) return;
    if (body.status === 'running') {
      running = true;
      setButton('Analyzing…', true);
      renderProgress();
      try {
        const result = await pollUntilSettled(sessionId);
        if (sessionId !== loadedSessionId || !result) return;
        renderResult(result);
        setButton('Re-analyze', false);
      } catch (err) {
        if (sessionId !== loadedSessionId) return;
        renderError(err.message);
        setButton('Try again', false);
      } finally {
        if (sessionId === loadedSessionId) running = false;
        stopPolling();
      }
      return;
    }
    if (body.status === 'error') {
      renderError(body.error || 'Analysis failed.');
      setButton('Try again', false);
      return;
    }
    renderResult(body);
    setButton('Re-analyze', false);
  } catch {
    // No cached analysis is the normal case; leave the empty prompt in place.
  }
}

async function runAnalysis() {
  const sessionId = loadedSessionId;
  if (!sessionId || running) return;
  running = true;
  stopPolling();
  setButton('Analyzing…', true);
  renderProgress();

  try {
    const started = await fetchJson(analysisUrl(sessionId, { force: true }), {
      method: 'POST',
    });
    if (sessionId !== loadedSessionId) return;
    if (started.status === 'done' || started.text) {
      renderResult(started);
      setButton('Re-analyze', false);
      return;
    }
    const result = await pollUntilSettled(sessionId);
    if (sessionId !== loadedSessionId || !result) return;
    renderResult(result);
    setButton('Re-analyze', false);
  } catch (err) {
    if (sessionId !== loadedSessionId) return;
    renderError(err.message);
    setButton('Try again', false);
  } finally {
    if (sessionId === loadedSessionId) running = false;
    stopPolling();
  }
}

buttonEl.addEventListener('click', runAnalysis);
