import { fetchJson, projectQuery } from './api.js';
import { loadedSessionId } from './session-player.js';

const panelEl = document.getElementById('analysis-panel');
const buttonEl = document.getElementById('analyze-btn');
const bodyEl = document.getElementById('analysis-body');

let enabled = false;
let running = false;

export function setAnalysisEnabled(on) {
  enabled = !!on;
  panelEl.hidden = !enabled;
}

const escapeHtml = (s) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/** Minimal renderer for the headings, bullets and bold the prompt asks for. */
function renderMarkdown(md) {
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

function renderResult(result) {
  const parts = [renderMarkdown(result.text || '')];

  if (result.sections && result.sections.length) {
    const rows = result.sections
      .map(
        (s) =>
          `<tr><td class="k">${escapeHtml(s.label || '')}</td><td>${s.seconds}s</td></tr>`
      )
      .join('');
    parts.push(
      `<h4>Measured time on screen</h4><table class="dwell">${rows}</table>`
    );
  }
  for (const warning of result.warnings || []) {
    parts.push(`<p class="warn">${escapeHtml(warning)}</p>`);
  }
  parts.push(
    '<p class="note">Time on screen is measured from scroll position with idle ' +
      'gaps removed. It shows what was displayed, not where the visitor looked. ' +
      `Written by ${escapeHtml(result.model || 'the model')}.</p>`
  );
  bodyEl.innerHTML = parts.join('');
}

function setButton(label, disabled) {
  buttonEl.textContent = label;
  buttonEl.disabled = disabled;
}

export function resetAnalysis() {
  if (!enabled) return;
  bodyEl.innerHTML =
    '<p class="empty">Analyze this session to see where attention went.</p>';
  setButton('Analyze with AI', !loadedSessionId);
}

export async function showCachedAnalysis(sessionId) {
  if (!enabled) return;
  resetAnalysis();
  try {
    const cached = await fetchJson(
      `/api/sessions/${sessionId}/analysis${projectQuery()}`
    );
    if (sessionId !== loadedSessionId) return;
    renderResult(cached);
    setButton('Re-analyze', false);
  } catch {
    // No cached analysis is the normal case; leave the empty prompt in place.
  }
}

async function runAnalysis() {
  const sessionId = loadedSessionId;
  if (!sessionId || running) return;
  running = true;
  setButton('Analyzing…', true);
  bodyEl.innerHTML =
    '<p class="empty">Rebuilding the page and measuring attention. This takes a few moments.</p>';

  const query = projectQuery();
  const url = `/api/sessions/${sessionId}/analysis${query ? `${query}&force=1` : '?force=1'}`;
  try {
    const result = await fetchJson(url, { method: 'POST' });
    if (sessionId !== loadedSessionId) return;
    renderResult(result);
    setButton('Re-analyze', false);
  } catch (err) {
    if (sessionId !== loadedSessionId) return;
    bodyEl.innerHTML = `<p class="warn">${escapeHtml(err.message)}</p>`;
    setButton('Try again', false);
  } finally {
    running = false;
  }
}

buttonEl.addEventListener('click', runAnalysis);
