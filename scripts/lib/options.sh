# cloud-init option defaults, Tailscale checks and per-profile packages.
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

USER_DATA_OUT="$TMP_DIR/user-data"
META_DATA_OUT="$TMP_DIR/meta-data"
INSTANCE_ID="${HOSTNAME}-$(date +%Y%m%d%H%M%S)"
CLOUD_INIT_USERNAME="${CLOUD_INIT_USERNAME:-jdizzle}"
CLOUD_INIT_USER_PASSWORD="${CLOUD_INIT_USER_PASSWORD:-}"
CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH="${CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH:-false}"
CLOUD_INIT_DISABLE_ROOT_SSH="${CLOUD_INIT_DISABLE_ROOT_SSH:-false}"
CLOUD_INIT_BOOT_SUMMARY="${CLOUD_INIT_BOOT_SUMMARY:-true}"
CLOUD_INIT_ENABLE_FAIL2BAN="${CLOUD_INIT_ENABLE_FAIL2BAN:-false}"
CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES="${CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES:-false}"
CLOUD_INIT_ENABLE_HARDWARE_CHECKS="${CLOUD_INIT_ENABLE_HARDWARE_CHECKS:-false}"
CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK="${CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK:-true}"
CLOUD_INIT_ENABLE_SYSCTL_HARDENING="${CLOUD_INIT_ENABLE_SYSCTL_HARDENING:-false}"
CLOUD_INIT_ENABLE_FIREWALL_BASELINE="${CLOUD_INIT_ENABLE_FIREWALL_BASELINE:-false}"
CLOUD_INIT_ENABLE_SELINUX_ENFORCING="${CLOUD_INIT_ENABLE_SELINUX_ENFORCING:-false}"
CLOUD_INIT_K3S_SECURITY_BASELINE="${CLOUD_INIT_K3S_SECURITY_BASELINE:-false}"
CLOUD_INIT_FANCY_MOTD="${CLOUD_INIT_FANCY_MOTD:-false}"
CLOUD_INIT_CONFIGURE_NEOVIM="${CLOUD_INIT_CONFIGURE_NEOVIM:-false}"
CLOUD_INIT_INSTALL_CLI_TOOLS="${CLOUD_INIT_INSTALL_CLI_TOOLS:-false}"
CLOUD_INIT_EXTRA_PACKAGES="${CLOUD_INIT_EXTRA_PACKAGES:-}"
for misc_value in "$CLOUD_INIT_CONFIGURE_NEOVIM" "$CLOUD_INIT_INSTALL_CLI_TOOLS"; do
  if [[ "$misc_value" != "true" && "$misc_value" != "false" ]]; then
    echo "Invalid Neovim or CLI tools flag: expected true or false." >&2
    exit 1
  fi
done
if [[ ! "$CLOUD_INIT_EXTRA_PACKAGES" =~ ^[a-z0-9+.\ -]*$ ]]; then
  echo "Invalid extra packages: use space-separated Debian package names." >&2
  exit 1
fi
read -r -a extra_package_names <<< "$CLOUD_INIT_EXTRA_PACKAGES"
for pkg in "${extra_package_names[@]}"; do
  if [[ ! "$pkg" =~ ^[a-z0-9][a-z0-9+.-]+$ ]]; then
    echo "Invalid extra package: ${pkg}. Use Debian package names, not versions, URLs or command options." >&2
    exit 1
  fi
done
CLOUD_INIT_INSTALL_TELEGRAF="${CLOUD_INIT_INSTALL_TELEGRAF:-false}"
CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP="${CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP:-false}"
CLOUD_INIT_SSH_PORT="${CLOUD_INIT_SSH_PORT:-}"
CLOUD_INIT_SSH_ALLOW_GROUPS="${CLOUD_INIT_SSH_ALLOW_GROUPS:-}"
CLOUD_INIT_EXTRA_SSH_KEYS="${CLOUD_INIT_EXTRA_SSH_KEYS:-}"
CLOUD_INIT_SSH_PUBLIC_KEY_URL="${CLOUD_INIT_SSH_PUBLIC_KEY_URL:-}"
if [[ -n "$CLOUD_INIT_SSH_PUBLIC_KEY_URL" ]]; then
  validate_http_url "CLOUD_INIT_SSH_PUBLIC_KEY_URL" "$CLOUD_INIT_SSH_PUBLIC_KEY_URL"
