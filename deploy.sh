#!/usr/bin/env bash
# Ship the current code to the production EC2 box and restart the service.
# Requires ./setup.sh to have been run once on the box first.
# Recorded sessions (data/) are untouched — this only replaces code.
#
# Usage: ./deploy.sh
set -euo pipefail

HOST="ec2-user@ec2-35-167-249-40.us-west-2.compute.amazonaws.com"
KEY="$HOME/Downloads/capture-key.pem"

cd "$(dirname "$0")"
npm run build
tar czf /tmp/capture-deploy.tar.gz server.py requirements.txt public
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new /tmp/capture-deploy.tar.gz "$HOST:/tmp/"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd /opt/capture
tar xzf /tmp/capture-deploy.tar.gz
[ -d .venv ] || python3.11 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart capture
sleep 2
curl -sf localhost/config >/dev/null && echo "health check: OK"
REMOTE

echo "deployed to $HOST"
