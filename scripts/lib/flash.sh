# Confirm the target, write the image and mount the boot partition.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

if [[ -n "${FLASH_EXPECTED_IDENTITY:-}" ]]; then
  python3 "$SCRIPT_DIR/../flash_helper/devices.py" "$DEVICE" "$FLASH_EXPECTED_IDENTITY"
fi

if ! DEVICE_INFO="$(lsblk -dnbo TYPE,SIZE "$DEVICE")"; then
  echo "Cannot inspect target type and capacity: $DEVICE; refusing to overwrite it." >&2
  exit 1
fi
read -r DEVICE_TYPE DEVICE_BYTES <<< "$DEVICE_INFO"
IMAGE_BYTES="$(stat -c%s "$IMG_TO_WRITE")"
if [[ "$DEVICE_TYPE" != "disk" || ! "$DEVICE_BYTES" =~ ^[0-9]+$ ]]; then
  echo "Target must be a whole disk with a known capacity: $DEVICE" >&2
  exit 1
fi
if (( IMAGE_BYTES == 0 || IMAGE_BYTES > DEVICE_BYTES )); then
  echo "Image size ($IMAGE_BYTES bytes) is empty or exceeds target capacity ($DEVICE_BYTES bytes)." >&2
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
mounted_devices() {
  if ! lsblk --json --paths --output NAME,MOUNTPOINTS "$DEVICE" > "$TMP_DIR/target-mounts.json"; then
    echo "Cannot inspect mounts on $DEVICE; refusing to overwrite it." >&2
    return 1
  fi
  python3 - "$TMP_DIR/target-mounts.json" <<'PY'
import json
import sys

def mounted(node):
    mounts = node.get("mountpoints") or []
    if any(path in {"/", "/boot", "/boot/efi", "/home", "/usr", "/var", "[SWAP]"} for path in mounts):
        sys.exit("Target contains a system filesystem or swap; refusing to overwrite it.")
    for child in node.get("children", []):
        yield from mounted(child)
    if any(mounts):
        yield node["name"]

with open(sys.argv[1], encoding="utf-8") as source:
    snapshot = json.load(source)
for name in dict.fromkeys(name for node in snapshot["blockdevices"] for name in mounted(node)):
    print(name)
PY
}

MOUNTED_DEVICES="$(mounted_devices)"
while IFS= read -r mounted_device; do
  [[ -z "$mounted_device" ]] && continue
  if ! umount -- "$mounted_device"; then
    echo "Cannot unmount $mounted_device; refusing to overwrite $DEVICE. Close applications using the card and retry." >&2
    exit 1
  fi
done <<< "$MOUNTED_DEVICES"

MOUNTED_DEVICES="$(mounted_devices)"
if [[ -n "$MOUNTED_DEVICES" ]]; then
  echo "Target still has mounted filesystems; refusing to overwrite $DEVICE:" >&2
  printf '%s\n' "$MOUNTED_DEVICES" >&2
  exit 1
fi

echo "Flashing image..."
WRITE_DEVICE="$DEVICE"
if [[ -n "${FLASH_EXPECTED_IDENTITY:-}" ]]; then
  exec 7<>"$DEVICE"
  python3 "$SCRIPT_DIR/../flash_helper/devices.py" "$DEVICE" "$FLASH_EXPECTED_IDENTITY"
  WRITE_DEVICE="/proc/self/fd/7"
fi
dd if="$IMG_TO_WRITE" of="$WRITE_DEVICE" bs=8M conv=fsync status=progress
sync
echo "Verifying the entire written image from the card..."
blockdev --flushbufs "$DEVICE"
if ! cmp -n "$IMAGE_BYTES" -- "$IMG_TO_WRITE" "$DEVICE"; then
  echo "Image readback verification failed. Check the card, reader and USB connection; card is not ready." >&2
  exit 1
fi
if [[ -n "${FLASH_EXPECTED_IDENTITY:-}" ]]; then
  python3 "$SCRIPT_DIR/../flash_helper/devices.py" "$DEVICE" "$FLASH_EXPECTED_IDENTITY"
fi
if ! partprobe "$DEVICE"; then
  echo "Cannot refresh partition table on $DEVICE; stopping before cloud-init injection." >&2
  exit 1
fi
udevadm settle

if [[ "${FLASH_MEDIA_ONLY:-false}" == "true" ]]; then
  return 0
fi

DEVICE_PATH="$(readlink -f -- "$DEVICE")"
if [[ "$DEVICE_PATH" =~ (mmcblk|nvme) ]]; then
  BOOT_PART="${DEVICE_PATH}p1"
else
  BOOT_PART="${DEVICE_PATH}1"
fi

if [[ ! -b "$BOOT_PART" ]]; then
  echo "Could not find boot partition for $DEVICE" >&2
  exit 1
fi

echo "Mounting boot partition: $BOOT_PART"
mount "$BOOT_PART" "$TMP_DIR/boot"
if [[ -n "${FLASH_EXPECTED_IDENTITY:-}" ]]; then
  python3 "$SCRIPT_DIR/../flash_helper/devices.py" "$DEVICE" "$FLASH_EXPECTED_IDENTITY"
fi