fi
CLOUD_INIT_TAILSCALE_ENABLE="${CLOUD_INIT_TAILSCALE_ENABLE:-false}"
CLOUD_INIT_TAILSCALE_AUTH_KEY="${CLOUD_INIT_TAILSCALE_AUTH_KEY:-}"
CLOUD_INIT_TAILSCALE_HOSTNAME="${CLOUD_INIT_TAILSCALE_HOSTNAME:-${HOSTNAME}}"
CLOUD_INIT_TAILSCALE_TAGS="${CLOUD_INIT_TAILSCALE_TAGS:-}"
CLOUD_INIT_TAILSCALE_SSH="${CLOUD_INIT_TAILSCALE_SSH:-false}"
CLOUD_INIT_TAILSCALE_ROUTES="${CLOUD_INIT_TAILSCALE_ROUTES:-}"
CLOUD_INIT_TAILSCALE_EXIT_NODE="${CLOUD_INIT_TAILSCALE_EXIT_NODE:-}"

if [[ "$PROFILE" == "network-toolkit" ]]; then
  CLOUD_INIT_TAILSCALE_ENABLE="true"
  CLOUD_INIT_TAILSCALE_TAGS="tag:network-toolkit"
  CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH="true"
  CLOUD_INIT_DISABLE_ROOT_SSH="true"
  CLOUD_INIT_ENABLE_HARDWARE_CHECKS="true"
  CLOUD_INIT_INSTALL_TELEGRAF="false"
  CLOUD_INIT_FANCY_MOTD="false"
  CLOUD_INIT_CONFIGURE_NEOVIM="false"
  CLOUD_INIT_INSTALL_CLI_TOOLS="false"
  CLOUD_INIT_EXTRA_PACKAGES=""
  CLOUD_INIT_ENABLE_FIREWALL_BASELINE="false"
fi

