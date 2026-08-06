#!/usr/bin/env bash
set -euo pipefail

HOST="${CAPTURE_HOST:-ec2-user@ec2-35-167-249-40.us-west-2.compute.amazonaws.com}"
KEY="${CAPTURE_KEY:-$HOME/Downloads/capture-key.pem}"
PUBLIC_URL="${CAPTURE_PUBLIC_URL:-https://d1lt8dgxjrizij.cloudfront.net}"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" bash -s "$PUBLIC_URL" <<'REMOTE'
set -euo pipefail
PUBLIC_URL="${1:?public URL not passed through}"

sudo dnf install -y -q python3.11 >/dev/null

sudo mkdir -p /opt/capture && sudo chown ec2-user /opt/capture

mkdir -p /opt/capture/data
printf '%s\n' "$PUBLIC_URL" > /opt/capture/data/public_url
echo "public URL recorded: $PUBLIC_URL"

if ! sudo test -f /etc/capture.env; then
  {
    echo "REPLAY_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')"
    echo "RETENTION_DAYS=30"
  } | sudo tee /etc/capture.env >/dev/null
  sudo chmod 600 /etc/capture.env
  echo "generated new replay password (save it now):"
  sudo grep REPLAY_PASSWORD /etc/capture.env | cut -d= -f2
else
  echo "existing /etc/capture.env kept (password unchanged)"
fi

RANGES=$(curl -sf https://ip-ranges.amazonaws.com/ip-ranges.json | python3.11 -c "
import sys, json
d = json.load(sys.stdin)
v4 = [p['ip_prefix'] for p in d['prefixes'] if p['service'] == 'CLOUDFRONT_ORIGIN_FACING']
v6 = [p['ipv6_prefix'] for p in d['ipv6_prefixes'] if p['service'] == 'CLOUDFRONT_ORIGIN_FACING']
print(','.join(v4 + v6))
" 2>/dev/null || true)
if [ -n "${RANGES:-}" ]; then
  echo "FORWARDED_ALLOW_IPS=$RANGES" | sudo tee /etc/capture-cloudfront.env >/dev/null
  echo "trusted CloudFront ranges refreshed ($(echo "$RANGES" | tr ',' '\n' | wc -l | tr -d ' ') CIDRs)"
elif sudo test -f /etc/capture-cloudfront.env; then
  echo "warning: could not fetch CloudFront ranges — keeping the existing list"
else
  echo "FORWARDED_ALLOW_IPS=" | sudo tee /etc/capture-cloudfront.env >/dev/null
  echo "warning: could not fetch CloudFront ranges — proxy headers left untrusted"
fi

sudo tee /etc/systemd/system/capture.service >/dev/null <<'UNIT'
[Unit]
Description=Capture session replay server
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/opt/capture
EnvironmentFile=/etc/capture.env
EnvironmentFile=/etc/capture-cloudfront.env
ExecStart=/opt/capture/.venv/bin/uvicorn server:app --host 0.0.0.0 --port 80 --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable capture 2>/dev/null
echo "bootstrap done — run ./deploy.sh to ship the code and start the service"
REMOTE
