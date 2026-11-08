# Usage text and the small validation helpers.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

usage() {
  cat <<'EOF'
Prepare an SD card with a cloud-init enabled Raspberry Pi image and callback metadata.

Usage:
  scripts/prepare_sd_cloud_init.sh \
    --profile pi5|pi4|pi3bplus|network-toolkit \
    --hostname HOSTNAME \
    --device /dev/sdX \
    --ssh-key-file ~/.ssh/id_ed25519.pub \
    --callback-url http://HOST:PORT/api/cloud-init/callback \
    [--image-url URL] \
    [--image-file /path/to/image.img|.img.xz] \
    [--image-sha256 HEX] \
    [--workspace /path/to/workdir] \
    [--yes]

Notes:
- This script will ERASE the selected device.
- Pi 5, Pi 4 and Pi 3B+ default to Ubuntu 24.04 preinstalled server ARM64.
- Legacy Pi OS Bookworm/Bullseye images do not support this cloud-init workflow.
- Network Toolkit defaults to Pi OS Lite 64-bit Trixie; set its Wi-Fi, browser password hash and Tailscale auth key before imaging.
- User-data is validated before erasing; the written image and injected seed are read back before reporting ready.
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

toolkit_render() {
  local interpreter="$SCRIPT_DIR/../.venv/bin/python"
  if [[ ! -x "$interpreter" ]]; then
    interpreter="$(command -v python3)"
  fi
  "$interpreter" "$SCRIPT_DIR/../callback_site/network_toolkit.py" "$1"
}

validate_http_url() {
  local name="$1"
  local value="$2"
  if ! python3 - "$value" <<'PY'
import re
import sys
from urllib.parse import urlparse

value = sys.argv[1]
if not value or re.search(r"[\x00-\x1f\x7f\s'\"]", value):
    raise SystemExit(1)
parts = urlparse(value)
if parts.scheme not in {"http", "https"}:
    raise SystemExit(1)
if not parts.netloc:
    raise SystemExit(1)
PY
  then
    echo "Invalid ${name}: ${value}" >&2
    exit 1
  fi
}

validate_ssh_public_key_lines() {
  local name="$1"
  local input="$2"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ ! "$line" =~ ^(ssh-(rsa|ed25519)|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
      echo "Invalid SSH public key line in ${name}: ${line}" >&2
      exit 1
    fi
  done <<< "$input"
}
