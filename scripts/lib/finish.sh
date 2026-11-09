# Copy the validated seed to the boot partition and apply boot config tweaks.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

echo "Injecting cloud-init files into boot partition"
cp "$USER_DATA_OUT" "$TMP_DIR/boot/user-data"
cp "$META_DATA_OUT" "$TMP_DIR/boot/meta-data"

if [[ "$PROFILE" == "network-toolkit" ]]; then
  cp "$TMP_DIR/network-config" "$TMP_DIR/boot/network-config"
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

echo "Verifying cloud-init seed from the card..."
blockdev --flushbufs "$DEVICE"
mount -o ro "$BOOT_PART" "$TMP_DIR/boot"
for seed_dir in "$TMP_DIR/boot" "$TMP_DIR/boot/boot/firmware"; do
  [[ -d "$seed_dir" ]] || continue
  for seed_file in user-data meta-data; do
    if ! cmp -- "$TMP_DIR/$seed_file" "$seed_dir/$seed_file"; then
      echo "Cloud-init seed readback failed: $seed_dir/$seed_file. Card is not ready." >&2
      exit 1
    fi
  done
  if [[ "$PROFILE" == "network-toolkit" ]] && ! cmp -- "$TMP_DIR/network-config" "$seed_dir/network-config"; then
    echo "Network configuration readback failed: $seed_dir/network-config. Card is not ready." >&2
    exit 1
  fi
done
umount "$TMP_DIR/boot"

if [[ -n "${FLASH_EXPECTED_IDENTITY:-}" ]]; then
  python3 "$SCRIPT_DIR/../flash_helper/devices.py" "$DEVICE" "$FLASH_EXPECTED_IDENTITY"
fi
echo "Done. SD card is ready for ${HOSTNAME} (${PROFILE})."
