/**
 * capture.js — loader snippet. Derives the server URL and project token
 * from its own <script> tag, then lazy-loads the recorder bundle:
 *
 *   <script src="https://capture.example.com/capture.js" data-token="cap_xxx"></script>
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
  // Published globally for the recorder bundle; inline page config wins
  // over the derived values.
  var config = (window.CAPTURE_CONFIG = Object.assign(derived, window.CAPTURE_CONFIG || {}));

  function loadRecorder() {
    var script = document.createElement('script');
    script.src = config.recorderUrl || '/recorder.bundle.js';
    script.async = true;
    document.head.appendChild(script);
  }

  // Idle-deferred so recording never competes with the host page's load.
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