if [[ "$CLOUD_INIT_TAILSCALE_ENABLE" == "true" ]]; then
  if [[ -z "$CLOUD_INIT_TAILSCALE_TAGS" ]]; then
    # Same defaults the Ansible roles use, so first-boot and Node Ops agree.
    if [[ "$PROFILE" == "playbook-server" ]]; then
      CLOUD_INIT_TAILSCALE_TAGS="tag:homelab"
    elif [[ "$PROFILE" == "pi3bplus" ]]; then
      CLOUD_INIT_TAILSCALE_TAGS="tag:rpi-3bplus,tag:homelab"
    elif [[ "$PROFILE" == "pi4" ]]; then
      CLOUD_INIT_TAILSCALE_TAGS="tag:rpi4,tag:homelab"
    else
      CLOUD_INIT_TAILSCALE_TAGS="tag:rpi5,tag:homelab"
    fi
  fi
  if [[ -z "$CLOUD_INIT_TAILSCALE_AUTH_KEY" ]]; then
    echo "Tailscale is enabled but no auth key was provided; 'tailscale up' would wait for an interactive login forever." >&2
    exit 1
  fi
  if [[ ! "$CLOUD_INIT_TAILSCALE_AUTH_KEY" =~ ^tskey-[A-Za-z0-9_-]+$ ]]; then
    echo "Invalid CLOUD_INIT_TAILSCALE_AUTH_KEY: expected a tskey-... auth key." >&2
    exit 1
  fi
  if [[ ! "$CLOUD_INIT_TAILSCALE_TAGS" =~ ^tag:[A-Za-z0-9-]+(,tag:[A-Za-z0-9-]+)*$ ]]; then
    echo "Invalid CLOUD_INIT_TAILSCALE_TAGS: expected comma-separated tag:name values." >&2
    exit 1
  fi
  if [[ ! "$CLOUD_INIT_TAILSCALE_HOSTNAME" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]; then
    echo "Invalid CLOUD_INIT_TAILSCALE_HOSTNAME value: ${CLOUD_INIT_TAILSCALE_HOSTNAME}" >&2
    exit 1
  fi
  if [[ -n "$CLOUD_INIT_TAILSCALE_ROUTES" && ! "$CLOUD_INIT_TAILSCALE_ROUTES" =~ ^[0-9A-Fa-f:./]+(,[0-9A-Fa-f:./]+)*$ ]]; then
    echo "Invalid CLOUD_INIT_TAILSCALE_ROUTES: expected comma-separated CIDRs." >&2
    exit 1
  fi
  if [[ -n "$CLOUD_INIT_TAILSCALE_EXIT_NODE" && ! "$CLOUD_INIT_TAILSCALE_EXIT_NODE" =~ ^[A-Za-z0-9._:-]+$ ]]; then
    echo "Invalid CLOUD_INIT_TAILSCALE_EXIT_NODE value." >&2
    exit 1
  fi
fi

# Append one KEY=value line (shell-quoted, so any value is safe) to the baked node env file.
emit_env_line() {
  printf '      %s=%q\n' "$1" "$2" >> "$USER_DATA_OUT"
}

CLOUD_INIT_K3S_MODE="${CLOUD_INIT_K3S_MODE:-}"
if [[ ! "$CLOUD_INIT_K3S_MODE" =~ ^(server|agent)?$ ]]; then
  echo "Invalid CLOUD_INIT_K3S_MODE: expected server or agent." >&2
  exit 1
fi

# An explicit mode wins; otherwise guess agent from worker markers in the config text.
k3s_mode() {
  if [[ -n "$CLOUD_INIT_K3S_MODE" ]]; then
    printf '%s' "$CLOUD_INIT_K3S_MODE"
  elif printf '%s\n' "$DEFAULT_K3S_CONFIG" | grep -Eq 'role:\s*"?worker"?|role=worker|node-role\.kubernetes\.io/worker'; then
    printf agent
  else
    printf server
  fi
}

# Pin the install so a new node matches the servers; blank follows the stable channel.
CLOUD_INIT_K3S_VERSION="${CLOUD_INIT_K3S_VERSION:-}"
CLOUD_INIT_K3S_ALLOW_SD_ETCD="${CLOUD_INIT_K3S_ALLOW_SD_ETCD:-false}"
if [[ -n "$CLOUD_INIT_K3S_VERSION" && ! "$CLOUD_INIT_K3S_VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+$ ]]; then
  echo "Invalid CLOUD_INIT_K3S_VERSION: expected something like v1.36.4+k3s1." >&2
  exit 1
fi
if [[ "$DISTRIBUTION" == "k3s" ]]; then
  if [[ -z "$CLOUD_INIT_K3S_VERSION" ]]; then
    echo "NOTE: K3s version is not pinned; the node installs the current stable release, which may be newer than the servers." >&2
  fi
  if [[ "$(k3s_mode)" == "server" && "$CLOUD_INIT_K3S_ALLOW_SD_ETCD" != "true" ]]; then
    echo "NOTE: this node installs a K3s server. On first boot it refuses to install if its root is an SD card (etcd needs an SSD); set CLOUD_INIT_K3S_ALLOW_SD_ETCD=true to override." >&2
  fi
fi
if [[ ! "$CLOUD_INIT_USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
  echo "Invalid CLOUD_INIT_USERNAME value: ${CLOUD_INIT_USERNAME}" >&2
  echo "Allowed: lowercase letters, numbers, '_' or '-' (must start with a letter or underscore)." >&2
  exit 1
fi

if [[ -n "$CLOUD_INIT_SSH_PORT" ]]; then
  if [[ ! "$CLOUD_INIT_SSH_PORT" =~ ^[0-9]+$ ]] || (( CLOUD_INIT_SSH_PORT < 1 || CLOUD_INIT_SSH_PORT > 65535 )); then
    echo "Invalid CLOUD_INIT_SSH_PORT value: ${CLOUD_INIT_SSH_PORT}" >&2
    exit 1
  fi
fi

if [[ -n "$CLOUD_INIT_SSH_ALLOW_GROUPS" ]]; then
  if [[ ! "$CLOUD_INIT_SSH_ALLOW_GROUPS" =~ ^[A-Za-z0-9_,.-]+([[:space:]]*[A-Za-z0-9_,.-]+)*$ ]]; then
    echo "Invalid CLOUD_INIT_SSH_ALLOW_GROUPS value: ${CLOUD_INIT_SSH_ALLOW_GROUPS}" >&2
    exit 1
  fi
fi

if [[ -n "$CLOUD_INIT_EXTRA_SSH_KEYS" ]]; then
  validate_ssh_public_key_lines "CLOUD_INIT_EXTRA_SSH_KEYS" "$CLOUD_INIT_EXTRA_SSH_KEYS"
fi

USER_PASSWORD_HASH=""
if [[ -n "$CLOUD_INIT_USER_PASSWORD" ]]; then
  USER_PASSWORD_HASH="$(printf '%s' "$CLOUD_INIT_USER_PASSWORD" | openssl passwd -6 -stdin)"
fi

PROFILE_ROLE="pi5"
PROFILE_STORAGE="nvme"
PROFILE_PACKAGES="qemu-guest-agent curl git python3 python3-apt htop nvme-cli lshw"
if [[ "$PROFILE" == "pi3bplus" ]]; then
  PROFILE_ROLE="pi-3bplus"
  PROFILE_STORAGE="sdcard"
  PROFILE_PACKAGES="curl git python3 python3-apt htop lshw"
elif [[ "$PROFILE" == "pi4" ]]; then
  # SD card or USB SSD; K3s control-plane needs the SSD.
  PROFILE_ROLE="pi4"
  PROFILE_STORAGE="sd-or-usb"
  PROFILE_PACKAGES="curl git python3 python3-apt htop lshw"
elif [[ "$PROFILE" == "network-toolkit" ]]; then
  PROFILE_ROLE="network-toolkit"
  PROFILE_STORAGE="sdcard"
  PROFILE_PACKAGES="$(toolkit_render packages)"
elif [[ "$PROFILE" == "playbook-server" ]]; then
  PROFILE_ROLE="playbook-server"
  PROFILE_STORAGE="disk"
  PROFILE_PACKAGES="qemu-guest-agent htop lshw python3-apt $(server_render packages)"
fi

PROFILE_PACKAGES="$PROFILE_PACKAGES $CLOUD_INIT_EXTRA_PACKAGES"
if [[ "$CLOUD_INIT_CONFIGURE_NEOVIM" == "true" ]]; then
  PROFILE_PACKAGES="$PROFILE_PACKAGES neovim"
fi
if [[ "$CLOUD_INIT_INSTALL_CLI_TOOLS" == "true" ]]; then
  PROFILE_PACKAGES="$PROFILE_PACKAGES tmux ripgrep fd-find fzf jq unzip bash-completion"
fi

# Ubuntu 21.10 to 23.10 moved the vxlan module (flannel's overlay) out of the base raspi kernel package.
if [[ "$DISTRIBUTION" == "k3s" && "${IMAGE_URL:-${IMAGE_FILE:-}}" =~ ubuntu-(21\.10|22\.04|23\.04|23\.10) ]]; then
  PROFILE_PACKAGES="$PROFILE_PACKAGES linux-modules-extra-raspi"
fi
