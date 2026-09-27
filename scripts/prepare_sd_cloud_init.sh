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

if [[ "$PROFILE" != "pi5" && "$PROFILE" != "pibplus" ]]; then
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
require_cmd mktemp
require_cmd sha256sum
require_cmd python3
require_cmd openssl

if [[ -z "$IMAGE_FILE" ]]; then
  if [[ -z "$IMAGE_URL" && "$PROFILE" == "pi5" ]]; then
    IMAGE_URL="https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz"
  fi

  if [[ -z "$IMAGE_URL" ]]; then
    echo "Provide --image-url or --image-file for profile: $PROFILE" >&2
    exit 1
  fi
  validate_http_url "--image-url" "$IMAGE_URL"
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
CLOUD_INIT_USERNAME="${CLOUD_INIT_USERNAME:-jdizzle}"
CLOUD_INIT_USER_PASSWORD="${CLOUD_INIT_USER_PASSWORD:-}"
CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH="${CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH:-false}"
CLOUD_INIT_DISABLE_ROOT_SSH="${CLOUD_INIT_DISABLE_ROOT_SSH:-false}"
CLOUD_INIT_BOOT_SUMMARY="${CLOUD_INIT_BOOT_SUMMARY:-true}"
CLOUD_INIT_ENABLE_FAIL2BAN="${CLOUD_INIT_ENABLE_FAIL2BAN:-false}"
CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES="${CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES:-false}"
CLOUD_INIT_ENABLE_HARDWARE_CHECKS="${CLOUD_INIT_ENABLE_HARDWARE_CHECKS:-false}"
CLOUD_INIT_FANCY_MOTD="${CLOUD_INIT_FANCY_MOTD:-false}"
CLOUD_INIT_INSTALL_TELEGRAF="${CLOUD_INIT_INSTALL_TELEGRAF:-false}"
CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP="${CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP:-false}"
CLOUD_INIT_SSH_PORT="${CLOUD_INIT_SSH_PORT:-}"
CLOUD_INIT_SSH_ALLOW_GROUPS="${CLOUD_INIT_SSH_ALLOW_GROUPS:-}"
CLOUD_INIT_EXTRA_SSH_KEYS="${CLOUD_INIT_EXTRA_SSH_KEYS:-}"

