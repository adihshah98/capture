#!/usr/bin/env bash
set -euo pipefail

HOST="${CAPTURE_STAGING_HOST:-ec2-user@ec2-54-212-242-132.us-west-2.compute.amazonaws.com}"
KEY="${CAPTURE_KEY:-$HOME/Downloads/capture-key.pem}"

if ! curl -sf -m 2 http://localhost:8080/healthz >/dev/null; then
  ssh -i "$KEY" -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
    -o StrictHostKeyChecking=accept-new -L 8080:localhost:80 "$HOST"
  echo "tunnel opened on localhost:8080"
fi

CAPTURE_HOST="$HOST" CAPTURE_PUBLIC_URL=http://localhost:8080 exec "$(dirname "$0")/deploy.sh"
