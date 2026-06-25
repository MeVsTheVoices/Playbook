#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare an SD card with a cloud-init enabled Raspberry Pi image and callback metadata.

Usage:
  scripts/prepare_sd_cloud_init.sh \
    --profile pi5|pibplus \
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
- For Pi 5, --image-url is optional and defaults to Ubuntu 24.04 preinstalled server ARM64.
- For Pi B+, pass --image-url or --image-file for your cloud-init compatible image.
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

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

if [[ "$PROFILE" != "pi5" && "$PROFILE" != "pibplus" ]]; then
  echo "Invalid --profile value: $PROFILE" >&2
  exit 1
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

require_cmd curl
require_cmd lsblk
require_cmd dd
require_cmd mount
require_cmd umount
require_cmd mountpoint
require_cmd udevadm
require_cmd parted
require_cmd mktemp
require_cmd sha256sum

if [[ -z "$IMAGE_FILE" ]]; then
  if [[ -z "$IMAGE_URL" && "$PROFILE" == "pi5" ]]; then
    IMAGE_URL="https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz"
  fi

  if [[ -z "$IMAGE_URL" ]]; then
    echo "Provide --image-url or --image-file for profile: $PROFILE" >&2
    exit 1
  fi
fi

mkdir -p "$WORKSPACE"
TMP_DIR="$(mktemp -d "${WORKSPACE}/sdprep.XXXXXX")"
trap 'if mountpoint -q "$TMP_DIR/boot"; then umount "$TMP_DIR/boot"; fi; rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/boot"

DOWNLOADED_IMAGE=""
SOURCE_IMAGE=""

if [[ -n "$IMAGE_FILE" ]]; then
  if [[ ! -f "$IMAGE_FILE" ]]; then
    echo "Image file not found: $IMAGE_FILE" >&2
    exit 1
  fi
  SOURCE_IMAGE="$IMAGE_FILE"
else
  DOWNLOADED_IMAGE="$TMP_DIR/$(basename "$IMAGE_URL")"
  echo "Downloading image: $IMAGE_URL"
  EXPECTED_SIZE="$(curl -fsSLI "$IMAGE_URL" | awk -F': *' 'tolower($1) == "content-length" {gsub("\r", "", $2); print $2; exit}')"
  curl -fsSL "$IMAGE_URL" -o "$DOWNLOADED_IMAGE" &
  CURL_PID="$!"
  LAST_PCT=-1
  LAST_BYTES=0

  while kill -0 "$CURL_PID" 2>/dev/null; do
    CURRENT_SIZE="$(stat -c%s "$DOWNLOADED_IMAGE" 2>/dev/null || echo 0)"
    if [[ -n "$EXPECTED_SIZE" && "$EXPECTED_SIZE" =~ ^[0-9]+$ && "$EXPECTED_SIZE" -gt 0 ]]; then
      PCT=$(( CURRENT_SIZE * 100 / EXPECTED_SIZE ))
      if [[ "$PCT" -gt 100 ]]; then
        PCT=100
      fi
      if [[ "$LAST_PCT" -lt 0 || "$PCT" -ge $(( LAST_PCT + 5 )) || "$PCT" -eq 100 ]]; then
        echo "Download progress: ${PCT}% (${CURRENT_SIZE}/${EXPECTED_SIZE} bytes)"
        LAST_PCT="$PCT"
      fi
    else
      if [[ "$LAST_BYTES" -eq 0 || "$CURRENT_SIZE" -ge $(( LAST_BYTES + 52428800 )) ]]; then
        echo "Download progress: ${CURRENT_SIZE} bytes"
        LAST_BYTES="$CURRENT_SIZE"
      fi
    fi
    sleep 2
  done

  wait "$CURL_PID"
  echo "Download complete: $DOWNLOADED_IMAGE"
  SOURCE_IMAGE="$DOWNLOADED_IMAGE"
fi

if [[ -z "$IMAGE_SHA256" && -n "$IMAGE_URL" ]]; then
  HASH_URL_DOT_SHA256="${IMAGE_URL}.sha256"
  HASH_FILE="$TMP_DIR/image.sha256"
  if curl -fsL "$HASH_URL_DOT_SHA256" -o "$HASH_FILE"; then
    IMAGE_SHA256="$(awk '/[A-Fa-f0-9]{64}/ {print $1; exit}' "$HASH_FILE")"
    if [[ -n "$IMAGE_SHA256" ]]; then
      echo "Found checksum at ${HASH_URL_DOT_SHA256}"
    fi
  fi

  if [[ -z "$IMAGE_SHA256" ]]; then
    HASH_URL_SUMS="$(dirname "$IMAGE_URL")/SHA256SUMS"
    HASH_SUMS_FILE="$TMP_DIR/SHA256SUMS"
    if curl -fsL "$HASH_URL_SUMS" -o "$HASH_SUMS_FILE"; then
      IMAGE_BASENAME="$(basename "$SOURCE_IMAGE")"
      IMAGE_SHA256="$(awk -v f="$IMAGE_BASENAME" '$2 ~ f {print $1; exit}' "$HASH_SUMS_FILE")"
      if [[ -n "$IMAGE_SHA256" ]]; then
        echo "Found checksum in ${HASH_URL_SUMS}"
      fi
    fi
  fi
fi

if [[ -z "$IMAGE_SHA256" ]]; then
  echo "Hash verification skipped (no expected SHA256 provided or discovered)."
