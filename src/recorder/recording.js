import { record } from 'rrweb';
import { startClickCapture } from './clicks.js';
import { startViewportCapture } from './viewport.js';
import { startVisibilityCapture } from './visibility.js';
import { config } from './config.js';
import { getSession } from './identity.js';
import { status, setStatus, setSessionId } from './state.js';
import { pushEvent, flush, resetBuffer, scheduleFlush, stopFlushTimer } from './transport.js';
import { gatesSatisfied, checkUrlTriggers } from './triggers.js';

export let stopFn = null;

let stopClicks = null;
let stopViewport = null;
let stopVisibility = null;

function stopAuxCapture() {
  if (stopClicks) {
    stopClicks();
    stopClicks = null;
  }
  if (stopViewport) {
    stopViewport();
    stopViewport = null;
  }
  if (stopVisibility) {
    stopVisibility();
    stopVisibility = null;
  }
}

const INTERACTIVE_SOURCES = new Set([1, 2, 3, 5, 6]);
const isInteractive = (event) =>
  event.type === 3 && INTERACTIVE_SOURCES.has(event.data && event.data.source);

let idle = false;
let lastUserActivity = Date.now();

function pushMarker(tag, payload = {}, at = Date.now()) {
  pushEvent({ type: 5, data: { tag, payload }, timestamp: at });
}

export function stopRecording(nextStatus) {
  setStatus(nextStatus);
  resetBuffer();
  stopFlushTimer();
  stopAuxCapture();
  if (stopFn) {
    stopFn();
    stopFn = null;
  }
}

export function abort(message) {
  console.error(`capture: ${message} — recording stopped`);
  stopRecording('failed');
}

function onSessionRotated(next) {
  setSessionId(next.id);
  resetBuffer();
  if (!next.sampled) {
    setStatus('disabled');
    setTimeout(() => {
      stopAuxCapture();
      if (stopFn) {
        stopFn();
        stopFn = null;
      }
      watchForNextSession();
    }, 0);
    return;
  }
  setStatus(gatesSatisfied() ? 'active' : 'buffering');
  record.takeFullSnapshot(true);
  checkUrlTriggers();
}

export function startRecording() {
  setStatus(gatesSatisfied() ? 'active' : 'buffering');
  idle = false;
  lastUserActivity = Date.now();
  stopFn = record({
    emit(event) {
      if (isInteractive(event)) {
        lastUserActivity = Date.now();
        const current = getSession();
        if (current.rotated) {
          onSessionRotated(current);
          if (status === 'disabled') return;
        }
        if (idle) {
          idle = false;
          pushMarker('capture-idle-end');
          record.takeFullSnapshot(true);
        }
        pushEvent(event);
        return;
      }

      if (idle) return;
      if (Date.now() - lastUserActivity > config.idleThresholdMs) {
        idle = true;
        pushMarker('capture-idle');
        flush(false);
        return;
      }
      pushEvent(event);
    },
    checkoutEveryNms: config.checkoutEveryNms,
    errorHandler(err) {
      console.warn('capture: rrweb internal error, recording continues', err);
      return true;
    },

    maskAllInputs: true,
    blockClass: 'capture-block',
    maskTextClass: 'capture-mask',
    ...(config.maskTextSelector ? { maskTextSelector: config.maskTextSelector } : {}),
    ...(config.blockSelector ? { blockSelector: config.blockSelector } : {}),

    sampling: {
      mousemove: 50,
      scroll: 100,
      media: 800,
      input: 'last',
    },
  });
  stopClicks = startClickCapture(
    (payload) => record.addCustomEvent('capture-click', payload),
    config
  );
  stopViewport = startViewportCapture((payload) => record.addCustomEvent('capture-viewport', payload));
  stopVisibility = startVisibilityCapture((state, at) => {
    pushMarker('capture-visibility', { state }, at);
    if (state === 'hidden') flush('hidden');
  });
  scheduleFlush();
  checkUrlTriggers();
}

export function watchForNextSession() {
  let lastCheck = 0;
  const onActivity = () => {
    const now = Date.now();
    if (now - lastCheck < 5000) return;
    lastCheck = now;
    const current = getSession();
    if (!current.rotated) return;
    setSessionId(current.id);
    if (!current.sampled) return;
    for (const type of ['pointerdown', 'keydown', 'scroll']) {
      removeEventListener(type, onActivity, true);
    }
    startRecording();
  };
  for (const type of ['pointerdown', 'keydown', 'scroll']) {
    addEventListener(type, onActivity, { capture: true, passive: true });
  }
}
