# Find, verify and decompress the OS image.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

if [[ -z "$IMAGE_FILE" ]]; then
  if [[ -z "$IMAGE_URL" && ( "$PROFILE" == "pi5" || "$PROFILE" == "pi4" || "$PROFILE" == "pi3bplus" ) ]]; then
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