else
  if [[ ! "$IMAGE_SHA256" =~ ^[A-Fa-f0-9]{64}$ ]]; then
    echo "Invalid --image-sha256 value. Expected a 64-character hex SHA256." >&2
    exit 1
  fi

  ACTUAL_SHA256="$(sha256sum "$SOURCE_IMAGE" | awk '{print $1}')"
  EXPECTED_SHA256="$(echo "$IMAGE_SHA256" | tr 'A-F' 'a-f')"
  if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
    echo "Image hash verification failed." >&2
    echo "Expected: $EXPECTED_SHA256" >&2
    echo "Actual:   $ACTUAL_SHA256" >&2
    exit 1
  fi
  echo "Image hash verified: $ACTUAL_SHA256"
fi

IMG_TO_WRITE="$TMP_DIR/base.img"
if [[ "$SOURCE_IMAGE" == *.xz ]]; then
  require_cmd xz
  echo "Decompressing image..."
  xz -dc "$SOURCE_IMAGE" > "$IMG_TO_WRITE"
elif [[ "$SOURCE_IMAGE" == *.img ]]; then
  cp "$SOURCE_IMAGE" "$IMG_TO_WRITE"
else
  echo "Unsupported image format. Use .img or .img.xz" >&2
  exit 1
fi

echo "About to write image to $DEVICE"
lsblk "$DEVICE"
if [[ "$AUTO_CONFIRM" != "true" ]]; then
  read -r -p "Type YES to continue: " CONFIRM
  if [[ "$CONFIRM" != "YES" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

echo "Unmounting existing partitions on $DEVICE"
while read -r part _; do
  if [[ -n "$part" ]]; then
    umount "/dev/$part" 2>/dev/null || true
  fi
done < <(lsblk -ln -o NAME,MOUNTPOINT "$DEVICE" | tail -n +2)

echo "Flashing image..."
dd if="$IMG_TO_WRITE" of="$DEVICE" bs=8M conv=fsync status=progress
sync
udevadm settle

if [[ "$DEVICE" =~ (mmcblk|nvme) ]]; then
  BOOT_PART="${DEVICE}1"
  if [[ ! -b "$BOOT_PART" ]]; then
    BOOT_PART="${DEVICE}p1"
  fi
else
  BOOT_PART="${DEVICE}1"
fi

if [[ ! -b "$BOOT_PART" ]]; then
  echo "Could not find boot partition for $DEVICE" >&2
  exit 1
fi

echo "Mounting boot partition: $BOOT_PART"
mount "$BOOT_PART" "$TMP_DIR/boot"

USER_DATA_OUT="$TMP_DIR/user-data"
META_DATA_OUT="$TMP_DIR/meta-data"
INSTANCE_ID="${HOSTNAME}-$(date +%Y%m%d%H%M%S)"

PROFILE_ROLE="pi5"
PROFILE_STORAGE="nvme"
PROFILE_PACKAGES="qemu-guest-agent curl git python3 python3-apt htop nvme-cli"
if [[ "$PROFILE" == "pibplus" ]]; then
  PROFILE_ROLE="pi-bplus"
  PROFILE_STORAGE="sdcard"
  PROFILE_PACKAGES="curl git python3 python3-apt htop"
fi

cat > "$USER_DATA_OUT" <<EOF
#cloud-config
hostname: ${HOSTNAME}
manage_etc_hosts: true

users:
  - default
  - name: ops
    gecos: Rack Operations
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - ${SSH_KEY_CONTENT}

package_update: true
package_upgrade: false
packages:
EOF

for pkg in $PROFILE_PACKAGES; do
  echo "  - ${pkg}" >> "$USER_DATA_OUT"
done

cat >> "$USER_DATA_OUT" <<EOF

write_files:
  - path: /etc/rack-profile
    permissions: "0644"
    content: |
      rack_role=${PROFILE_ROLE}
      storage=${PROFILE_STORAGE}
      provisioner=cloud-init

phone_home:
  url: "${CALLBACK_URL}"
  post: [instance_id, hostname, fqdn]
  tries: 15

runcmd:
  - [ systemctl, enable, ssh ]
  - [ systemctl, restart, ssh ]
  - [ sh, -c, "curl -fsS -X POST -H 'Content-Type: application/json' -d '{\"instance_id\":\"${INSTANCE_ID}\",\"hostname\":\"${HOSTNAME}\",\"profile\":\"${PROFILE}\",\"stage\":\"runcmd\"}' '${CALLBACK_URL}' || true" ]

final_message: "Cloud-init finished for ${HOSTNAME}"
EOF

cat > "$META_DATA_OUT" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: ${HOSTNAME}
EOF

echo "Injecting cloud-init files into boot partition"
cp "$USER_DATA_OUT" "$TMP_DIR/boot/user-data"
cp "$META_DATA_OUT" "$TMP_DIR/boot/meta-data"

if [[ -d "$TMP_DIR/boot/boot/firmware" ]]; then
  cp "$USER_DATA_OUT" "$TMP_DIR/boot/boot/firmware/user-data"
  cp "$META_DATA_OUT" "$TMP_DIR/boot/boot/firmware/meta-data"
fi

sync
umount "$TMP_DIR/boot"

echo "Done. SD card is ready for ${HOSTNAME} (${PROFILE})."
