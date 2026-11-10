# Seed a generic Ubuntu cloud image, then flash it to a disk or write image files.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

# NoCloud reads its seed from the root filesystem, so the seed travels inside the image
# and the full device readback in flash.sh covers it too.
SEED_DIR=/var/lib/cloud/seed/nocloud
mkdir -p "$TMP_DIR/root"
LOOP_DEV="$(losetup --find --show --partscan "$IMG_TO_WRITE")"
udevadm settle || true
ROOT_PART=""
for part in "${LOOP_DEV}"p*; do
  [[ -b "$part" ]] || continue
  if [[ "$(blkid -o value -s LABEL "$part" 2>/dev/null || true)" == "cloudimg-rootfs" ]]; then
    ROOT_PART="$part"
    break
  fi
done
if [[ -z "$ROOT_PART" ]]; then
  echo "No cloudimg-rootfs partition found; playbook-server needs an Ubuntu generic cloud image." >&2
  exit 1
fi

echo "Injecting cloud-init seed into ${SEED_DIR} on the image root filesystem"
mount "$ROOT_PART" "$TMP_DIR/root"
install -d -m 0755 "$TMP_DIR/root$SEED_DIR"
install -m 0600 "$USER_DATA_OUT" "$TMP_DIR/root$SEED_DIR/user-data"
install -m 0600 "$META_DATA_OUT" "$TMP_DIR/root$SEED_DIR/meta-data"
sync
umount "$TMP_DIR/root"
mount -o ro "$ROOT_PART" "$TMP_DIR/root"
for seed_file in user-data meta-data; do
  if ! cmp -- "$TMP_DIR/$seed_file" "$TMP_DIR/root$SEED_DIR/$seed_file"; then
    echo "Cloud-init seed readback failed in the image: $SEED_DIR/$seed_file" >&2
    exit 1
  fi
done
umount "$TMP_DIR/root"
losetup -d "$LOOP_DEV"
LOOP_DEV=""

if [[ -n "$DEVICE" ]]; then
  # shellcheck source=flash.sh
  source "$SCRIPT_DIR/lib/flash.sh"
  echo "Done. ${DEVICE} is ready for ${HOSTNAME} (${PROFILE}, ${SERVER_ARCH}); boot it in UEFI mode."
  return 0
fi

mkdir -p "$OUTPUT_DIR"
ARTIFACT_NAME="${HOSTNAME}-ubuntu-24.04-${SERVER_ARCH}"
ARTIFACT_BASE="$OUTPUT_DIR/$ARTIFACT_NAME"
ARTIFACTS=()
IFS=',' read -r -a server_formats <<< "$SERVER_FORMATS"
for server_format in "${server_formats[@]}"; do
  case "$server_format" in
    raw)
      echo "Writing ${ARTIFACT_NAME}.img"
      cp --sparse=always "$IMG_TO_WRITE" "$ARTIFACT_BASE.img.part"
      mv "$ARTIFACT_BASE.img.part" "$ARTIFACT_BASE.img"
      ARTIFACTS+=("$ARTIFACT_NAME.img")
      ;;
    raw.xz)
      require_cmd xz
      echo "Compressing ${ARTIFACT_NAME}.img.xz"
      xz -T0 -6 -c "$IMG_TO_WRITE" > "$ARTIFACT_BASE.img.xz.part"
      mv "$ARTIFACT_BASE.img.xz.part" "$ARTIFACT_BASE.img.xz"
      ARTIFACTS+=("$ARTIFACT_NAME.img.xz")
      ;;
    qcow2)
      echo "Writing ${ARTIFACT_NAME}.qcow2 (virtual size ${SERVER_DISK_SIZE})"
      qemu-img convert -f raw -O qcow2 -c "$IMG_TO_WRITE" "$ARTIFACT_BASE.qcow2.part"
      qemu-img resize -q -f qcow2 "$ARTIFACT_BASE.qcow2.part" "$SERVER_DISK_SIZE"
      mv "$ARTIFACT_BASE.qcow2.part" "$ARTIFACT_BASE.qcow2"
      ARTIFACTS+=("$ARTIFACT_NAME.qcow2")
      ;;
  esac
done

SERVER_SETTINGS_JSON="$(server_render settings)"
BUILDER_COMMIT="$(git -c safe.directory="*" -C "$SCRIPT_DIR/.." rev-parse HEAD 2>/dev/null || true)"
SOURCE_SHA256="$(sha256sum "$SOURCE_IMAGE" | awk '{print $1}')"
MANIFEST_PROFILE="$PROFILE" MANIFEST_HOSTNAME="$HOSTNAME" MANIFEST_ARCH="$SERVER_ARCH" \
MANIFEST_IMAGE_URL="$IMAGE_URL" MANIFEST_IMAGE_FILE="$(basename "$SOURCE_IMAGE")" MANIFEST_IMAGE_SHA256="$SOURCE_SHA256" \
MANIFEST_CALLBACK_URL="$CALLBACK_URL" MANIFEST_BUILDER_COMMIT="$BUILDER_COMMIT" MANIFEST_SETTINGS="$SERVER_SETTINGS_JSON" \
python3 - "$ARTIFACT_BASE.json" "${ARTIFACTS[@]}" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

target, *artifacts = sys.argv[1:]
env = os.environ
settings = json.loads(env["MANIFEST_SETTINGS"])
manifest = {
    "profile": env["MANIFEST_PROFILE"],
    "hostname": env["MANIFEST_HOSTNAME"],
    "arch": env["MANIFEST_ARCH"],
    "os": "Ubuntu 24.04 LTS",
    "source_image": {"url": env["MANIFEST_IMAGE_URL"], "file": env["MANIFEST_IMAGE_FILE"], "sha256": env["MANIFEST_IMAGE_SHA256"]},
    "repository": settings["server_repo_url"],
    "ref": settings["server_ref"],
    "port": int(settings["server_port"]),
    "host_flasher": settings["server_install_flasher"] == "true",
    "callback_url": env["MANIFEST_CALLBACK_URL"],
    "builder_commit": env["MANIFEST_BUILDER_COMMIT"],
    "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "artifacts": artifacts,
}
with open(target, "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2)
    handle.write("\n")
PY

(
  cd "$OUTPUT_DIR"
  find . -maxdepth 1 -type f \( -name '*.img' -o -name '*.img.xz' -o -name '*.qcow2' \) -printf '%f\n' | sort | xargs -r sha256sum -- > SHA256SUMS.part
  mv SHA256SUMS.part SHA256SUMS
)
if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
  chown "$SUDO_UID:$SUDO_GID" "$OUTPUT_DIR" "$OUTPUT_DIR/SHA256SUMS" "$ARTIFACT_BASE.json"
  for artifact in "${ARTIFACTS[@]}"; do
    chown "$SUDO_UID:$SUDO_GID" "$OUTPUT_DIR/$artifact"
  done
fi

echo "Done. Image files for ${HOSTNAME} (${PROFILE}, ${SERVER_ARCH}) in ${OUTPUT_DIR}:"
for artifact in "${ARTIFACTS[@]}" "$ARTIFACT_NAME.json" SHA256SUMS; do
  echo "  $artifact"
done
