import { record } from 'rrweb';
import { config } from './config.js';
import { readSession, writeSession } from './identity.js';
import { status, setStatus } from './state.js';
import { flush } from './transport.js';

let urlTriggers = [];

export function gatesSatisfied() {
  const current = readSession();
  const triggerOk = !config.triggers || Boolean(current && current.triggered);
  const durationOk =
    !config.minDurationMs ||
    (current && Date.now() - current.started >= config.minDurationMs);
  return triggerOk && durationOk;
}

export function tryTransition(reason) {
  if (status !== 'buffering' || !gatesSatisfied()) return;
  setStatus('active');
  const current = readSession();
  record.addCustomEvent('capture-trigger', {
    reason: (current && current.triggerReason) || reason,
  });
  flush(false);
}

function activate(reason) {
  if (status !== 'buffering') return;
  const current = readSession();
  if (current && !current.triggered) {
    current.triggered = true;
    current.triggerReason = reason;
    writeSession(current);
  }
  tryTransition(reason);
}

export function checkUrlTriggers() {
  if (status !== 'buffering' || urlTriggers.length === 0) return;
  if (urlTriggers.some((re) => re.test(location.href))) activate('url');
}

export function setupTriggers() {
  urlTriggers = ((config.triggers && config.triggers.urls) || []).map(
    (pattern) => new RegExp(pattern)
  );

  if (urlTriggers.length) {
    for (const method of ['pushState', 'replaceState']) {
      const original = history[method].bind(history);
      history[method] = (...args) => {
        const result = original(...args);
        checkUrlTriggers();
        return result;
      };
    }
    window.addEventListener('popstate', checkUrlTriggers);
    window.addEventListener('hashchange', checkUrlTriggers);
  }

  if (config.triggers && config.triggers.onException) {
    window.addEventListener('error', () => activate('exception'));
    window.addEventListener('unhandledrejection', () => activate('exception'));
  }

  window.capture = Object.assign(window.capture || {}, {
    trigger: (reason) => activate(reason || 'manual'),
    status: () => status,
    config: () => ({ ...config }),
  });
}
