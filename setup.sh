#!/usr/bin/env bash
# One-time server bootstrap for the production EC2 box: app directory,
# generated replay password, systemd unit. Run once, then use ./deploy.sh
# for every code deploy. Re-running is safe — it won't rotate an existing
# password or disturb recorded data.
#
# Usage: ./setup.sh
set -euo pipefail

HOST="ec2-user@ec2-35-167-249-40.us-west-2.compute.amazonaws.com"
KEY="$HOME/Downloads/capture-key.pem"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail

# AL2023's default python3 is 3.9, too old for our pinned fastapi.
sudo dnf install -y -q python3.11 >/dev/null

sudo mkdir -p /opt/capture && sudo chown ec2-user /opt/capture

# Secrets live in /etc/capture.env, not the unit file, so redeploys and
# unit changes never rotate the password.
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

# CloudFront is the only thing the security group lets reach this box, and it
# appends the viewer's IP to X-Forwarded-For. Trusting its origin-facing ranges
# makes uvicorn report the real client instead of an edge address. Trusting '*'
# would instead take the *first* XFF entry, which the viewer can forge, so the
# list is refreshed here on every run — AWS changes it.
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
  # Empty means uvicorn trusts no proxy and falls back to the peer address:
  # rate limiting would see edge IPs, but nobody can forge a client IP.
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
# Lets uvicorn bind port 80 without running as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable capture 2>/dev/null
echo "bootstrap done — run ./deploy.sh to ship the code and start the service"
REMOTE
