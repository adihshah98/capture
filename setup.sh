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

sudo tee /etc/systemd/system/capture.service >/dev/null <<'UNIT'
[Unit]
Description=Capture session replay server
After=network.target

[Service]
User=ec2-user
WorkingDirectory=/opt/capture
EnvironmentFile=/etc/capture.env
ExecStart=/opt/capture/.venv/bin/uvicorn server:app --host 0.0.0.0 --port 80
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