if [[ ! "$CLOUD_INIT_USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  echo "Invalid CLOUD_INIT_USERNAME value: ${CLOUD_INIT_USERNAME}" >&2
  echo "Allowed: lowercase letters, numbers, '_' or '-' (must start with a letter or underscore)." >&2
  exit 1
fi

if [[ -n "$CLOUD_INIT_SSH_PORT" ]]; then
  if [[ ! "$CLOUD_INIT_SSH_PORT" =~ ^[0-9]+$ ]] || (( CLOUD_INIT_SSH_PORT < 1 || CLOUD_INIT_SSH_PORT > 65535 )); then
    echo "Invalid CLOUD_INIT_SSH_PORT value: ${CLOUD_INIT_SSH_PORT}" >&2
    exit 1
  fi
fi

if [[ -n "$CLOUD_INIT_SSH_ALLOW_GROUPS" ]]; then
  if [[ ! "$CLOUD_INIT_SSH_ALLOW_GROUPS" =~ ^[A-Za-z0-9_,.-]+([[:space:]]*[A-Za-z0-9_,.-]+)*$ ]]; then
    echo "Invalid CLOUD_INIT_SSH_ALLOW_GROUPS value: ${CLOUD_INIT_SSH_ALLOW_GROUPS}" >&2
    exit 1
  fi
fi

if [[ -n "$CLOUD_INIT_EXTRA_SSH_KEYS" ]]; then
  validate_ssh_public_key_lines "CLOUD_INIT_EXTRA_SSH_KEYS" "$CLOUD_INIT_EXTRA_SSH_KEYS"
fi

USER_PASSWORD_HASH=""
if [[ -n "$CLOUD_INIT_USER_PASSWORD" ]]; then
  USER_PASSWORD_HASH="$(printf '%s' "$CLOUD_INIT_USER_PASSWORD" | openssl passwd -6 -stdin)"
fi

PROFILE_ROLE="pi5"
PROFILE_STORAGE="nvme"
PROFILE_PACKAGES="qemu-guest-agent curl git python3 python3-apt htop nvme-cli lshw"
if [[ "$PROFILE" == "pibplus" ]]; then
  PROFILE_ROLE="pi-bplus"
  PROFILE_STORAGE="sdcard"
  PROFILE_PACKAGES="curl git python3 python3-apt htop lshw"
fi

cat > "$USER_DATA_OUT" <<EOF
#cloud-config
hostname: ${HOSTNAME}
manage_etc_hosts: true

users:
  - default
  - name: ${CLOUD_INIT_USERNAME}
    gecos: Rack Operations
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    ssh_authorized_keys:
      - ${SSH_KEY_CONTENT}
EOF

if [[ -n "$USER_PASSWORD_HASH" ]]; then
  cat >> "$USER_DATA_OUT" <<EOF
    lock_passwd: false
    passwd: ${USER_PASSWORD_HASH}
EOF
fi

if [[ "$CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'EOF'
ssh_pwauth: false
EOF
elif [[ -n "$USER_PASSWORD_HASH" ]]; then
  cat >> "$USER_DATA_OUT" <<'EOF'
ssh_pwauth: true
EOF
fi

cat >> "$USER_DATA_OUT" <<EOF

package_update: true
package_upgrade: false
packages:
EOF

for pkg in $PROFILE_PACKAGES; do
  echo "  - ${pkg}" >> "$USER_DATA_OUT"
done

if [[ "$CLOUD_INIT_ENABLE_FAIL2BAN" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'SEC_PKGS'
  - fail2ban
SEC_PKGS
fi

if [[ "$CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'SEC_PKGS'
  - unattended-upgrades
SEC_PKGS
fi

if [[ "${CLOUD_INIT_FANCY_MOTD:-false}" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'FANCY_MOTD_PKGS'
  - fortune-mod
  - cowsay
  - lolcat
FANCY_MOTD_PKGS
fi

# Add Telegraf package if enabled
if [[ "${CLOUD_INIT_INSTALL_TELEGRAF:-false}" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'TELEGRAF_PKGS'
  - telegraf
TELEGRAF_PKGS
fi

cat >> "$USER_DATA_OUT" <<EOF

write_files:
  - path: /etc/rack-profile
    permissions: "0644"
    content: |
      rack_role=${PROFILE_ROLE}
      storage=${PROFILE_STORAGE}
      provisioner=cloud-init
EOF

if [[ "$DISTRIBUTION" == "k3s" ]]; then
  SANITIZED_K3S_CONFIG="$(printf '%s\n' "$DEFAULT_K3S_CONFIG" | awk -v hostname="${HOSTNAME}" '
    BEGIN { seen_node_name = 0 }
    /^[[:space:]]*#/ { print; next }
    tolower($0) ~ /^[[:space:]]*node-name[[:space:]]*:/ {
      if (!seen_node_name) {
        printf "node-name: %s\n", hostname
        seen_node_name = 1
      }
      next
    }
    tolower($0) ~ /^[[:space:]]*node-ip[[:space:]]*:/ { next }
    { print }
  ' )"
  cat >> "$USER_DATA_OUT" <<K3S_FILE
  - path: /etc/rancher/k3s/config.yaml
    permissions: "0644"
    content: |
$(printf '%s\n' "$SANITIZED_K3S_CONFIG" | sed 's/^/      /')
K3S_FILE
fi

if [[ "$CLOUD_INIT_DISABLE_ROOT_SSH" == "true" || "$CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH" == "true" || -n "$CLOUD_INIT_SSH_PORT" || -n "$CLOUD_INIT_SSH_ALLOW_GROUPS" ]]; then
  cat >> "$USER_DATA_OUT" <<SSH_HARDEN_FILE
  - path: /etc/ssh/sshd_config.d/99-rack-hardening.conf
    permissions: "0644"
    content: |
      # Managed by rack cloud-init option defaults.
SSH_HARDEN_FILE
  if [[ "$CLOUD_INIT_DISABLE_ROOT_SSH" == "true" ]]; then
    echo "      PermitRootLogin no" >> "$USER_DATA_OUT"
  fi
  if [[ "$CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH" == "true" ]]; then
    echo "      PasswordAuthentication no" >> "$USER_DATA_OUT"
    echo "      KbdInteractiveAuthentication no" >> "$USER_DATA_OUT"
  fi
  if [[ -n "$CLOUD_INIT_SSH_PORT" ]]; then
    echo "      Port ${CLOUD_INIT_SSH_PORT}" >> "$USER_DATA_OUT"
  fi
  if [[ -n "$CLOUD_INIT_SSH_ALLOW_GROUPS" ]]; then
    echo "      AllowGroups ${CLOUD_INIT_SSH_ALLOW_GROUPS}" >> "$USER_DATA_OUT"
  fi
fi

if [[ "$CLOUD_INIT_ENABLE_FAIL2BAN" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<FAIL2BAN_CONF
  - path: /etc/fail2ban/jail.d/rack-sshd.local
    permissions: "0644"
    content: |
      [sshd]
      enabled = true
      backend = systemd
      port = ${CLOUD_INIT_SSH_PORT:-ssh}
      maxretry = 6
      findtime = 10m
      bantime = 1h
FAIL2BAN_CONF
fi

if [[ -n "$CLOUD_INIT_EXTRA_SSH_KEYS" ]]; then
  cat >> "$USER_DATA_OUT" <<EXTRA_KEYS
  - path: /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys
    permissions: "0600"
    owner: ${CLOUD_INIT_USERNAME}:${CLOUD_INIT_USERNAME}
    content: |
$(printf '%s\n' "$CLOUD_INIT_EXTRA_SSH_KEYS" | sed 's/^/      /')
EXTRA_KEYS
fi

cat >> "$USER_DATA_OUT" <<EOF
  - path: /usr/local/bin/rack-local-ip
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      ip -4 -o addr show scope global up 2>/dev/null | awk '
        {
          split(\$4, a, "/");
          if (a[1] != "" && a[1] !~ /^169\.254\./) {
            print a[1];
            exit
          }
        }
      '
      if [[ -z "\$(ip -4 -o addr show scope global up 2>/dev/null | awk '{split(\$4, a, "/"); if (a[1] != "" && a[1] !~ /^169\\.254\\./) {print a[1]; exit}}')" ]]; then
        hostname -I 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if (\$i !~ /^127\\./ && \$i !~ /^169\\.254\\./) {print \$i; exit}}'
      fi
  - path: /usr/local/bin/rack-hardware-check
    permissions: "0755"
    content: |
      #!/bin/bash
      # Rack hardware detection and reporting
      PI_MODEL="\$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0' || echo 'unknown')"
      RAM_MB="\$(awk '/MemTotal/ {print int(\$2/1024)}' /proc/meminfo 2>/dev/null || echo '0')"
      SD_CAPACITY="\$(lsblk -b --output SIZE -n /dev/mmcblk0 2>/dev/null | awk '{print int(\$1/1024/1024/1024)}' || echo '0')"
      NVME_PRESENT="false"
      [[ -b /dev/nvme0n1 ]] && NVME_PRESENT="true"
      HAT_PRESENT="false"
      i2cdetect -y 1 2>/dev/null | grep -q "50\\|51" && HAT_PRESENT="true"
      POE_ENABLED="false"
      [[ -f /sys/class/pwm/pwmchip0/device/dtoverlay ]] && POE_ENABLED="true"
      SOURCE_IP="\$(/usr/local/bin/rack-local-ip 2>/dev/null || true)"
      NODE_REPORT_B64="\$(/usr/local/bin/rack-node-report 2>/dev/null | base64 -w0 || true)"
      PAYLOAD_JSON="\$(PI_MODEL=\"\$PI_MODEL\" RAM_MB=\"\$RAM_MB\" SD_CAPACITY=\"\$SD_CAPACITY\" NVME_PRESENT=\"\$NVME_PRESENT\" HAT_PRESENT=\"\$HAT_PRESENT\" POE_ENABLED=\"\$POE_ENABLED\" SOURCE_IP=\"\$SOURCE_IP\" NODE_REPORT_B64=\"\$NODE_REPORT_B64\" PROFILE='${PROFILE}' python3 <<'PY'
      import json
      import os
      import socket


      def as_int(value: str) -> int:
        try:
          return int(float(str(value or "0").strip()))
        except (TypeError, ValueError):
          return 0


      def as_bool(value: str) -> bool:
        return str(value or "").strip().lower() == "true"


      payload = {
        "instance_id": f"{socket.gethostname()}-hw",
        "hostname": socket.gethostname(),
        "profile": os.getenv("PROFILE", "pi5"),
        "source_ip": os.getenv("SOURCE_IP", ""),
        "stage": "hardware-check",
        "node_report_b64": os.getenv("NODE_REPORT_B64", ""),
        "hardware": {
          "model": os.getenv("PI_MODEL", "unknown"),
          "ram_mb": as_int(os.getenv("RAM_MB", "0")),
          "sd_capacity_gb": as_int(os.getenv("SD_CAPACITY", "0")),
          "nvme": as_bool(os.getenv("NVME_PRESENT", "false")),
          "hat": as_bool(os.getenv("HAT_PRESENT", "false")),
          "poe": as_bool(os.getenv("POE_ENABLED", "false")),
        },
      }

      print(json.dumps(payload, separators=(",", ":")))
      PY
      )"
      curl -fsS -X POST -H 'Content-Type: application/json' \\
        --data-binary "\$PAYLOAD_JSON" \
        '${CALLBACK_URL}' || true
  - path: /usr/local/bin/rack-callback-payload
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      python3 - <<'PY'
      import json
      import os

      payload = {
        "instance_id": os.environ.get("INSTANCE_ID", ""),
        "hostname": os.environ.get("HOSTNAME", ""),
        "profile": os.environ.get("PROFILE", ""),
        "source_ip": os.environ.get("SOURCE_IP", ""),
        "stage": os.environ.get("STAGE", "runcmd"),
        "node_report_b64": os.environ.get("NODE_REPORT_B64", ""),
      }
      print(json.dumps(payload, separators=(",", ":")))
      PY
  - path: /usr/local/bin/rack-power-restored-callback
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      SOURCE_IP="$(/usr/local/bin/rack-local-ip 2>/dev/null || true)"
      NODE_REPORT_B64="$(/usr/local/bin/rack-node-report 2>/dev/null | base64 -w0 || true)"
      PAYLOAD_JSON="$(INSTANCE_ID="${INSTANCE_ID:-$(hostname)}" HOSTNAME="${HOSTNAME:-$(hostname)}" PROFILE="${PROFILE:-}" SOURCE_IP="${SOURCE_IP}" NODE_REPORT_B64="${NODE_REPORT_B64}" STAGE="power_restore" /usr/local/bin/rack-callback-payload)"
      if [[ -n "${CALLBACK_URL:-}" ]]; then
        curl --retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors --connect-timeout 5 --max-time 20 -fsS -X POST -H "Content-Type: application/json" -d "${PAYLOAD_JSON}" "${CALLBACK_URL}" || true
      fi
  - path: /etc/systemd/system/rack-power-restored-callback.service
    permissions: "0644"
    content: |
      [Unit]
      Description=Rack node callback after power restore
      Wants=network-online.target
      After=network-online.target

      [Service]
      Type=oneshot
      Environment="CALLBACK_URL=${CALLBACK_URL}"
      Environment="PROFILE=${PROFILE}"
      Environment="INSTANCE_ID=${INSTANCE_ID}"
      ExecStart=/usr/local/bin/rack-power-restored-callback

      [Install]
      WantedBy=multi-user.target
  - path: /usr/local/bin/rack-node-report
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      python3 <<'PY'
      import json
      import subprocess
      from datetime import datetime, timezone


      def run_text(cmd):
        try:
          out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
          return out.strip()
        except Exception:
          return ""


      def run_json(cmd):
        text = run_text(cmd)
        if not text:
          return {}
        try:
          return json.loads(text)
        except json.JSONDecodeError:
          return {"raw": text[:20000]}


      report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hostname": run_text(["hostname"]),
        "kernel": run_text(["uname", "-a"]),
        "os_release": run_text(["bash", "-lc", "cat /etc/os-release 2>/dev/null"]),
        "cpu": run_text(["bash", "-lc", "lscpu 2>/dev/null"]),
        "memory": run_text(["bash", "-lc", "free -h 2>/dev/null"]),
        "storage": run_text(["bash", "-lc", "lsblk -e7 -o NAME,SIZE,TYPE,MODEL,MOUNTPOINT 2>/dev/null"]),
        "network": run_text(["bash", "-lc", "ip -brief address 2>/dev/null"]),
        "lshw": run_json(["bash", "-lc", "lshw -json 2>/dev/null"]),
      }
      print(json.dumps(report, separators=(",", ":")))
      PY
  - path: /usr/local/bin/rack-bootstrap-summary
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail

      if [[ "${CLOUD_INIT_BOOT_SUMMARY}" != "true" ]]; then
        exit 0
      fi

      reset=$'\033[0m'
      bold=$'\033[1m'
      red=$'\033[31m'
      yellow=$'\033[33m'
      green=$'\033[32m'
      cyan=$'\033[36m'
      magenta=$'\033[35m'

      print_border() {
        printf '%s+--------------------------------------------------------------+%s\n' "$cyan" "$reset"
      }

      print_row() {
        local color=$1
        local key=$2
        local value=$3
        printf '%s|%s %-24s %s| %s%-28s%s %s|%s\n' "$color" "$reset" "$key" "$color" "$bold" "$value" "$reset" "$color" "$reset"
      }

      print_border
      printf '%s|%s %-58s %s|%s\n' "$red" "$reset" "$bold RACK BOOT COMPLETE$reset" "$red" "$reset"
      print_border
      print_row "$red" "Hostname" "$(hostname)"
      print_row "$yellow" "Profile" "${PROFILE}"
      print_row "$green" "Role" "${PROFILE_ROLE}"
      print_row "$cyan" "Storage" "${PROFILE_STORAGE}"
      print_row "$magenta" "Distribution" "${DISTRIBUTION}"
      print_row "$red" "User" "${CLOUD_INIT_USERNAME}"
      print_row "$yellow" "SSH password auth" "${CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH}"
      print_row "$green" "Root SSH login" "${CLOUD_INIT_DISABLE_ROOT_SSH}"
      print_row "$cyan" "Boot summary" "${CLOUD_INIT_BOOT_SUMMARY}"
      print_row "$magenta" "Fail2ban" "${CLOUD_INIT_ENABLE_FAIL2BAN}"
      print_row "$red" "Unattended upgrades" "${CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES}"
      print_row "$yellow" "Hardware checks" "${CLOUD_INIT_ENABLE_HARDWARE_CHECKS:-false}"
      print_row "$green" "Fancy MOTD" "${CLOUD_INIT_FANCY_MOTD:-false}"
      print_row "$cyan" "Telegraf" "${CLOUD_INIT_INSTALL_TELEGRAF:-false}"
      k3s_config_state="not set"
      if [[ -n "${CLOUD_INIT_K3S_CONFIG:-}" ]]; then
        k3s_config_state="present"
      fi
      print_row "$magenta" "K3s config" "${k3s_config_state}"
      print_row "$red" "SSH port" "${CLOUD_INIT_SSH_PORT:-22}"
      print_row "$yellow" "SSH allow groups" "${CLOUD_INIT_SSH_ALLOW_GROUPS:-default}"
      print_row "$green" "Callback URL" "${CALLBACK_URL}"
      print_border
      printf '%s%sAll configured steps finished.%s\n' "$bold" "$green" "$reset"
      printf '%s%sYou can now continue from the portal.%s\n' "$bold" "$cyan" "$reset"
  - path: /etc/update-motd.d/10-fancy
    permissions: "0755"
    content: |
      #!/bin/bash
      if [[ "${CLOUD_INIT_FANCY_MOTD:-false}" != "true" ]]; then
        exit 0
      fi
      if command -v fortune >/dev/null 2>&1 && command -v cowsay >/dev/null 2>&1; then
        if command -v lolcat >/dev/null 2>&1; then
          fortune | cowsay | lolcat 2>/dev/null || fortune | cowsay
        else
          fortune | cowsay
        fi
      else
        echo "Rack node online: $(hostname)"
      fi
  - path: /usr/local/bin/rack-telegraf-bootstrap
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail

      INFLUX_URL="\${CLOUD_INIT_TELEGRAF_INFLUX_URL:-}"
      INFLUX_TOKEN="\${CLOUD_INIT_TELEGRAF_INFLUX_TOKEN:-}"
      INFLUX_ORG="\${CLOUD_INIT_TELEGRAF_INFLUX_ORG:-}"
      INFLUX_BUCKET="\${CLOUD_INIT_TELEGRAF_INFLUX_BUCKET:-}"
      CONFIG_URL="\${CLOUD_INIT_TELEGRAF_CONFIG_URL:-}"

      mkdir -p /etc/telegraf

      if [[ -n "\$CONFIG_URL" ]]; then
        if [[ -n "\$INFLUX_TOKEN" ]]; then
          curl -fsSL -H "Authorization: Token \$INFLUX_TOKEN" "\$CONFIG_URL" -o /etc/telegraf/telegraf.conf
        else
          curl -fsSL "\$CONFIG_URL" -o /etc/telegraf/telegraf.conf
        fi
      elif [[ -n "\$INFLUX_URL" && -n "\$INFLUX_TOKEN" && -n "\$INFLUX_ORG" && -n "\$INFLUX_BUCKET" ]]; then
        cat > /etc/telegraf/telegraf.conf <<TELEGRAF_CONF
        [agent]
          interval = "10s"
          round_interval = true
          metric_batch_size = 1000
          metric_buffer_limit = 10000
          flush_interval = "10s"
          hostname = "\$(hostname)"

        [[outputs.influxdb_v2]]
          urls = ["\$INFLUX_URL"]
          token = "\$INFLUX_TOKEN"
          organization = "\$INFLUX_ORG"
          bucket = "\$INFLUX_BUCKET"

        [[inputs.cpu]]
          percpu = true
          totalcpu = true

        [[inputs.mem]]
        [[inputs.disk]]
          ignore_fs = ["tmpfs", "devtmpfs", "overlay", "squashfs"]
        [[inputs.diskio]]
        [[inputs.net]]
        [[inputs.system]]
        TELEGRAF_CONF
      else
        echo "Telegraf enabled but no config_url or complete Influx credentials provided; skipping config"
        exit 0
      fi

      systemctl enable telegraf || true
      systemctl restart telegraf || true

phone_home:
  url: "${CALLBACK_URL}"
  post: [instance_id, hostname, fqdn]
  tries: 15

runcmd:
  - [ systemctl, enable, ssh ]
  - [ systemctl, restart, ssh ]
EOF

if [[ "$DISTRIBUTION" == "k3s" ]]; then
  if [[ -z "${CLOUD_INIT_K3S_CONFIG:-}" ]]; then
    DEFAULT_K3S_CONFIG="# pragma: k3s-config-v1
server: https://${DEFAULT_K3S_SERVER_HOST}:6443
node-name: ${HOSTNAME}
"
  fi

  K3S_INSTALL_MODE="server"
  if printf '%s\n' "$DEFAULT_K3S_CONFIG" | grep -Eq 'role:\s*"?worker"?|role=worker|node-role\.kubernetes\.io/worker'; then
    K3S_INSTALL_MODE="agent"
  fi

  if [[ "$K3S_INSTALL_MODE" == "agent" ]]; then
    cat >> "$USER_DATA_OUT" <<'K3S_RUNCMD'
  - [ sh, -c, 'mkdir -p /etc/rancher/k3s && curl -fsSL https://get.k3s.io | INSTALL_K3S_EXEC="agent --with-node-id --config /etc/rancher/k3s/config.yaml" sh -' ]
K3S_RUNCMD
  else
  cat >> "$USER_DATA_OUT" <<'K3S_RUNCMD'
  - [ sh, -c, 'mkdir -p /etc/rancher/k3s && curl -fsSL https://get.k3s.io | INSTALL_K3S_EXEC="server --with-node-id --config /etc/rancher/k3s/config.yaml" sh -' ]
K3S_RUNCMD
  fi
fi

# Conditionally append runcmd items based on cloud-init options
if [[ "${CLOUD_INIT_ENABLE_HARDWARE_CHECKS:-false}" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ /usr/local/bin/rack-hardware-check ]
RUNCMD_EOF
fi

if [[ "${CLOUD_INIT_INSTALL_TELEGRAF:-false}" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ /usr/local/bin/rack-telegraf-bootstrap ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_ENABLE_FAIL2BAN" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'if systemctl list-unit-files | grep -q "^fail2ban.service"; then systemctl enable --now fail2ban || true; fi' ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive unattended-upgrades || true' ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_BOOT_SUMMARY" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ /usr/local/bin/rack-bootstrap-summary ]
RUNCMD_EOF
fi

if [[ -n "$CLOUD_INIT_EXTRA_SSH_KEYS" ]]; then
  cat >> "$USER_DATA_OUT" <<EXTRA_KEYS_RUNCMD
  - [ sh, -c, 'if [ -f /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys ]; then cat /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys >> /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys; chown ${CLOUD_INIT_USERNAME}:${CLOUD_INIT_USERNAME} /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys; chmod 600 /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys; fi' ]
EXTRA_KEYS_RUNCMD
fi

# Append final runcmd callback using a runtime payload generator so all values are expanded on the node, not during host-side template generation.
cat >> "$USER_DATA_OUT" <<RUNCMD_EOF
  - [ sh, -c, 'SOURCE_IP="$(/usr/local/bin/rack-local-ip 2>/dev/null || true)"; NODE_REPORT_B64="$(/usr/local/bin/rack-node-report 2>/dev/null | base64 -w0 || true)"; PAYLOAD_JSON="$(INSTANCE_ID="\${INSTANCE_ID}" HOSTNAME="\${HOSTNAME}" PROFILE="\${PROFILE}" SOURCE_IP="\${SOURCE_IP}" NODE_REPORT_B64="\${NODE_REPORT_B64}" /usr/local/bin/rack-callback-payload)"; if ! curl --retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors --connect-timeout 5 --max-time 20 -fsS -X POST -H "Content-Type: application/json" -d "\${PAYLOAD_JSON}" "${CALLBACK_URL}"; then echo "rack callback POST failed after retries to ${CALLBACK_URL}" >&2; fi' ]

RUNCMD_EOF

if [[ "$CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'sleep 2; reboot' ]
RUNCMD_EOF
fi

cat >> "$USER_DATA_OUT" <<RUNCMD_EOF
  - [ sh, -c, 'systemctl daemon-reload && systemctl enable --now rack-power-restored-callback.service || true' ]
RUNCMD_EOF

cat >> "$USER_DATA_OUT" <<RUNCMD_EOF

final_message: "Cloud-init finished for ${HOSTNAME}"
RUNCMD_EOF

cat > "$META_DATA_OUT" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: ${HOSTNAME}
EOF

if [[ "$VALIDATE_CLOUD_INIT_SCHEMA" == "true" ]]; then
  if ! command -v cloud-init >/dev/null 2>&1; then
    echo "cloud-init CLI is required for schema validation but was not found on this host." >&2
    echo "Install cloud-init or rerun with CLOUD_INIT_VALIDATE_SCHEMA=false to bypass validation." >&2
    exit 1
  fi

  echo "Validating generated cloud-init user-data schema"
  if ! cloud-init schema --config-file "$USER_DATA_OUT"; then
    echo "cloud-init schema command failed; trying legacy 'cloud-init devel schema'" >&2
    if ! cloud-init devel schema --config-file "$USER_DATA_OUT"; then
      echo "cloud-init schema validation failed for generated user-data: $USER_DATA_OUT" >&2
      exit 1
    fi
  fi
fi

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
