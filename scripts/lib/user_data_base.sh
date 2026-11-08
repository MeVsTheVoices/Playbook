# user-data header: user, packages and the node-side environment file.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

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
    sudo: "ALL=(ALL) NOPASSWD: ALL"
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

bootcmd:
  - [ sh, -c, 'timedatectl set-ntp true; for i in \$(seq 1 90); do [ "\$(timedatectl show -p NTPSynchronized --value)" = yes ] && exit 0; sleep 2; done; echo "rack: clock not synced after 180s; repository signatures may fail" >&2; exit 1' ]

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

RACK_K3S_MODE=""
if [[ "$DISTRIBUTION" == "k3s" ]]; then
  RACK_K3S_MODE="$(k3s_mode)"
fi

# Root-only environment for the node-side scripts below. Those scripts run on the node,
# where this host's CLOUD_INIT_* variables do not exist, so every value they read is baked here.
cat >> "$USER_DATA_OUT" <<'NODE_ENV_HEADER'
  - path: /etc/rack-node.env
    permissions: "0600"
    content: |
NODE_ENV_HEADER
for node_env_var in \
  CALLBACK_URL INSTANCE_ID PROFILE HOSTNAME PROFILE_ROLE PROFILE_STORAGE DISTRIBUTION RACK_K3S_MODE \
  CLOUD_INIT_USERNAME CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH CLOUD_INIT_DISABLE_ROOT_SSH \
  CLOUD_INIT_BOOT_SUMMARY CLOUD_INIT_ENABLE_FAIL2BAN CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES \
  CLOUD_INIT_ENABLE_HARDWARE_CHECKS CLOUD_INIT_ENABLE_SYSCTL_HARDENING \
  CLOUD_INIT_ENABLE_FIREWALL_BASELINE CLOUD_INIT_ENABLE_SELINUX_ENFORCING \
  CLOUD_INIT_FANCY_MOTD CLOUD_INIT_INSTALL_TELEGRAF CLOUD_INIT_SSH_PORT CLOUD_INIT_SSH_ALLOW_GROUPS \
  CLOUD_INIT_SSH_PUBLIC_KEY_URL \
  CLOUD_INIT_TELEGRAF_INFLUX_URL CLOUD_INIT_TELEGRAF_INFLUX_TOKEN CLOUD_INIT_TELEGRAF_INFLUX_ORG \
  CLOUD_INIT_TELEGRAF_INFLUX_BUCKET CLOUD_INIT_TELEGRAF_CONFIG_URL \
  CLOUD_INIT_TAILSCALE_ENABLE CLOUD_INIT_TAILSCALE_HOSTNAME CLOUD_INIT_TAILSCALE_TAGS \
  CLOUD_INIT_TAILSCALE_SSH CLOUD_INIT_TAILSCALE_ROUTES CLOUD_INIT_TAILSCALE_EXIT_NODE; do
  emit_env_line "$node_env_var" "${!node_env_var:-}"
done
# The boot summary only reports whether a K3s config exists; never copy its content (it holds the token).
if [[ "$DISTRIBUTION" == "k3s" ]]; then
  emit_env_line CLOUD_INIT_K3S_CONFIG "set"
fi
if [[ -n "$CLOUD_INIT_SSH_PUBLIC_KEY_URL" ]]; then
  emit_env_line CLOUD_INIT_SSH_PUBLIC_KEY_SHA256 "$(printf '%s' "$SSH_KEY_CONTENT" | sha256sum | cut -d ' ' -f 1)"
fi
