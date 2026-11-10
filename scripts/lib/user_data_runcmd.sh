# runcmd: install steps and the final callback.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

# Match raspi-config's per-user rule without deleting other cloud-init sudo grants.
cat >> "$USER_DATA_OUT" <<ADMIN_RUNCMD
  - [ bash, -c, 'set -euo pipefail; rule="${CLOUD_INIT_USERNAME} ALL=(ALL) NOPASSWD: ALL"; printf "%s\n" "\$rule" | visudo -cf -; printf "%s\n" "\$rule" | install -o root -g root -m 0440 /dev/stdin /etc/sudoers.d/010_${CLOUD_INIT_USERNAME}-nopasswd; visudo -cf /etc/sudoers.d/010_${CLOUD_INIT_USERNAME}-nopasswd; runuser -u ${CLOUD_INIT_USERNAME} -- sudo -n true' ]
ADMIN_RUNCMD

cat >> "$USER_DATA_OUT" <<'SSH_RUNCMD'
  - [ systemctl, enable, ssh ]
  - [ systemctl, restart, ssh ]
SSH_RUNCMD

if [[ "$CLOUD_INIT_CONFIGURE_NEOVIM" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<NEOVIM_RUNCMD
  - [ /usr/local/bin/rack-neovim-bootstrap, ${CLOUD_INIT_USERNAME} ]
NEOVIM_RUNCMD
fi

# A Pi has no RTC and boots on the image build date, so HTTPS installers fail with "certificate is not yet valid".
cat >> "$USER_DATA_OUT" <<'CLOCK_RUNCMD'
  - [ sh, -c, 'timedatectl set-ntp true || true; for i in $(seq 1 90); do [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && exit 0; sleep 2; done; echo "rack: clock not synced after 180s" >&2; exit 0' ]
  - [ rm, -f, /etc/apt/apt.conf.d/00-rack-clock ]
CLOCK_RUNCMD

if [[ -n "$CLOUD_INIT_SSH_PUBLIC_KEY_URL" ]]; then
  cat >> "$USER_DATA_OUT" <<'SSH_FETCH_RUNCMD'
  - [ bash, -c, '. /etc/rack-node.env; /usr/local/bin/rack-ssh-key-bootstrap "$CLOUD_INIT_SSH_PUBLIC_KEY_URL" "$CLOUD_INIT_SSH_PUBLIC_KEY_SHA256" "$CLOUD_INIT_USERNAME"' ]
SSH_FETCH_RUNCMD
fi

if [[ "$CLOUD_INIT_TAILSCALE_ENABLE" == "true" ]]; then
  # Must run before the K3s install so the Tailscale address is already in the K3s config.
  cat >> "$USER_DATA_OUT" <<'TS_RUNCMD'
  - [ /usr/local/bin/rack-tailscale-bootstrap ]
TS_RUNCMD
fi

if [[ "$DISTRIBUTION" == "k3s" ]]; then
  if [[ -z "${CLOUD_INIT_K3S_CONFIG:-}" ]]; then
    DEFAULT_K3S_CONFIG="# pragma: k3s-config-v1
server: https://${DEFAULT_K3S_SERVER_HOST}:6443
node-name: ${HOSTNAME}
"
  fi

  K3S_INSTALL_MODE="$(k3s_mode)"

  K3S_SERVER_EXEC="server --with-node-id --config /etc/rancher/k3s/config.yaml"
  K3S_AGENT_EXEC="agent --with-node-id --config /etc/rancher/k3s/config.yaml"
  if [[ "${CLOUD_INIT_K3S_SECURITY_BASELINE:-false}" == "true" ]]; then
    echo "WARNING: K3s security baseline is on; it adds --secrets-encryption, which conflicts with the current cluster (encryption disabled). Leave it off until the cluster is migrated." >&2
    K3S_SERVER_EXEC="$K3S_SERVER_EXEC --secrets-encryption --kube-apiserver-arg anonymous-auth=false --kube-apiserver-arg authorization-mode=Node,RBAC --kubelet-arg read-only-port=0 --kubelet-arg protect-kernel-defaults=true"
    K3S_AGENT_EXEC="$K3S_AGENT_EXEC --kubelet-arg read-only-port=0 --kubelet-arg protect-kernel-defaults=true"
  fi

  K3S_VERSION_ENV=""
  if [[ -n "$CLOUD_INIT_K3S_VERSION" ]]; then
    K3S_VERSION_ENV="INSTALL_K3S_VERSION=$CLOUD_INIT_K3S_VERSION "
  fi
  # etcd on an SD card is what broke quorum before: stop a server install there unless it was allowed.
  # The guard and the install share one sh -c, because cloud-init keeps running after a failed runcmd entry.
  K3S_SD_GUARD=""
  if [[ "$K3S_INSTALL_MODE" == "server" && "$CLOUD_INIT_K3S_ALLOW_SD_ETCD" != "true" ]]; then
    K3S_SD_GUARD='case "$(findmnt -no SOURCE --target /var/lib)" in /dev/mmcblk*) echo "rack: refusing to install a K3s server on an SD card (etcd needs an SSD)" >&2; exit 1;; esac; '
  fi

  if [[ "$K3S_INSTALL_MODE" == "agent" ]]; then
    cat >> "$USER_DATA_OUT" <<K3S_RUNCMD
  - [ sh, -c, 'mkdir -p /etc/rancher/k3s && curl -fsSL https://get.k3s.io | ${K3S_VERSION_ENV}INSTALL_K3S_EXEC="$K3S_AGENT_EXEC" sh -' ]
K3S_RUNCMD
  else
  cat >> "$USER_DATA_OUT" <<K3S_RUNCMD
  - [ sh, -c, '${K3S_SD_GUARD}mkdir -p /etc/rancher/k3s && curl -fsSL https://get.k3s.io | ${K3S_VERSION_ENV}INSTALL_K3S_EXEC="$K3S_SERVER_EXEC" sh -' ]
K3S_RUNCMD
  fi
fi

# Conditionally append runcmd items based on cloud-init options
if [[ "${CLOUD_INIT_ENABLE_HARDWARE_CHECKS:-false}" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ /usr/local/bin/rack-hardware-check ]
RUNCMD_EOF
fi

if [[ "${CLOUD_INIT_INSTALL_TELEGRAF:-false}" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ /usr/local/bin/rack-telegraf-bootstrap ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_ENABLE_FAIL2BAN" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'if systemctl list-unit-files | grep -q "^fail2ban.service"; then systemctl enable --now fail2ban || true; fi' ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'if command -v apt-get >/dev/null 2>&1; then DEBIAN_FRONTEND=noninteractive apt-get update || true; DEBIAN_FRONTEND=noninteractive apt-get -y install unattended-upgrades || true; DEBIAN_FRONTEND=noninteractive dpkg-reconfigure -f noninteractive unattended-upgrades || true; elif command -v dnf >/dev/null 2>&1; then dnf -y install dnf-automatic || true; systemctl enable --now dnf-automatic.timer || true; fi' ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_ENABLE_SYSCTL_HARDENING" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'sysctl --system || true' ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_ENABLE_FIREWALL_BASELINE" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<RUNCMD_EOF
  - [ sh, -c, 'if command -v firewall-cmd >/dev/null 2>&1; then systemctl enable --now firewalld || true; firewall-cmd --permanent --add-service=ssh || true; firewall-cmd --reload || true; elif command -v ufw >/dev/null 2>&1; then ufw allow ${CLOUD_INIT_SSH_PORT:-22}/tcp || true; ufw --force enable || true; fi' ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_ENABLE_SELINUX_ENFORCING" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'if [ -f /etc/selinux/config ]; then sed -i "s/^SELINUX=.*/SELINUX=enforcing/" /etc/selinux/config || true; command -v setenforce >/dev/null 2>&1 && setenforce 1 || true; fi' ]
RUNCMD_EOF
fi

if [[ "$CLOUD_INIT_BOOT_SUMMARY" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ /usr/local/bin/rack-bootstrap-summary ]
RUNCMD_EOF
fi

if [[ -n "$CLOUD_INIT_EXTRA_SSH_KEYS" ]]; then
  cat >> "$USER_DATA_OUT" <<EXTRA_KEYS_RUNCMD
  - [ sh, -c, 'if [ -f /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys ]; then cat /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys >> /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys; chown ${CLOUD_INIT_USERNAME}:${CLOUD_INIT_USERNAME} /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys; chmod 600 /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys; fi' ]
EXTRA_KEYS_RUNCMD
fi

# Append final runcmd callback using a runtime payload generator so all values are expanded on the node, not during host-side template generation.
if [[ "$PROFILE" == "network-toolkit" ]]; then
  cat >> "$USER_DATA_OUT" <<'TOOLKIT_BOOT_FIXES_RUNCMD'
  - [ /usr/local/bin/rack-toolkit-boot-fixes ]
TOOLKIT_BOOT_FIXES_RUNCMD
  read -r TOOLKIT_TTYD_URL TOOLKIT_TTYD_SHA256 < <(toolkit_render ttyd)
  cat >> "$USER_DATA_OUT" <<TOOLKIT_TTYD_RUNCMD
  - [ bash, -c, 'set -euo pipefail; tmp=\$(mktemp); trap ''rm -f "\$tmp"'' EXIT; curl --retry 3 --connect-timeout 10 --max-time 120 -fsSL "${TOOLKIT_TTYD_URL}" -o "\$tmp"; printf "%s  %s\n" "${TOOLKIT_TTYD_SHA256}" "\$tmp" | sha256sum -c -; install -o root -g root -m 0755 "\$tmp" /usr/local/bin/ttyd' ]
TOOLKIT_TTYD_RUNCMD
  cat >> "$USER_DATA_OUT" <<'TOOLKIT_RUNCMD'
  - [ /usr/local/bin/rack-toolkit-activate ]
TOOLKIT_RUNCMD
  if [[ "${CLOUD_INIT_TOOLKIT_ENABLE_FUNNEL:-false}" == "true" ]]; then
    cat >> "$USER_DATA_OUT" <<'TOOLKIT_RUNCMD'
  - [ systemctl, enable, --now, rack-toolkit-funnel.service ]
TOOLKIT_RUNCMD
  fi
fi

cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ bash, -c, '. /etc/rack-node.env; SOURCE_IP="$(/usr/local/bin/rack-local-ip 2>/dev/null || true)"; NODE_REPORT_B64="$(/usr/local/bin/rack-node-report 2>/dev/null | base64 -w0 || true)"; PAYLOAD_JSON="$(INSTANCE_ID="$INSTANCE_ID" HOSTNAME="$HOSTNAME" PROFILE="$PROFILE" SOURCE_IP="$SOURCE_IP" NODE_REPORT_B64="$NODE_REPORT_B64" /usr/local/bin/rack-callback-payload)"; if ! curl --retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors --connect-timeout 5 --max-time 20 -fsS -X POST -H "Content-Type: application/json" -d "$PAYLOAD_JSON" "$CALLBACK_URL"; then echo "rack callback POST failed after retries to $CALLBACK_URL" >&2; fi' ]
  - [ sh, -c, '/usr/local/bin/rack-boot-log runcmd || true' ]

RUNCMD_EOF

if [[ "$CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'
  - [ sh, -c, 'systemctl daemon-reload && systemctl enable --now rack-power-restored-callback.service' ]
RUNCMD_EOF
fi

# Let cloud-init finish its final modules before requesting shutdown.
if [[ "$CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'RUNCMD_EOF'

power_state:
  mode: reboot
  delay: now
  timeout: 600
  condition: true
RUNCMD_EOF
fi

cat >> "$USER_DATA_OUT" <<RUNCMD_EOF

final_message: "Cloud-init finished for ${HOSTNAME}"
RUNCMD_EOF
