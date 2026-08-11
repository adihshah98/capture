"""The tag a brand pastes into its site, and the public URL it points at.

Shared by the CLI (`add-project` prints it) and the auth callback (which needs the
same public base for its redirect_uri), so it lives apart from both.
"""

import os

from . import paths

PUBLIC_URL_FILE = paths.DATA_ROOT / "public_url"

PLACEHOLDER_HOST = "https://capture.example.com"


def public_base_url() -> str:
    """Where this server is reachable from a browser. The env var wins so a box
    can be moved without rewriting state; the file is what setup.sh records."""
    try:
        from_file = PUBLIC_URL_FILE.read_text()
    except Exception:
        from_file = ""
    for value in (os.environ.get("CAPTURE_PUBLIC_URL"), from_file):
        if value:
            return value.strip().rstrip("/")
    return PLACEHOLDER_HOST


# Byte-identical to the block in docs/integration.md; a test pins them.
INTEGRATION_SNIPPET = """<script>
(function () {
  var host = '__HOST__';
  window.CAPTURE_CONFIG = Object.assign(
    { endpoint: host + '/ingest', configUrl: host + '/config', token: '__TOKEN__' },
    window.CAPTURE_CONFIG || {}
  );
  function load() {
    if (window.__captureLoaded) return;
    window.__captureLoaded = 1;
    var s = document.createElement('script');
    s.async = 1;
    s.src = host + '/recorder.bundle.js';
    s.onerror = function () { window.__captureLoaded = 0; };
    document.head.appendChild(s);
  }
  function schedule() {
    if ('requestIdleCallback' in window) requestIdleCallback(load, { timeout: 3000 });
    else setTimeout(load, 1500);
  }
  if (document.readyState === 'complete') schedule();
  else addEventListener('load', schedule);
})();
</script>"""


def integration_snippet(base: str, token: str, indent: str = "  ") -> str:
    body = INTEGRATION_SNIPPET.replace("__HOST__", base).replace("__TOKEN__", token)
    return "\n".join(indent + line if line else line for line in body.split("\n"))
