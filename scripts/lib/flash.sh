# Confirm the target, write the image and mount the boot partition.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

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
