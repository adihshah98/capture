#!/usr/bin/env bash
set -euo pipefail

# Each deploy builds a self-contained release directory and the service is
# pointed at it by a symlink. Nothing is written into the tree the running
# server is executing from: no tar over live code, no pip into the venv the
# live interpreter is using. The new release is started on a spare port and
# health-checked before anything switches, so a missing module or a failed
# migration is caught while the old release is still serving. Rolling back is
# repointing the symlink at a release that already ran.

HOST="${CAPTURE_HOST:-ec2-user@ec2-35-167-249-40.us-west-2.compute.amazonaws.com}"
KEY="${CAPTURE_KEY:-$HOME/Downloads/capture-key.pem}"
PUBLIC_URL="${CAPTURE_PUBLIC_URL:-https://d1lt8dgxjrizij.cloudfront.net}"
# Each release carries its own venv and node_modules, a few hundred MB.
KEEP_RELEASES="${CAPTURE_KEEP_RELEASES:-3}"
PREFLIGHT_PORT="${CAPTURE_PREFLIGHT_PORT:-8099}"

# The repo root, not this script's directory: everything below — the build, the
# tar whitelist, the git rev — is relative to the root.
cd "$(dirname "$0")/.."
npm run build

RELEASE="$(date -u +%Y%m%d%H%M%S)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null || RELEASE="$RELEASE-dirty"
echo "release $RELEASE"

# A glob, not a list: an explicit one silently drops any module added since
# it was last edited, and the server only fails on import at startup.
# `capture` is the whole Python app, so this no longer enumerates modules by
# name -- that list silently omitted analyze_store.py from the day it was added,
# and the box ran the pre-SQLite store until it was noticed.
# COPYFILE_DISABLE keeps macOS tar from shipping AppleDouble forks
# (._0001_init.sql). Those match *.sql, sort first, and are not UTF-8 —
# which is how a deploy failed in migrate() before applying anything.
COPYFILE_DISABLE=1 tar czf /tmp/capture-deploy.tar.gz \
  --exclude='__pycache__' --exclude='._*' \
  capture \
  migrations requirements.txt public \
  package.json package-lock.json \
  src/analyze/extract.mjs src/analyze/replayer.bundle.js
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new /tmp/capture-deploy.tar.gz "$HOST:/tmp/"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" 'bash -s' \
  "$RELEASE" "$KEEP_RELEASES" "$PREFLIGHT_PORT" <<'REMOTE'
set -euo pipefail
RELEASE="${1:?release id}"; KEEP="${2:?keep count}"; PORT="${3:?preflight port}"

ROOT=/opt/capture
RELEASES="$ROOT/releases"
SHARED="$ROOT/shared"
TARGET="$RELEASES/$RELEASE"
UNIT=/etc/systemd/system/capture.service

sudo mkdir -p "$RELEASES" "$SHARED/playwright"
sudo chown -R ec2-user "$RELEASES" "$SHARED"
rm -rf "$TARGET" && mkdir -p "$TARGET"
# A release that never went live must not linger: it would otherwise age out a
# release that did, leaving nothing good to roll back to.
trap 'if [ "$(readlink -f "$ROOT/current" 2>/dev/null)" != "$TARGET" ]; then rm -rf "$TARGET"; fi' EXIT
tar xzf /tmp/capture-deploy.tar.gz -C "$TARGET"

echo "building the release venv"
python3.11 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/pip" install -q -r "$TARGET/requirements.txt"

