import { resolveConfig } from './recorder/config.js';
import {
  forgetVisitor,
  getSession,
  getVisitorId,
  getWindowId,
  isOptedOut,
  setOptedOut,
} from './recorder/identity.js';
import { setSessionId, setStatus, setVisitorId, setWindowId, status } from './recorder/state.js';
import { flush } from './recorder/transport.js';
import { setupTriggers } from './recorder/triggers.js';
import { startRecording, stopRecording, watchForNextSession } from './recorder/recording.js';

let booted = false;

async function boot() {
  if (booted) return;
  booted = true;

  await resolveConfig();

  const session = getSession();
  setSessionId(session.id);
  setWindowId(getWindowId());
  setVisitorId(getVisitorId());
  setupTriggers();

  if (session.sampled) {
    startRecording();
  } else {
    watchForNextSession();
  }

  window.addEventListener('pagehide', () => flush('unload'));
  window.addEventListener('beforeunload', () => flush('unload'));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush('hidden');
  });
}

function publishConsentApi() {
  window.capture = Object.assign(window.capture || {}, {
    status: () => status,
    grantConsent: () => {
      setOptedOut(false);
      boot();
    },
    optIn: () => window.capture.grantConsent(),
    optOut: () => {
      setOptedOut(true);
      stopRecording('opted-out');
      forgetVisitor();
    },
    isOptedOut,
  });
}

function main() {
  const inline = window.CAPTURE_CONFIG || {};
  publishConsentApi();

  if (isOptedOut()) {
    setStatus('opted-out');
    return;
  }
  if (navigator.globalPrivacyControl === true && inline.respectGlobalPrivacyControl !== false) {
    setStatus('opted-out');
    return;
  }
  if (inline.requireConsent === true) {
    setStatus('awaiting-consent');
    return;
  }

  boot();
}

main();
