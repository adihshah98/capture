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
  var config = (window.CAPTURE_CONFIG = Object.assign(derived, window.CAPTURE_CONFIG || {}));

  // Page config outranks the derived values, so a page that folds untrusted
  // input into CAPTURE_CONFIG must not be able to point this at a third party.
  function allowedBundleUrl(url) {
    try {
      var resolved = new URL(url, document.baseURI);
      var origins = [location.origin];
      if (script && script.src) origins.push(new URL(script.src).origin);
      if (origins.indexOf(resolved.origin) === -1) {
        console.error('capture: refusing recorder bundle from ' + resolved.origin);
        return null;
      }
      return resolved.href;
    } catch (err) {
      console.error('capture: unusable recorderUrl', err);
      return null;
    }
  }

  function loadRecorder() {
    var src = allowedBundleUrl(config.recorderUrl || '/recorder.bundle.js');
    if (!src) return;
    var el = document.createElement('script');
    el.src = src;
    el.async = true;
    document.head.appendChild(el);
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
