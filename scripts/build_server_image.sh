#!/usr/bin/env bash
# Build a seeded Ubuntu Server 24.04 image that runs this repository's dashboard on first boot.
# Suitable for local or future CI builds; needs root (loop mounts), via sudo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/build_server_image.sh --ssh-key-file ~/.ssh/id_ed25519.pub [options]
  scripts/build_server_image.sh --ssh-key "ssh-ed25519 AAAA... me@host" [options]

Options:
  --arch amd64|arm64        target architecture (default amd64)
  --hostname NAME           node hostname (default playbook-server)
  --username NAME           administrative login (default playbook)
  --repo-url URL            repository cloned on first boot (default the public Playbook repo)
  --ref REF                 branch, tag or commit to deploy (default main)
  --port PORT               dashboard port (default 8088)
  --no-flasher              skip the host SD/USB flasher
  --callback-url URL        cloud-init callback (default: the node's own dashboard)
  --formats LIST            comma-separated raw, raw.xz, qcow2 (default raw.xz,qcow2)
  --disk-size SIZE          qcow2 virtual disk size (default 20G)
  --output-dir DIR          where image files go (default dist/server-image)
  --workspace DIR           scratch space for the 3.5 GB raw image (default .build)
  --image-file FILE         use a local Ubuntu 24.04 generic cloud image instead of downloading

Writes NAME-ubuntu-24.04-ARCH.{img.xz,qcow2}, a JSON manifest and SHA256SUMS. Flash the .img.xz
to a disk (UEFI boot) or boot the qcow2 in a VM; first boot installs Docker, clones the repository
and starts the dashboard. Images contain only public keys; never bake auth keys into shared images.
EOF
}

ARCH="amd64"
HOSTNAME_VALUE="playbook-server"
USERNAME_VALUE="playbook"
REPO_URL=""
REF=""
PORT="8088"
FLASHER="true"
CALLBACK_URL=""
FORMATS="raw.xz,qcow2"
DISK_SIZE="20G"
OUTPUT_DIR="$REPO_DIR/dist/server-image"
WORKSPACE="$REPO_DIR/.build"
SSH_KEY_FILE=""
SSH_KEY=""
IMAGE_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch) ARCH="$2"; shift 2 ;;
    --hostname) HOSTNAME_VALUE="$2"; shift 2 ;;
    --username) USERNAME_VALUE="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-flasher) FLASHER="false"; shift ;;
    --callback-url) CALLBACK_URL="$2"; shift 2 ;;
    --formats) FORMATS="$2"; shift 2 ;;
    --disk-size) DISK_SIZE="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --ssh-key-file) SSH_KEY_FILE="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --image-file) IMAGE_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -n "$SSH_KEY" ]]; then
  KEY_TMP="$(mktemp)"
  trap 'rm -f "$KEY_TMP"' EXIT
  printf '%s\n' "$SSH_KEY" > "$KEY_TMP"
  SSH_KEY_FILE="$KEY_TMP"
fi
if [[ -z "$SSH_KEY_FILE" ]]; then
  echo "Provide --ssh-key-file or --ssh-key (a public key for the administrative login)." >&2
  exit 1
fi
if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "Invalid --port value: $PORT" >&2
  exit 1
fi
CALLBACK_URL="${CALLBACK_URL:-http://127.0.0.1:${PORT}/api/cloud-init/callback}"

cmd=(
  "$REPO_DIR/scripts/prepare_sd_cloud_init.sh"
  --profile playbook-server
  --hostname "$HOSTNAME_VALUE"
  --arch "$ARCH"
  --output-dir "$OUTPUT_DIR"
  --formats "$FORMATS"
  --disk-size "$DISK_SIZE"
  --ssh-key-file "$SSH_KEY_FILE"
  --callback-url "$CALLBACK_URL"
  --workspace "$WORKSPACE"
)
if [[ -n "$IMAGE_FILE" ]]; then
  cmd+=(--image-file "$IMAGE_FILE")
fi

# sudo drops the caller's environment, so the cloud-init settings travel explicitly.
settings=(
  CLOUD_INIT_DISTRIBUTION=standard
  CLOUD_INIT_USERNAME="$USERNAME_VALUE"
  CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH=true
  CLOUD_INIT_DISABLE_ROOT_SSH=true
  CLOUD_INIT_K3S_SERVER_HOST=127.0.0.1
  CLOUD_INIT_SERVER_REPO_URL="$REPO_URL"
  CLOUD_INIT_SERVER_REF="$REF"
  CLOUD_INIT_SERVER_PORT="$PORT"
  CLOUD_INIT_SERVER_INSTALL_FLASHER="$FLASHER"
  CLOUD_INIT_VALIDATE_SCHEMA="${CLOUD_INIT_VALIDATE_SCHEMA:-true}"
)
if [[ $EUID -eq 0 ]]; then
  env "${settings[@]}" "${cmd[@]}"
  exit $?
fi
sudo env "${settings[@]}" "${cmd[@]}"
