#!/usr/bin/env bash
# One-time setup for the dashcam uploader. Run ON the comma device:
#   ./device_setup.sh <server_host> [ssh_port]
set -euo pipefail

HOST="${1:?usage: device_setup.sh <server_host> [ssh_port]}"
PORT="${2:-22}"
DIR=/data/dashcam_upload

mkdir -p "$DIR"

if [ ! -f "$DIR/id_ed25519" ]; then
  ssh-keygen -t ed25519 -N "" -f "$DIR/id_ed25519" -C "comma-dashcam"
fi

KEYSCAN_TMP="$(mktemp)"
ssh-keyscan -p "$PORT" "$HOST" > "$KEYSCAN_TMP" 2>/dev/null || true
if [ ! -s "$KEYSCAN_TMP" ]; then
  rm -f "$KEYSCAN_TMP"
  echo "error: ssh-keyscan got no keys from $HOST:$PORT (host unreachable?)" >&2
  exit 1
fi
mv "$KEYSCAN_TMP" "$DIR/known_hosts"

if [ ! -f "$DIR/config.json" ]; then
  cat > "$DIR/config.json" <<EOF
{
  "ssid": "CHANGE_ME_WIFI_NAME",
  "host": "$HOST",
  "port": $PORT,
  "user": "CHANGE_ME_SSH_USER",
  "remote_root": "/srv/dashcam",
  "files": ["fcamera.hevc", "ecamera.hevc", "qlog.zst", "qlog"]
}
EOF
  echo "Wrote $DIR/config.json — edit ssid and user before use."
fi

echo ""
echo "Add this public key to the server's ~/.ssh/authorized_keys:"
cat "$DIR/id_ed25519.pub"
