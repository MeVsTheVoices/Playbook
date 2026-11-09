#!/usr/bin/env bash
# Run the host SD/USB flasher used by the containerised callback dashboard.
#
#   scripts/run_sd_flasher.sh            run in the foreground (uses sudo)
#   scripts/run_sd_flasher.sh install    install and start a systemd service
#   scripts/run_sd_flasher.sh uninstall  stop and remove that service
#   scripts/run_sd_flasher.sh status     show the service status
#
# The shared token lives in .local/sd-flasher.env, which Docker Compose loads
# into the dashboard. Restart the dashboard after the token is first created.
# SD_FLASHER_BIND (default 127.0.0.1) sets the listen address for install; on a
# native Docker Engine host use the docker0 gateway (e.g. 172.17.0.1).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN_FILE="${SD_FLASHER_TOKEN_FILE:-$REPO_DIR/.local/sd-flasher.env}"
AGENT="$REPO_DIR/callback_site/sd_flasher.py"
UNIT_NAME="playbook-sd-flasher.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"

ensure_token() {
  if [[ -s "$TOKEN_FILE" ]]; then
    return
  fi
  mkdir -p "$(dirname "$TOKEN_FILE")"
  (umask 077 && printf 'SD_FLASHER_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > "$TOKEN_FILE")
  echo "Created $TOKEN_FILE; recreate the dashboard so it loads the token:" >&2
  echo "  docker compose up -d --force-recreate callback-site" >&2
}

case "${1:-run}" in
  run)
    shift || true
    ensure_token
    exec sudo python3 "$AGENT" --token-file "$TOKEN_FILE" "$@"
    ;;
  install)
    ensure_token
    PYTHON="$(command -v python3)"
    BIND="${SD_FLASHER_BIND:-127.0.0.1}"
    if [[ ! "$BIND" =~ ^[0-9.]+$ ]]; then
      echo "SD_FLASHER_BIND must be an IPv4 address: $BIND" >&2
      exit 2
    fi
    sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Playbook SD/USB flasher for the containerised callback dashboard
# docker0 must exist before binding to its gateway address.
After=network.target docker.service

[Service]
ExecStart=$PYTHON $AGENT --bind $BIND --token-file $TOKEN_FILE
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable "$UNIT_NAME"
    sudo systemctl restart "$UNIT_NAME"
    sudo systemctl --no-pager --lines=5 status "$UNIT_NAME"
    ;;
  uninstall)
    sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
    sudo rm -f "$UNIT_PATH"
    sudo systemctl daemon-reload
    echo "Removed $UNIT_NAME (token file $TOKEN_FILE kept)."
    ;;
  status)
    systemctl --no-pager status "$UNIT_NAME"
    ;;
  *)
    echo "Usage: $0 [run [agent options]|install|uninstall|status]" >&2
    exit 2
    ;;
esac
