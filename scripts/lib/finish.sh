# meta-data, schema check, copy to the boot partition and boot config tweaks.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

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

if [[ "$PROFILE" == "network-toolkit" ]]; then
  toolkit_render network > "$TMP_DIR/boot/network-config"
fi

if [[ "$PROFILE" == "pi5" && "${CLOUD_INIT_USB_MAX_CURRENT_ENABLE:-false}" == "true" ]]; then
  for config_target in "$TMP_DIR/boot/config.txt" "$TMP_DIR/boot/boot/firmware/config.txt"; do
    if [[ -f "$config_target" ]]; then
      if ! grep -q '^usb_max_current_enable=1$' "$config_target"; then
        printf '\nusb_max_current_enable=1\n' >> "$config_target"
      fi
    fi
  done
fi

if [[ "$PROFILE" == "pi5" && ( "${CLOUD_INIT_PCIEX1_ENABLE:-false}" == "true" || "${CLOUD_INIT_PCIEX1_GEN3:-false}" == "true" ) ]]; then
  for config_target in "$TMP_DIR/boot/config.txt" "$TMP_DIR/boot/boot/firmware/config.txt"; do
    if [[ -f "$config_target" ]]; then
      if ! grep -q '^dtparam=pciex1$' "$config_target"; then
        printf '\ndtparam=pciex1\n' >> "$config_target"
      fi
      if [[ "${CLOUD_INIT_PCIEX1_GEN3:-false}" == "true" ]]; then
        if ! grep -q '^dtparam=pciex1_gen=3$' "$config_target"; then
          printf 'dtparam=pciex1_gen=3\n' >> "$config_target"
        fi
      fi
    fi
  done
fi

if [[ -d "$TMP_DIR/boot/boot/firmware" ]]; then
  cp "$USER_DATA_OUT" "$TMP_DIR/boot/boot/firmware/user-data"
  cp "$META_DATA_OUT" "$TMP_DIR/boot/boot/firmware/meta-data"
  if [[ "$PROFILE" == "network-toolkit" ]]; then
    cp "$TMP_DIR/boot/network-config" "$TMP_DIR/boot/boot/firmware/network-config"
  fi
fi

# Raspberry Pi OS starts without the memory cgroup, and K3s cannot start without it.
if [[ "$DISTRIBUTION" == "k3s" ]]; then
  for cmdline_file in "$TMP_DIR/boot/cmdline.txt" "$TMP_DIR/boot/boot/firmware/cmdline.txt"; do
    if [[ -f "$cmdline_file" ]]; then
      for cgroup_flag in cgroup_memory=1 cgroup_enable=memory; do
        if ! grep -qw -- "$cgroup_flag" "$cmdline_file"; then
          sed -i "1s/\$/ ${cgroup_flag}/" "$cmdline_file"
        fi
      done
    fi
  done
fi

sync
umount "$TMP_DIR/boot"

echo "Done. SD card is ready for ${HOSTNAME} (${PROFILE})."
