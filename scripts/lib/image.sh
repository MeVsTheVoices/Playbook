# Find, verify and decompress the OS image.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

if [[ -z "$IMAGE_FILE" ]]; then
  if [[ -z "$IMAGE_URL" && "$PROFILE" == "network-toolkit" ]]; then
    IMAGE_URL="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/2026-10-06-raspios-trixie-arm64-lite.img.xz"
    IMAGE_SHA256="${IMAGE_SHA256:-483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843}"
  fi
  if [[ -z "$IMAGE_URL" && ( "$PROFILE" == "pi5" || "$PROFILE" == "pi4" || "$PROFILE" == "pi3bplus" ) ]]; then
    IMAGE_URL="https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz"
  fi

  if [[ -z "$IMAGE_URL" ]]; then
    echo "Provide --image-url or --image-file for profile: $PROFILE" >&2
    exit 1
  fi
  validate_http_url "--image-url" "$IMAGE_URL"
fi

if [[ "${IMAGE_URL,,} ${IMAGE_FILE,,}" =~ raspios[^[:space:]]*(bookworm|bullseye) ]]; then
  echo "Legacy Raspberry Pi OS Bookworm/Bullseye images lack cloud-init; choose Ubuntu Server or cloud-init-enabled Pi OS Trixie." >&2
  exit 1
fi

mkdir -p "$WORKSPACE"
TMP_DIR="$(mktemp -d "${WORKSPACE}/sdprep.XXXXXX")"
trap 'if mountpoint -q "$TMP_DIR/boot"; then umount "$TMP_DIR/boot"; fi; if [[ -n "${CACHE_PART:-}" ]]; then rm -f "$CACHE_PART" "${CACHE_PART}.sha256"; fi; rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/boot"

DOWNLOADED_IMAGE=""
SOURCE_IMAGE=""
CACHE_PART=""
CACHE_TARGET=""
IMAGE_CACHE_DIR="${IMAGE_CACHE_DIR:-${CALLBACK_APPDATA_DIR:-${SCRIPT_DIR}/../callback_site/data}/image_cache}"
CACHE_LOCKED="false"

if [[ -n "$IMAGE_FILE" ]]; then
  if [[ ! -f "$IMAGE_FILE" ]]; then
    echo "Image file not found: $IMAGE_FILE" >&2
    exit 1
  fi
  SOURCE_IMAGE="$IMAGE_FILE"
  if [[ "$IMAGE_FILE" == "$IMAGE_CACHE_DIR/"* ]]; then
    require_cmd flock
    exec 9>"${IMAGE_FILE}.lock"
    flock 9
    CACHE_LOCKED="true"
  fi
  if [[ -z "$IMAGE_SHA256" && -f "${SOURCE_IMAGE}.sha256" ]]; then
    IMAGE_SHA256="$(cat "${SOURCE_IMAGE}.sha256")"
  fi
else
  mkdir -p "$IMAGE_CACHE_DIR"
  URL_DIGEST="$(printf '%s' "$IMAGE_URL" | sha256sum | awk '{print $1}')"
  case "${IMAGE_URL%%\?*}" in
    *.img.xz) IMAGE_SUFFIX=".img.xz" ;;
    *.img) IMAGE_SUFFIX=".img" ;;
    *) echo "Unsupported image format. Use .img or .img.xz" >&2; exit 1 ;;
  esac
  CACHE_TARGET="${IMAGE_CACHE_PATH:-${IMAGE_CACHE_DIR}/${URL_DIGEST}${IMAGE_SUFFIX}}"
  require_cmd flock
  exec 9>"${CACHE_TARGET}.lock"
  echo "Waiting for image cache lock..."
  flock 9
  CACHE_LOCKED="true"
  echo "Image source: $IMAGE_URL"
  if [[ -s "$CACHE_TARGET" ]]; then
    SOURCE_IMAGE="$CACHE_TARGET"
    if [[ -z "$IMAGE_SHA256" && -f "${CACHE_TARGET}.sha256" ]]; then
      IMAGE_SHA256="$(cat "${CACHE_TARGET}.sha256")"
    fi
    echo "Using cached image: $SOURCE_IMAGE"
  else
    CACHE_PART="$(mktemp "${CACHE_TARGET}.XXXXXX.part")"
    DOWNLOADED_IMAGE="$CACHE_PART"
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
fi

if [[ -z "$IMAGE_SHA256" && -n "$IMAGE_URL" && -n "$DOWNLOADED_IMAGE" ]]; then
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
      IMAGE_BASENAME="$(basename "$IMAGE_URL")"
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

if [[ -n "${CACHE_TARGET:-}" ]]; then
  if [[ -n "$CACHE_PART" ]]; then
    # Publish only a complete, verified download; keep it even if flashing fails.
    ACTUAL_SHA256="$(sha256sum "$SOURCE_IMAGE" | awk '{print $1}')"
    printf '%s\n' "$ACTUAL_SHA256" > "${CACHE_PART}.sha256"
    mv "$CACHE_PART" "$CACHE_TARGET"
    mv "${CACHE_PART}.sha256" "${CACHE_TARGET}.sha256"
    CACHE_PART=""
    SOURCE_IMAGE="$CACHE_TARGET"
  fi
  echo "Cached image: $SOURCE_IMAGE"
fi
if [[ "$CACHE_LOCKED" == "true" ]]; then
  flock -u 9
  exec 9>&-
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
