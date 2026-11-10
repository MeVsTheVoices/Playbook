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
OUTPUT_DIR=""
SERVER_ARCH=""
SERVER_FORMATS="raw.xz,qcow2"
SERVER_DISK_SIZE="20G"
DISTRIBUTION="${CLOUD_INIT_DISTRIBUTION:-standard}"
VALIDATE_CLOUD_INIT_SCHEMA="${CLOUD_INIT_VALIDATE_SCHEMA:-true}"
DEFAULT_K3S_SERVER_HOST="${CLOUD_INIT_K3S_SERVER_HOST:-}"
if [[ -z "$DEFAULT_K3S_SERVER_HOST" ]]; then
  DEFAULT_K3S_SERVER_HOST="$(hostname -I | awk '{print $1}')"
  [[ "$DISTRIBUTION" != "k3s" ]] || echo "WARNING: CLOUD_INIT_K3S_SERVER_HOST unset; defaulting the K3s server to this machine's LAN IP (${DEFAULT_K3S_SERVER_HOST}). Joining nodes need the Tailscale address instead." >&2
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
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --arch)
      SERVER_ARCH="$2"
      shift 2
      ;;
    --formats)
      SERVER_FORMATS="$2"
      shift 2
      ;;
    --disk-size)
      SERVER_DISK_SIZE="$2"
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

if [[ -z "$PROFILE" || -z "$HOSTNAME" || -z "$DEVICE$OUTPUT_DIR" || -z "$SSH_KEY_FILE" || -z "$CALLBACK_URL" ]]; then
  echo "Missing required argument(s)." >&2
  usage
  exit 1
fi
if [[ -n "$DEVICE" && -n "$OUTPUT_DIR" ]]; then
  echo "Use either --device or --output-dir, not both." >&2
  exit 1
fi

if [[ "$PROFILE" != "pi5" && "$PROFILE" != "pi4" && "$PROFILE" != "pi3bplus" && "$PROFILE" != "network-toolkit" && "$PROFILE" != "playbook-server" ]]; then
  echo "Invalid --profile value: $PROFILE" >&2
  exit 1
fi
if [[ "$PROFILE" != "playbook-server" && ( -n "$OUTPUT_DIR" || -n "$SERVER_ARCH" ) ]]; then
  echo "--output-dir and --arch are only supported for the playbook-server profile." >&2
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

if [[ "$PROFILE" == "playbook-server" ]]; then
  if [[ "$DISTRIBUTION" != "standard" ]]; then
    echo "playbook-server requires the standard distribution, not K3s." >&2
    exit 1
  fi
  # The image default follows the architecture; a supplied image names its own.
  if [[ -z "$SERVER_ARCH" ]]; then
    SERVER_ARCH="amd64"
    if [[ "${IMAGE_URL,,} ${IMAGE_FILE,,}" =~ (arm64|aarch64) ]]; then
      SERVER_ARCH="arm64"
    fi
  fi
  if [[ "$SERVER_ARCH" != "amd64" && "$SERVER_ARCH" != "arm64" ]]; then
    echo "Invalid --arch value: $SERVER_ARCH (use amd64 or arm64)." >&2
    exit 1
  fi
  if [[ ! "$SERVER_FORMATS" =~ ^(raw|raw\.xz|qcow2)(,(raw|raw\.xz|qcow2))*$ ]]; then
    echo "Invalid --formats value: $SERVER_FORMATS (comma-separated raw, raw.xz, qcow2)." >&2
    exit 1
  fi
  if [[ ! "$SERVER_DISK_SIZE" =~ ^[1-9][0-9]*[GT]$ ]]; then
    echo "Invalid --disk-size value: $SERVER_DISK_SIZE (for example 20G)." >&2
    exit 1
  fi
  server_render packages >/dev/null
fi

if [[ -n "$DEVICE" && ! -b "$DEVICE" ]]; then
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
if [[ "$PROFILE" == "playbook-server" ]]; then
  require_cmd qemu-img
  require_cmd losetup
  require_cmd blkid
fi

# Serialize writers without locking the block device: udev needs that device lock
# to process the partition-change events triggered by partprobe.
if [[ -n "$DEVICE" ]]; then
  DEVICE_LOCK_ID="$(stat -Lc '%t-%T' -- "$DEVICE")"
  DEVICE_LOCK_DIR=/run/playbook-sd-locks
  mkdir -p "$DEVICE_LOCK_DIR"
  if ! exec 8>"$DEVICE_LOCK_DIR/$DEVICE_LOCK_ID.lock"; then
    echo "Cannot open writer lock for target device: $DEVICE" >&2
    exit 1
  fi
  if ! flock -n 8; then
    echo "Another SD preparation is using $DEVICE; refusing concurrent writes." >&2
    exit 1
  fi
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

if [[ "$PROFILE" == "playbook-server" ]]; then
  # shellcheck source=lib/server_image.sh
  source "$SCRIPT_DIR/lib/server_image.sh"
else
  # shellcheck source=lib/flash.sh
  source "$SCRIPT_DIR/lib/flash.sh"

  # shellcheck source=lib/finish.sh
  source "$SCRIPT_DIR/lib/finish.sh"
fi
