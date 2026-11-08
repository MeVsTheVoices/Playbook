# Validate the generated seed before any destructive device operations.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

cat > "$META_DATA_OUT" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: ${HOSTNAME}
EOF

if [[ "$PROFILE" == "network-toolkit" ]]; then
  toolkit_render network > "$TMP_DIR/network-config"
fi

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
