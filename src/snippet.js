/**
 * capture.js — tiny loader snippet (the equivalent of PostHog's array.js).
 *
 * Drop this on any page with a <script src="/capture.js"></script> tag.
 * It stays small so instrumented pages pay almost nothing up front: the
 * real rrweb recorder bundle is lazy-loaded once the page is idle.
 *
 * Configure (optional) before the script tag:
 *   window.CAPTURE_CONFIG = { endpoint: '/ingest', recorderUrl: '/recorder.bundle.js' }
 */
(function () {
  if (window.__captureLoaded) return;
  window.__captureLoaded = true;

  var config = window.CAPTURE_CONFIG || {};

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
