#!/usr/bin/env bash
# Ship the current code to the production EC2 box and restart the service.
# Requires ./setup.sh to have been run once on the box first.
# Recorded sessions (data/) are untouched — this only replaces code.
#
# The box is not reachable directly: its security group only admits
# CloudFront, which terminates TLS and is what brand tags point at. So the
# deploy is checked twice — on the origin, then through the CDN.
#
# A failed check rolls the previous code back and exits non-zero.
#
# Usage: ./deploy.sh
set -euo pipefail

HOST="ec2-user@ec2-35-167-249-40.us-west-2.compute.amazonaws.com"
KEY="$HOME/Downloads/capture-key.pem"
PUBLIC_URL="https://d1lt8dgxjrizij.cloudfront.net"

cd "$(dirname "$0")"
npm run build
tar czf /tmp/capture-deploy.tar.gz server.py requirements.txt public
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new /tmp/capture-deploy.tar.gz "$HOST:/tmp/"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/capture

# Snapshot the running code so a bad deploy can be undone. data/ is excluded:
# recordings are never part of the rollback.
rm -rf .rollback && mkdir .rollback
cp -a server.py requirements.txt public .rollback/ 2>/dev/null || true

tar xzf /tmp/capture-deploy.tar.gz
[ -d .venv ] || python3.11 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart capture

# /healthz reports storage writability and whether the bundles exist, so this
# fails on a broken deploy rather than on a closed port.
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

# The origin is healthy; confirm the path brands actually use works too. A
# CloudFront or security-group change can break this while the box is fine.
fail=0
check() { # description expected-status url
  actual=$(curl -s -o /dev/null -w '%{http_code}' "$3")
  if [ "$actual" = "$2" ]; then
    printf '  ok    %-34s %s\n' "$1" "$actual"
  else
    printf '  FAIL  %-34s %s (expected %s)\n' "$1" "$actual" "$2" >&2
    fail=1
  fi
}

echo "checking $PUBLIC_URL"
check "health"                 200 "$PUBLIC_URL/healthz"
check "snippet"                200 "$PUBLIC_URL/capture.js"
check "recorder bundle"        200 "$PUBLIC_URL/recorder.bundle.js"
check "remote config"          200 "$PUBLIC_URL/config"
check "replay UI is gated"     401 "$PUBLIC_URL/replay.html"
check "read API is gated"      401 "$PUBLIC_URL/api/sessions"

# Served, not just present: a truncated or stale build returns 200 too.
if curl -sf "$PUBLIC_URL/capture.js" | grep -q "recorder.bundle.js"; then
  printf '  ok    %-34s\n' "snippet content"
else
  printf '  FAIL  %-34s\n' "snippet content" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "deployed, but the public URL is not serving correctly — investigate before announcing" >&2
  exit 1
fi

echo "deployed to $HOST and verified at $PUBLIC_URL"