# The browser download is large and identical across releases, so it lives in
# shared and is only fetched when missing.
export PLAYWRIGHT_BROWSERS_PATH="$SHARED/playwright"
if command -v node >/dev/null 2>&1; then
  (cd "$TARGET" && npm install --omit=dev --no-audit --no-fund --silent)
  if [ -z "$(ls -A "$SHARED/playwright" 2>/dev/null)" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      (cd "$TARGET" && npx --yes playwright install --with-deps chromium)
    else
      sudo dnf install -y -q nss nspr atk at-spi2-atk at-spi2-core cups-libs \
        libdrm libXcomposite libXdamage libXext libXfixes libXrandr \
        mesa-libgbm libxkbcommon pango cairo alsa-lib
      (cd "$TARGET" && npx --yes playwright install chromium)
    fi
  else
    echo "playwright browser already in shared"
  fi
else
  echo "warning: node not installed — AI analysis will be unavailable" >&2
fi

set -a; . /etc/capture.env; set +a
if [ -z "${CAPTURE_DATABASE_URL:-}" ]; then
  echo "CAPTURE_DATABASE_URL is not in /etc/capture.env — refusing to deploy" >&2
  exit 1
fi

if [ "${CAPTURE_READ_ONLY:-}" = "1" ]; then
  echo "read-only server — skipping migrations, it does not own this schema"
elif ! (cd "$TARGET" && .venv/bin/python -m capture.db); then
  echo "migrations failed — the running release is untouched" >&2
  exit 1
fi

# Prove the release boots and answers before the live one is disturbed. This is
# what catches a missing module, which previously only surfaced once the old
# code had already been overwritten. CAPTURE_PREFLIGHT keeps this instance
# inert: no retention sweep racing the live one, no cohort batch resumed twice.
echo "pre-flighting on port $PORT"
# The port must be silent first. An instance leaked by an earlier deploy would
# answer in place of the new release and pass one that cannot even import.
pkill -f "uvicorn capture.server:app .*--port $PORT" 2>/dev/null || true
sleep 1
if curl -sf -o /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null; then
  echo "something is still answering on port $PORT — refusing to pre-flight blind" >&2
  exit 1
fi

# Not wrapped in a subshell: $! has to be uvicorn itself, or the kill below
# reaps the subshell and leaves the server running.
cd "$TARGET"
CAPTURE_PREFLIGHT=1 .venv/bin/uvicorn capture.server:app \
  --host 127.0.0.1 --port "$PORT" --log-level warning >/tmp/preflight.log 2>&1 &
PREFLIGHT_PID=$!
cd "$ROOT"
ok=0
for _ in $(seq 1 40); do
  kill -0 "$PREFLIGHT_PID" 2>/dev/null || break
  if curl -sf -o /tmp/preflight.json "http://127.0.0.1:$PORT/healthz"; then ok=1; break; fi
  sleep 1
done
kill "$PREFLIGHT_PID" 2>/dev/null || true
wait "$PREFLIGHT_PID" 2>/dev/null || true
pkill -f "uvicorn capture.server:app .*--port $PORT" 2>/dev/null || true
if [ "$ok" -ne 1 ]; then
  echo "the new release did not come up — the running one is untouched" >&2
  tail -20 /tmp/preflight.log >&2 || true
  exit 1
fi
echo "pre-flight ok: $(cat /tmp/preflight.json)"

PREVIOUS="$(readlink -f "$ROOT/current" 2>/dev/null || true)"

# Swap first: until this exists, the rewritten unit would name a missing path.
ln -sfnT "$TARGET" "$ROOT/current.new"
mv -fT "$ROOT/current.new" "$ROOT/current"

sudo tee "$UNIT" >/dev/null <<UNITFILE
[Unit]
Description=Capture session replay server
After=network.target

[Service]
User=ec2-user
WorkingDirectory=$ROOT/current
Environment=PLAYWRIGHT_BROWSERS_PATH=$SHARED/playwright
EnvironmentFile=/etc/capture.env
EnvironmentFile=/etc/capture-cloudfront.env
ExecStart=$ROOT/current/.venv/bin/uvicorn capture.server:app --host 0.0.0.0 --port 80 --proxy-headers --forwarded-allow-ips=\${FORWARDED_ALLOW_IPS} --timeout-graceful-shutdown 25
AmbientCapabilities=CAP_NET_BIND_SERVICE
TimeoutStopSec=40
Restart=always

[Install]
WantedBy=multi-user.target
UNITFILE
sudo systemctl daemon-reload
sudo systemctl restart capture

for _ in $(seq 1 30); do
  if curl -sf -o /tmp/health.json localhost/healthz; then
    echo "origin health: $(cat /tmp/health.json)"
    live="$(readlink -f "$ROOT/current")"
    ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
      [ "$(readlink -f "$old")" = "$live" ] && continue
      rm -rf "$old"
    done
    echo "releases on the box: $(ls -1 "$RELEASES" | wc -l)"
    exit 0
  fi
  sleep 1
done

echo "origin health check FAILED" >&2
curl -s localhost/healthz >&2 || echo "(no response at all)" >&2
if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ]; then
  echo "rolling back to $(basename "$PREVIOUS")" >&2
  ln -sfnT "$PREVIOUS" "$ROOT/current.new"
  mv -fT "$ROOT/current.new" "$ROOT/current"
  sudo systemctl restart capture
  sleep 3
  if curl -sf -o /dev/null localhost/healthz; then
    echo "rolled back to the previous release, which is healthy" >&2
  else
    echo "ROLLBACK ALSO UNHEALTHY — the box needs manual attention" >&2
  fi
else
  echo "no previous release to roll back to" >&2
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

echo "deployed $RELEASE to $HOST and verified at $PUBLIC_URL"
