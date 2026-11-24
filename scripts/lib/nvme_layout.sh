# Reserve a Longhorn partition on a Pi 5 NVMe boot disk.
# Sourced by flash.sh after the image is written and verified; shares its shell, so it is not run on its own.
#
# Pi OS grows root (partition 2) to the end of the disk on first boot, so a partition after root would make
# that resize fail. Root is therefore moved to the end of the disk instead, and partition 3, labelled longhorn,
# fills the space between the boot partition and root:
#
#   [1 boot][3 longhorn ........................][2 root, NVME_ROOT_GIB]
#
# The first-boot resize then has nothing to do, and rack_storage_disk mounts the longhorn partition.

NVME_ROOT_GIB="${CLOUD_INIT_NVME_ROOT_GIB:-64}"
NVME_MIN_LONGHORN_GIB=16
GIB_SECTORS=2097152
MBR_LAST_SECTOR=4294967295

if [[ ! "$NVME_ROOT_GIB" =~ ^[1-9][0-9]{1,4}$ ]] || (( NVME_ROOT_GIB < 16 )); then
  echo "NVMe root size must be a whole number of GiB, at least 16: $NVME_ROOT_GIB" >&2
  exit 1
fi
for nvme_cmd in sfdisk e2fsck resize2fs mkfs.ext4 blkid; do
  require_cmd "$nvme_cmd"
done

NVME_DEVICE_PATH="$(readlink -f -- "$DEVICE")"
if [[ "$NVME_DEVICE_PATH" == /dev/mmcblk* ]]; then
  echo "NVMe layout was requested, but $DEVICE is an SD card; refusing to split it." >&2
  exit 1
fi
if [[ "$(blockdev --getss "$DEVICE")" != "512" ]]; then
  echo "NVMe layout needs 512-byte logical sectors; $DEVICE reports $(blockdev --getss "$DEVICE")." >&2
  exit 1
fi

read -r NVME_LABEL_ID P1_START P1_SIZE P1_TYPE P2_START P2_SIZE < <(
  sfdisk --json "$IMG_TO_WRITE" | python3 -c '
import json, sys
table = json.load(sys.stdin)["partitiontable"]
parts = table.get("partitions", [])
if table.get("label") != "dos" or len(parts) != 2 or parts[1].get("type") != "83":
    sys.exit("image is not the usual two-partition Pi layout (boot, then a Linux root)")
p1, p2 = parts
print(table["id"], p1["start"], p1["size"], p1["type"], p2["start"], p2["size"])
'
)
if [[ -z "${P2_SIZE:-}" ]] || (( (P2_START + P2_SIZE) * 512 > IMAGE_BYTES )); then
  echo "Cannot read the image partition table; refusing to split $DEVICE." >&2
  exit 1
fi

DISK_SECTORS="$(blockdev --getsz "$DEVICE")"
LAST_SECTOR=$((DISK_SECTORS - 1))
if (( LAST_SECTOR > MBR_LAST_SECTOR )); then
  echo "Disk is larger than 2 TiB; an MBR (dos) table can only use the first 2 TiB."
  LAST_SECTOR=$MBR_LAST_SECTOR
fi
ROOT_START=$(( (LAST_SECTOR + 1 - NVME_ROOT_GIB * GIB_SECTORS) / 2048 * 2048 ))
ROOT_SIZE=$((LAST_SECTOR - ROOT_START + 1))
LONGHORN_START=$(( (P1_START + P1_SIZE + 2047) / 2048 * 2048 ))
LONGHORN_SIZE=$((ROOT_START - LONGHORN_START))

if (( ROOT_SIZE < P2_SIZE || LONGHORN_SIZE < NVME_MIN_LONGHORN_GIB * GIB_SECTORS )); then
  echo "Disk is too small for a ${NVME_ROOT_GIB} GiB root plus a ${NVME_MIN_LONGHORN_GIB} GiB Longhorn partition;"
  echo "keeping the standard layout. Longhorn will use a directory on the root filesystem instead."
  return 0
fi

nvme_part() {
  if [[ "$NVME_DEVICE_PATH" =~ (nvme|loop)[0-9]+(n[0-9]+)?$ ]]; then
    echo "${NVME_DEVICE_PATH}p$1"
  else
    echo "${NVME_DEVICE_PATH}$1"
  fi
}

echo "Moving root to the end of $DEVICE ($((ROOT_SIZE / GIB_SECTORS)) GiB) and reserving $((LONGHORN_SIZE / GIB_SECTORS)) GiB for Longhorn"
dd if="$IMG_TO_WRITE" of="$WRITE_DEVICE" bs=8M iflag=skip_bytes,count_bytes oflag=seek_bytes \
  skip=$((P2_START * 512)) seek=$((ROOT_START * 512)) count=$((P2_SIZE * 512)) conv=notrunc,fsync status=progress
blockdev --flushbufs "$WRITE_DEVICE"
if ! cmp -n $((P2_SIZE * 512)) -i "$((P2_START * 512)):$((ROOT_START * 512))" -- "$IMG_TO_WRITE" "$WRITE_DEVICE"; then
  echo "Root readback verification failed after moving it; card is not ready." >&2
  exit 1
fi

if [[ -n "${FLASH_EXPECTED_IDENTITY:-}" ]]; then
  python3 "$SCRIPT_DIR/../flash_helper/devices.py" "$DEVICE" "$FLASH_EXPECTED_IDENTITY"
fi
# Entries stay numbered 1, 2, 3 so root=PARTUUID=<id>-02 in cmdline.txt and fstab still point at root.
sfdisk --quiet --no-reread --wipe never --wipe-partitions never "$DEVICE" <<EOF
label: dos
label-id: $NVME_LABEL_ID
unit: sectors

start=$P1_START, size=$P1_SIZE, type=$P1_TYPE
start=$ROOT_START, size=$ROOT_SIZE, type=83
start=$LONGHORN_START, size=$LONGHORN_SIZE, type=83
EOF
if ! partprobe "$DEVICE"; then
  echo "Cannot refresh partition table on $DEVICE after the NVMe layout." >&2
  exit 1
fi
udevadm settle

ROOT_PART="$(nvme_part 2)"
LONGHORN_PART="$(nvme_part 3)"
e2fsck -fy "$ROOT_PART" || (( $? == 1 ))
resize2fs "$ROOT_PART"
mkfs.ext4 -F -q -L longhorn "$LONGHORN_PART"
udevadm settle
if [[ "$(blkid -o value -s LABEL "$LONGHORN_PART")" != "longhorn" ]]; then
  echo "Longhorn partition $LONGHORN_PART was not labelled; card is not ready." >&2
  exit 1
fi
echo "NVMe layout ready: root on $ROOT_PART, Longhorn on $LONGHORN_PART"
