# write_files for Tailscale, K3s, SSH hardening, fail2ban and sysctl.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

if [[ "$CLOUD_INIT_TAILSCALE_ENABLE" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<TS_KEY_FILE
  - path: /etc/rack-tailscale-authkey
    permissions: "0600"
    content: |
      ${CLOUD_INIT_TAILSCALE_AUTH_KEY}
TS_KEY_FILE
  cat >> "$USER_DATA_OUT" <<'TS_BOOTSTRAP'
  - path: /usr/local/bin/rack-tailscale-bootstrap
    permissions: "0755"
    content: |
      #!/bin/bash
      # First-boot Tailscale join, run before K3s so K3s can use the Tailscale address as node-ip.
      # Fail-safe: always exits 0 so cloud-init continues; Node Ops can finish a failed join later.
      set -uo pipefail
      ENV_FILE="${RACK_NODE_ENV:-/etc/rack-node.env}"
      KEY_FILE="${RACK_TS_KEY_FILE:-/etc/rack-tailscale-authkey}"
      K3S_CONFIG="${RACK_K3S_CONFIG_PATH:-/etc/rancher/k3s/config.yaml}"
      # shellcheck disable=SC1090
      . "$ENV_FILE"

      log() { echo "rack-tailscale: $*" >&2; }

      if ! command -v tailscale >/dev/null 2>&1; then
        # First boot races DNS, the clock and cloud-init's own apt run, so retry instead of giving up.
        installed=false
        for attempt in 1 2 3 4 5; do
          if curl -fsSL https://tailscale.com/install.sh | sh; then
            installed=true
            break
          fi
          log "install attempt ${attempt} failed"
          sleep 20
        done
        if [[ "$installed" != true ]]; then
          log "install failed; continuing without Tailscale"
          exit 0
        fi
      fi
      systemctl enable --now tailscaled || true

      # Tailscale accepts the key from a file; strip any whitespace the file format added.
      if [[ -f "$KEY_FILE" ]]; then
        tr -d '[:space:]' < "$KEY_FILE" > "${KEY_FILE}.clean" && mv "${KEY_FILE}.clean" "$KEY_FILE"
        chmod 0600 "$KEY_FILE"
      fi

      up_args=("--auth-key=file:${KEY_FILE}" "--hostname=${CLOUD_INIT_TAILSCALE_HOSTNAME}" "--advertise-tags=${CLOUD_INIT_TAILSCALE_TAGS}")
      if [[ "${CLOUD_INIT_TAILSCALE_SSH:-false}" == "true" ]]; then up_args+=(--ssh); fi
      if [[ -n "${CLOUD_INIT_TAILSCALE_ROUTES:-}" ]]; then up_args+=("--advertise-routes=${CLOUD_INIT_TAILSCALE_ROUTES}"); fi
      if [[ -n "${CLOUD_INIT_TAILSCALE_EXIT_NODE:-}" ]]; then up_args+=("--exit-node=${CLOUD_INIT_TAILSCALE_EXIT_NODE}"); fi

      joined=false
      for attempt in 1 2 3; do
        if tailscale up --timeout=60s "${up_args[@]}"; then
          joined=true
          break
        fi
        log "tailscale up failed (attempt ${attempt})"
        sleep 5
      done
      rm -f "$KEY_FILE"
      if [[ "$joined" != true ]]; then
        log "could not join the tailnet; K3s will start without a Tailscale node-ip"
        exit 0
      fi

      ts_ip=""
      for _ in $(seq 1 30); do
        ts_ip="$(tailscale ip -4 2>/dev/null | head -n1)"
        if [[ -n "$ts_ip" ]]; then break; fi
        sleep 2
      done
      if [[ -z "$ts_ip" ]]; then
        log "joined but no Tailscale IPv4 yet; K3s will start without node-ip"
        exit 0
      fi

      if [[ -f "$K3S_CONFIG" ]]; then
        sed -i -E '/^(node-ip|advertise-address)[[:space:]]*:/d' "$K3S_CONFIG"
        if [[ -n "$(tail -c1 "$K3S_CONFIG")" ]]; then echo >> "$K3S_CONFIG"; fi
        printf 'node-ip: %s\n' "$ts_ip" >> "$K3S_CONFIG"
        if [[ "${RACK_K3S_MODE:-}" == "server" ]]; then printf 'advertise-address: %s\n' "$ts_ip" >> "$K3S_CONFIG"; fi
        log "set K3s node-ip to ${ts_ip}"
      fi
      exit 0
TS_BOOTSTRAP
fi

if [[ "$DISTRIBUTION" == "k3s" ]]; then
  SANITIZED_K3S_CONFIG="$(printf '%s\n' "$DEFAULT_K3S_CONFIG" | awk -v hostname="${HOSTNAME}" '
    BEGIN { seen_node_name = 0 }
    /^[[:space:]]*#/ { print; next }
    tolower($0) ~ /^[[:space:]]*node-name[[:space:]]*:/ {
      if (!seen_node_name) {
        printf "node-name: %s\n", hostname
        seen_node_name = 1
      }
      next
    }
    tolower($0) ~ /^[[:space:]]*node-ip[[:space:]]*:/ { next }
    { print }
  ' )"
  cat >> "$USER_DATA_OUT" <<K3S_FILE
  - path: /etc/rancher/k3s/config.yaml
    permissions: "0600"
    content: |
$(printf '%s\n' "$SANITIZED_K3S_CONFIG" | sed 's/^/      /')
K3S_FILE
fi

if [[ "$CLOUD_INIT_DISABLE_ROOT_SSH" == "true" || "$CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH" == "true" || -n "$CLOUD_INIT_SSH_PORT" || -n "$CLOUD_INIT_SSH_ALLOW_GROUPS" ]]; then
  cat >> "$USER_DATA_OUT" <<SSH_HARDEN_FILE
  - path: /etc/ssh/sshd_config.d/99-rack-hardening.conf
    permissions: "0644"
    content: |
      # Managed by rack cloud-init option defaults.
SSH_HARDEN_FILE
  if [[ "$CLOUD_INIT_DISABLE_ROOT_SSH" == "true" ]]; then
    echo "      PermitRootLogin no" >> "$USER_DATA_OUT"
  fi
  if [[ "$CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH" == "true" ]]; then
    echo "      PasswordAuthentication no" >> "$USER_DATA_OUT"
    echo "      KbdInteractiveAuthentication no" >> "$USER_DATA_OUT"
  fi
  if [[ -n "$CLOUD_INIT_SSH_PORT" ]]; then
    echo "      Port ${CLOUD_INIT_SSH_PORT}" >> "$USER_DATA_OUT"
  fi
  if [[ -n "$CLOUD_INIT_SSH_ALLOW_GROUPS" ]]; then
    echo "      AllowGroups ${CLOUD_INIT_SSH_ALLOW_GROUPS}" >> "$USER_DATA_OUT"
  fi
fi

if [[ "$CLOUD_INIT_ENABLE_FAIL2BAN" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<FAIL2BAN_CONF
  - path: /etc/fail2ban/jail.d/rack-sshd.local
    permissions: "0644"
    content: |
      [sshd]
      enabled = true
      backend = systemd
      port = ${CLOUD_INIT_SSH_PORT:-ssh}
      maxretry = 6
      findtime = 10m
      bantime = 1h
FAIL2BAN_CONF
fi

if [[ -n "$CLOUD_INIT_EXTRA_SSH_KEYS" ]]; then
  cat >> "$USER_DATA_OUT" <<EXTRA_KEYS
  - path: /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys
    permissions: "0600"
    owner: ${CLOUD_INIT_USERNAME}:${CLOUD_INIT_USERNAME}
    content: |
$(printf '%s\n' "$CLOUD_INIT_EXTRA_SSH_KEYS" | sed 's/^/      /')
EXTRA_KEYS
fi

if [[ "$CLOUD_INIT_ENABLE_SYSCTL_HARDENING" == "true" ]]; then
  cat >> "$USER_DATA_OUT" <<'SYSCTL_HARDEN_FILE'
  - path: /etc/sysctl.d/99-rack-hardening.conf
    permissions: "0644"
    content: |
      net.ipv4.conf.all.accept_redirects = 0
      net.ipv4.conf.default.accept_redirects = 0
      net.ipv4.conf.all.send_redirects = 0
      net.ipv4.conf.default.send_redirects = 0
      net.ipv4.conf.all.accept_source_route = 0
      net.ipv4.conf.default.accept_source_route = 0
      net.ipv4.conf.all.rp_filter = 1
      net.ipv4.conf.default.rp_filter = 1
      net.ipv4.tcp_syncookies = 1
      net.ipv6.conf.all.accept_redirects = 0
      net.ipv6.conf.default.accept_redirects = 0
      kernel.kptr_restrict = 2
      kernel.dmesg_restrict = 1
      fs.suid_dumpable = 0
SYSCTL_HARDEN_FILE
fi
