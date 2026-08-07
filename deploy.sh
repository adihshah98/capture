#!/usr/bin/env bash
set -euo pipefail

HOST="${CAPTURE_HOST:-ec2-user@ec2-35-167-249-40.us-west-2.compute.amazonaws.com}"
KEY="${CAPTURE_KEY:-$HOME/Downloads/capture-key.pem}"
PUBLIC_URL="${CAPTURE_PUBLIC_URL:-https://d1lt8dgxjrizij.cloudfront.net}"

cd "$(dirname "$0")"
npm run build
tar czf /tmp/capture-deploy.tar.gz \
  server.py analyze.py requirements.txt public \
  package.json package-lock.json \
  src/analyze/extract.mjs src/analyze/replayer.bundle.js
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new /tmp/capture-deploy.tar.gz "$HOST:/tmp/"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/capture

rm -rf .rollback && mkdir .rollback
cp -a server.py analyze.py requirements.txt public src .rollback/ 2>/dev/null || true

tar xzf /tmp/capture-deploy.tar.gz
[ -d .venv ] || python3.11 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

# AI analysis needs a headless browser to rebuild the recorded page. Without
# node the rest of the app still runs; the server just reports analysis as
# unavailable and the button stays hidden.
if command -v node >/dev/null 2>&1; then
  npm install --omit=dev --no-audit --no-fund --silent
  export PLAYWRIGHT_BROWSERS_PATH=/opt/capture/.playwright
  if command -v apt-get >/dev/null 2>&1; then
    npx --yes playwright install --with-deps chromium
  else
    sudo dnf install -y -q nss nspr atk at-spi2-atk at-spi2-core cups-libs \
      libdrm libXcomposite libXdamage libXext libXfixes libXrandr \
      mesa-libgbm libxkbcommon pango cairo alsa-lib
    npx --yes playwright install chromium
  fi

  # Boxes bootstrapped before AI analysis existed have a unit without the
  # browser path, so the service would not find the chromium we just installed.
  if ! grep -q PLAYWRIGHT_BROWSERS_PATH /etc/systemd/system/capture.service; then
    sudo sed -i '/^WorkingDirectory=/a Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/capture/.playwright' \
      /etc/systemd/system/capture.service
    sudo systemctl daemon-reload
    echo "patched capture.service with the browser path"
  fi
else
  echo "warning: node not installed — AI analysis will be unavailable" >&2
fi

sudo systemctl restart capture

for i in $(seq 1 30); do
  if curl -sf -o /tmp/health.json localhost/healthz; then
    echo "origin health: $(cat /tmp/health.json)"
    exit 0
  fi
  sleep 1
done

echo "origin health check FAILED — rolling back" >&2
curl -s localhost/healthz >&2 || echo "(no response at all)" >&2
cp -a .rollback/. /opt/capture/
sudo systemctl restart capture
sleep 3
if curl -sf -o /dev/null localhost/healthz; then
  echo "rolled back to the previous build, which is healthy" >&2
else
  echo "ROLLBACK ALSO UNHEALTHY — the box needs manual attention" >&2
fi
exit 1
REMOTE

fail=0
check() {
  actual=$(curl -s -o /dev/null -w '%{http_code}' "$3")
  case " $2 " in
    *" $actual "*)
      printf '  ok    %-34s %s\n' "$1" "$actual" ;;
    *)
      printf '  FAIL  %-34s %s (expected %s)\n' "$1" "$actual" "$2" >&2
      fail=1 ;;
  esac
}

echo "checking $PUBLIC_URL"
check "health"                 200 "$PUBLIC_URL/healthz"
check "snippet"                200 "$PUBLIC_URL/capture.js"
check "recorder bundle"        200 "$PUBLIC_URL/recorder.bundle.js"
check "remote config"          200 "$PUBLIC_URL/config"
check "replay UI is gated"     "401 302" "$PUBLIC_URL/replay.html"
check "read API is gated"      401 "$PUBLIC_URL/api/sessions"

if curl -sf "$PUBLIC_URL/capture.js" | grep -q "recorder.bundle.js"; then
  printf '  ok    %-34s\n' "snippet content"
else
  printf '  FAIL  %-34s\n' "snippet content" >&2
  fail=1
fi

# Analysis is optional: report what the origin sees rather than failing the deploy.
health=$(curl -s "$PUBLIC_URL/healthz" || true)
case "$health" in
  *'"analysis_runtime_present":true'*)
    case "$health" in
      *'"analysis_configured":true'*)
        printf '  ok    %-34s\n' "AI analysis ready" ;;
      *)
        printf '  note  %-34s %s\n' "AI analysis" "browser ready, GEMINI_API_KEY not set" ;;
    esac ;;
  *)
    printf '  note  %-34s %s\n' "AI analysis" "headless browser unavailable — button hidden" ;;
esac

if [ "$fail" -ne 0 ]; then
  echo "deployed, but the public URL is not serving correctly — investigate before announcing" >&2
  exit 1
fi

echo "deployed to $HOST and verified at $PUBLIC_URL"
