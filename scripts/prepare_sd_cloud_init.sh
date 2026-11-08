#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

PROFILE=""
HOSTNAME=""
DEVICE=""
SSH_KEY_FILE=""
CALLBACK_URL=""
IMAGE_URL=""
IMAGE_FILE=""
IMAGE_SHA256=""
WORKSPACE="${PWD}/.build"
AUTO_CONFIRM="false"
DISTRIBUTION="${CLOUD_INIT_DISTRIBUTION:-standard}"
VALIDATE_CLOUD_INIT_SCHEMA="${CLOUD_INIT_VALIDATE_SCHEMA:-true}"
DEFAULT_K3S_SERVER_HOST="${CLOUD_INIT_K3S_SERVER_HOST:-}"
if [[ -z "$DEFAULT_K3S_SERVER_HOST" ]]; then
  DEFAULT_K3S_SERVER_HOST="$(hostname -I | awk '{print $1}')"
  echo "WARNING: CLOUD_INIT_K3S_SERVER_HOST unset; defaulting the K3s server to this machine's LAN IP (${DEFAULT_K3S_SERVER_HOST}). Joining nodes need the Tailscale address instead." >&2
fi
if [[ -z "$DEFAULT_K3S_SERVER_HOST" ]]; then
  DEFAULT_K3S_SERVER_HOST="127.0.0.1"
fi
DEFAULT_K3S_CONFIG="${CLOUD_INIT_K3S_CONFIG:-# pragma: k3s-config-v1
server: https://${DEFAULT_K3S_SERVER_HOST}:6443
node-name: ${HOSTNAME:-$(hostname)}
}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --hostname)
      HOSTNAME="$2"
      shift 2
      ;;
    --device)
      DEVICE="$2"
      shift 2
      ;;
    --ssh-key-file)
      SSH_KEY_FILE="$2"
      shift 2
      ;;
    --callback-url)
      CALLBACK_URL="$2"
      shift 2
      ;;
    --image-url)
      IMAGE_URL="$2"
      shift 2
      ;;
    --image-file)
      IMAGE_FILE="$2"
      shift 2
      ;;
    --workspace)
      WORKSPACE="$2"
      shift 2
      ;;
    --image-sha256)
      IMAGE_SHA256="$2"
      shift 2
      ;;
    --yes)
      AUTO_CONFIRM="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$PROFILE" || -z "$HOSTNAME" || -z "$DEVICE" || -z "$SSH_KEY_FILE" || -z "$CALLBACK_URL" ]]; then
  echo "Missing required argument(s)." >&2
  usage
  exit 1
fi

if [[ "$PROFILE" != "pi5" && "$PROFILE" != "pi4" && "$PROFILE" != "pi3bplus" && "$PROFILE" != "network-toolkit" ]]; then
  echo "Invalid --profile value: $PROFILE" >&2
  exit 1
fi

if [[ ! "$HOSTNAME" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]; then
  echo "Invalid --hostname value: ${HOSTNAME}" >&2
  echo "Allowed: letters, numbers, and '-' (max 63 chars; cannot start or end with '-')." >&2
  exit 1
fi

validate_http_url "--callback-url" "$CALLBACK_URL"

case "$DISTRIBUTION" in
  standard|k3s)
    ;;
  *)
    echo "Invalid distribution value: $DISTRIBUTION" >&2
    exit 1
    ;;
esac

if [[ "$PROFILE" == "network-toolkit" ]]; then
  if [[ "$DISTRIBUTION" != "standard" ]]; then
    echo "network-toolkit requires the standard distribution, not K3s." >&2
    exit 1
  fi
  if [[ ! "${CLOUD_INIT_TOOLKIT_ENABLE_FUNNEL:-false}" =~ ^(true|false)$ ]]; then
    echo "CLOUD_INIT_TOOLKIT_ENABLE_FUNNEL must be true or false." >&2
    exit 1
  fi
  toolkit_render packages >/dev/null
  if [[ ! "${CLOUD_INIT_TAILSCALE_AUTH_KEY:-}" =~ ^tskey-[A-Za-z0-9_-]+$ ]]; then
    echo "network-toolkit requires a Tailscale auth key before imaging." >&2
    exit 1
  fi
  if [[ -n "${CLOUD_INIT_TAILSCALE_ROUTES:-}" || -n "${CLOUD_INIT_TAILSCALE_EXIT_NODE:-}" || "${CLOUD_INIT_SSH_PORT:-22}" != "22" || "${CLOUD_INIT_USERNAME:-}" == "network-console" || "${CLOUD_INIT_USERNAME:-}" == "root" ]]; then
    echo "network-toolkit requires private SSH on port 22, a separate administrative user and no Tailscale routes or exit node." >&2
    exit 1
  fi
fi

if [[ ! -b "$DEVICE" ]]; then
  echo "Device does not exist or is not a block device: $DEVICE" >&2
  exit 1
fi

if [[ ! -f "$SSH_KEY_FILE" ]]; then
  echo "SSH key file not found: $SSH_KEY_FILE" >&2
  exit 1
fi

SSH_KEY_CONTENT="$(cat "$SSH_KEY_FILE")"
validate_ssh_public_key_lines "--ssh-key-file" "$SSH_KEY_CONTENT"

require_cmd curl
require_cmd lsblk
require_cmd dd
require_cmd mount
require_cmd umount
require_cmd mountpoint
require_cmd udevadm
require_cmd parted
require_cmd partprobe
require_cmd mktemp
require_cmd sha256sum
require_cmd python3
require_cmd openssl
require_cmd blockdev
require_cmd cmp
require_cmd flock

if ! exec 8<"$DEVICE"; then
  echo "Cannot open target device: $DEVICE" >&2
  exit 1
fi
if ! flock -n 8; then
  echo "Another SD preparation is using $DEVICE; refusing concurrent writes." >&2
  exit 1
fi

# shellcheck source=lib/image.sh
source "$SCRIPT_DIR/lib/image.sh"

# shellcheck source=lib/options.sh
source "$SCRIPT_DIR/lib/options.sh"

# shellcheck source=lib/user_data_base.sh
source "$SCRIPT_DIR/lib/user_data_base.sh"

# shellcheck source=lib/user_data_files.sh
source "$SCRIPT_DIR/lib/user_data_files.sh"

# shellcheck source=lib/user_data_scripts.sh
source "$SCRIPT_DIR/lib/user_data_scripts.sh"

# shellcheck source=lib/user_data_runcmd.sh
source "$SCRIPT_DIR/lib/user_data_runcmd.sh"

# shellcheck source=lib/validate.sh
source "$SCRIPT_DIR/lib/validate.sh"

# shellcheck source=lib/flash.sh
source "$SCRIPT_DIR/lib/flash.sh"

# shellcheck source=lib/finish.sh
source "$SCRIPT_DIR/lib/finish.sh"
