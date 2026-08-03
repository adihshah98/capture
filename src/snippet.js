/**
 * capture.js — tiny loader snippet (the equivalent of PostHog's array.js).
 *
 * Self-configuring: the snippet derives the capture server's location from
 * its own script src and reads the project token from data-token, so a
 * brand's entire integration is one tag:
 *
 *   <script src="https://capture.example.com/capture.js" data-token="cap_xxx"></script>
 *
 * It stays small so instrumented pages pay almost nothing up front: the
 * real rrweb recorder bundle is lazy-loaded once the page is idle.
 *
 * Optional overrides, before the script tag (inline config always wins):
 *   window.CAPTURE_CONFIG = { endpoint: '...', recorderUrl: '...', token: '...' }
 */
(function () {
  if (window.__captureLoaded) return;
  window.__captureLoaded = true;

  // document.currentScript is only set while this script executes, so
  // capture it now — loadRecorder runs later from an idle callback.
  var script = document.currentScript;
  var derived = {};
  if (script && script.src) {
    var base = new URL(script.src).origin;
    derived.endpoint = base + '/ingest';
    derived.recorderUrl = base + '/recorder.bundle.js';
    derived.configUrl = base + '/config';
    if (script.getAttribute('data-token')) {
      derived.token = script.getAttribute('data-token');
    }
  }
  // Publish the merged config so the recorder bundle picks up the derived
  // endpoints; anything the page set inline stays untouched.
  var config = (window.CAPTURE_CONFIG = Object.assign(derived, window.CAPTURE_CONFIG || {}));

  function loadRecorder() {
    var script = document.createElement('script');
    script.src = config.recorderUrl || '/recorder.bundle.js';
    script.async = true;
    document.head.appendChild(script);
  }

  // Defer the heavy bundle until the browser is idle so recording never
  // competes with the host page's own load.
  function schedule() {
    if ('requestIdleCallback' in window) {
      requestIdleCallback(loadRecorder, { timeout: 3000 });
    } else {
      setTimeout(loadRecorder, 1500);
    }
  }

  if (document.readyState === 'complete') {
    schedule();
  } else {
    window.addEventListener('load', schedule);
  }
})();
