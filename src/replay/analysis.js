import { fetchJson, currentProject } from './api.js';
import { loadedSessionId } from './session-player.js';

const panelEl = document.getElementById('analysis-panel');
const buttonEl = document.getElementById('analyze-btn');
const bodyEl = document.getElementById('analysis-body');

const POLL_MS = 2000;

let enabled = false;
let running = false;
let pollTimer = null;

export function setAnalysisEnabled(on) {
  enabled = !!on;
  panelEl.hidden = !enabled;
}

const escapeHtml = (s) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** Minimal renderer for the headings, bullets and bold the prompt asks for. */
export function renderMarkdown(md) {
  const html = [];
  let inList = false;
  for (const raw of md.split('\n')) {
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

function renderResult(result) {
  const themes = (result.insights && result.insights.themes) || result.themes || [];
  const parts = [renderThemeChips(themes), renderMarkdown(result.text || '')];

  for (const warning of result.warnings || []) {
    parts.push(`<p class="warn">${escapeHtml(warning)}</p>`);
  }
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

function analysisUrl(sessionId, { force = false } = {}) {
  const params = new URLSearchParams();
  if (currentProject) params.set('project', currentProject);
  if (force) params.set('force', '1');
  const qs = params.toString();
  return `/api/sessions/${sessionId}/analysis${qs ? `?${qs}` : ''}`;
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
      // 404 mid-run is transient right after a force clear; keep waiting.
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
  bodyEl.innerHTML =
    '<p class="empty">Analyze this session to see where attention went.</p>';
  setButton('Analyze with AI', !sessionId);
}

export async function showCachedAnalysis(sessionId) {
  if (!enabled) return;
  // Pass the id explicitly: loadSession assigns loadedSessionId before calling
  // us, but resetAnalysis also runs from clearPlayer, where it must read null.
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
