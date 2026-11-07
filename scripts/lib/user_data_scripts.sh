# write_files for the node-side scripts (callback, hardware checks, Telegraf, MOTD).
# Sourced by prepare_sd_cloud_init.sh; shares its shell, so it is not run on its own.

cat >> "$USER_DATA_OUT" <<EOF
  - path: /usr/local/bin/rack-local-ip
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      ip -4 -o addr show scope global up 2>/dev/null | awk '
        {
          split(\$4, a, "/");
          if (a[1] != "" && a[1] !~ /^169\.254\./) {
            print a[1];
            exit
          }
        }
      '
      if [[ -z "\$(ip -4 -o addr show scope global up 2>/dev/null | awk '{split(\$4, a, "/"); if (a[1] != "" && a[1] !~ /^169\\.254\\./) {print a[1]; exit}}')" ]]; then
        hostname -I 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if (\$i !~ /^127\\./ && \$i !~ /^169\\.254\\./) {print \$i; exit}}'
      fi
  - path: /usr/local/bin/rack-hardware-check
    permissions: "0755"
    content: |
      #!/bin/bash
      # Rack hardware detection and reporting
      PI_MODEL="\$(cat /proc/device-tree/model 2>/dev/null | tr -d '\0' || echo 'unknown')"
      ARCHITECTURE="\$(uname -m)"
      RAM_MB="\$(awk '/MemTotal/ {print int(\$2/1024)}' /proc/meminfo 2>/dev/null || echo '0')"
      SD_CAPACITY="\$(lsblk -b --output SIZE -n /dev/mmcblk0 2>/dev/null | awk '{print int(\$1/1024/1024/1024)}' || echo '0')"
      NVME_PRESENT="false"
      [[ -b /dev/nvme0n1 ]] && NVME_PRESENT="true"
      HAT_PRESENT="false"
      i2cdetect -y 1 2>/dev/null | grep -q "50\\|51" && HAT_PRESENT="true"
      POE_ENABLED="false"
      [[ -f /sys/class/pwm/pwmchip0/device/dtoverlay ]] && POE_ENABLED="true"
      SOURCE_IP="\$(/usr/local/bin/rack-local-ip 2>/dev/null || true)"
      NODE_REPORT_B64="\$(/usr/local/bin/rack-node-report 2>/dev/null | base64 -w0 || true)"
      PAYLOAD_JSON="\$(PI_MODEL=\"\$PI_MODEL\" ARCHITECTURE="\$ARCHITECTURE" RAM_MB=\"\$RAM_MB\" SD_CAPACITY=\"\$SD_CAPACITY\" NVME_PRESENT=\"\$NVME_PRESENT\" HAT_PRESENT=\"\$HAT_PRESENT\" POE_ENABLED=\"\$POE_ENABLED\" SOURCE_IP=\"\$SOURCE_IP\" NODE_REPORT_B64=\"\$NODE_REPORT_B64\" PROFILE='${PROFILE}' python3 <<'PY'
      import json
      import os
      import socket


      def as_int(value: str) -> int:
        try:
          return int(float(str(value or "0").strip()))
        except (TypeError, ValueError):
          return 0


      def as_bool(value: str) -> bool:
        return str(value or "").strip().lower() == "true"


      payload = {
        "instance_id": f"{socket.gethostname()}-hw",
        "hostname": socket.gethostname(),
        "profile": os.getenv("PROFILE", "pi5"),
        "source_ip": os.getenv("SOURCE_IP", ""),
        "stage": "hardware-check",
        "node_report_b64": os.getenv("NODE_REPORT_B64", ""),
        "hardware": {
          "model": os.getenv("PI_MODEL", "unknown"),
          "architecture": os.getenv("ARCHITECTURE", ""),
          "ram_mb": as_int(os.getenv("RAM_MB", "0")),
          "sd_capacity_gb": as_int(os.getenv("SD_CAPACITY", "0")),
          "nvme": as_bool(os.getenv("NVME_PRESENT", "false")),
          "hat": as_bool(os.getenv("HAT_PRESENT", "false")),
          "poe": as_bool(os.getenv("POE_ENABLED", "false")),
        },
      }

      print(json.dumps(payload, separators=(",", ":")))
      PY
      )"
      curl -fsS -X POST -H 'Content-Type: application/json' \\
        --data-binary "\$PAYLOAD_JSON" \
        '${CALLBACK_URL}' || true
  - path: /usr/local/bin/rack-callback-payload
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      python3 - <<'PY'
      import json
      import os

      payload = {
        "instance_id": os.environ.get("INSTANCE_ID", ""),
        "hostname": os.environ.get("HOSTNAME", ""),
        "profile": os.environ.get("PROFILE", ""),
        "source_ip": os.environ.get("SOURCE_IP", ""),
        "stage": os.environ.get("STAGE", "runcmd"),
        "node_report_b64": os.environ.get("NODE_REPORT_B64", ""),
      }
      print(json.dumps(payload, separators=(",", ":")))
      PY
  - path: /usr/local/bin/rack-power-restored-callback
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      SOURCE_IP="\$(/usr/local/bin/rack-local-ip 2>/dev/null || true)"
      NODE_REPORT_B64="\$(/usr/local/bin/rack-node-report 2>/dev/null | base64 -w0 || true)"
      # Generated payload includes the stage field: {"stage": "power_restore"}
      PAYLOAD_JSON="\$(INSTANCE_ID="\${INSTANCE_ID:-\$(hostname)}" HOSTNAME="\${HOSTNAME:-\$(hostname)}" PROFILE="\${PROFILE:-}" SOURCE_IP="\${SOURCE_IP}" NODE_REPORT_B64="\${NODE_REPORT_B64}" STAGE="power_restore" /usr/local/bin/rack-callback-payload)"
      if [[ -n "\${CALLBACK_URL:-}" ]]; then
        curl --retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors --connect-timeout 5 --max-time 20 -fsS -X POST -H "Content-Type: application/json" -d "\${PAYLOAD_JSON}" "\${CALLBACK_URL}" || true
      fi
      /usr/local/bin/rack-boot-log power_restore || true
  - path: /usr/local/bin/rack-boot-log
    permissions: "0755"
    content: |
      #!/bin/bash
      # Sends this node's boot and cloud-init logs to the callback site. The node opens the connection, so it works behind NAT.
      if [[ -r /etc/rack-node.env ]]; then
        . /etc/rack-node.env
      fi
      BOOT_LOG_URL="\${CALLBACK_URL:-${CALLBACK_URL}}"
      BOOT_LOG_URL="\${BOOT_LOG_URL%/callback}/boot-log"
      STAGE="\${1:-boot-log}" INSTANCE_ID="\${INSTANCE_ID:-}" PROFILE="\${PROFILE:-}" python3 <<'PY' | curl --retry 2 --retry-delay 2 --connect-timeout 5 --max-time 30 -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "\$BOOT_LOG_URL" || true
      import json
      import os
      import socket
      import subprocess

      SECTIONS = [
        ("node", "hostname; uname -a; uptime -s"),
        ("cloud-init status", "cloud-init status --long 2>&1"),
        ("systemctl --failed", "systemctl --failed --no-pager 2>&1"),
        ("journalctl -b (last 1500)", "journalctl -b --no-pager -o short-iso -n 1500 2>&1"),
        ("/var/log/cloud-init.log (last 800)", "tail -n 800 /var/log/cloud-init.log 2>&1"),
        ("/var/log/cloud-init-output.log (last 800)", "tail -n 800 /var/log/cloud-init-output.log 2>&1"),
        ("dmesg (last 400)", "dmesg --time-format iso 2>&1 | tail -n 400"),
        ("tailscale status", "tailscale status 2>&1 | head -n 20"),
      ]


      def run(cmd):
        try:
          return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60).stdout
        except Exception:
          return ""


      text = "".join(f"=== {title} ===\n{run(cmd)}\n" for title, cmd in SECTIONS)
      print(json.dumps({
        "hostname": socket.gethostname(),
        "instance_id": os.environ.get("INSTANCE_ID", ""),
        "profile": os.environ.get("PROFILE", ""),
        "stage": os.environ.get("STAGE", "boot-log"),
        "log": text[-600000:],
      }, separators=(",", ":")))
      PY
  - path: /etc/systemd/system/rack-power-restored-callback.service
    permissions: "0644"
    content: |
      [Unit]
      Description=Rack node callback after power restore
      Wants=network-online.target
      After=network-online.target

      [Service]
      Type=oneshot
      Environment="CALLBACK_URL=${CALLBACK_URL}"
      Environment="PROFILE=${PROFILE}"
      Environment="INSTANCE_ID=${INSTANCE_ID}"
      ExecStart=/usr/local/bin/rack-power-restored-callback

      [Install]
      WantedBy=multi-user.target
  - path: /usr/local/bin/rack-node-report
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      python3 <<'PY'
      import json
      import subprocess
      from datetime import datetime, timezone


      def run_text(cmd):
        try:
          out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
          return out.strip()
        except Exception:
          return ""


      def run_json(cmd):
        text = run_text(cmd)
        if not text:
          return {}
        try:
          return json.loads(text)
        except json.JSONDecodeError:
          return {"raw": text[:20000]}


      report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hostname": run_text(["hostname"]),
        "kernel": run_text(["uname", "-a"]),
        "os_release": run_text(["bash", "-lc", "cat /etc/os-release 2>/dev/null"]),
        "cpu": run_text(["bash", "-lc", "lscpu 2>/dev/null"]),
        "memory": run_text(["bash", "-lc", "free -h 2>/dev/null"]),
        "storage": run_text(["bash", "-lc", "lsblk -e7 -o NAME,SIZE,TYPE,MODEL,MOUNTPOINT 2>/dev/null"]),
        "network": run_text(["bash", "-lc", "ip -brief address 2>/dev/null"]),
        "lshw": run_json(["bash", "-lc", "lshw -json 2>/dev/null"]),
      }
      print(json.dumps(report, separators=(",", ":")))
      PY
  - path: /usr/local/bin/rack-bootstrap-summary
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      if [[ -r /etc/rack-node.env ]]; then
        . /etc/rack-node.env
      fi

      if [[ "\${CLOUD_INIT_BOOT_SUMMARY}" != "true" ]]; then
        exit 0
      fi

      reset=\$'\033[0m'
      bold=\$'\033[1m'
      red=\$'\033[31m'
      yellow=\$'\033[33m'
      green=\$'\033[32m'
      cyan=\$'\033[36m'
      magenta=\$'\033[35m'

      print_border() {
        printf '%s+--------------------------------------------------------------+%s\n' "\$cyan" "\$reset"
      }

      print_row() {
        local color=\$1
        local key=\$2
        local value=\$3
        printf '%s|%s %-24s %s| %s%-28s%s %s|%s\n' "\$color" "\$reset" "\$key" "\$color" "\$bold" "\$value" "\$reset" "\$color" "\$reset"
      }

      print_border
      printf '%s|%s %-58s %s|%s\n' "\$red" "\$reset" "\$bold RACK BOOT COMPLETE\$reset" "\$red" "\$reset"
      print_border
      print_row "\$red" "Hostname" "\$(hostname)"
      print_row "\$yellow" "Profile" "\${PROFILE}"
      print_row "\$green" "Role" "\${PROFILE_ROLE}"
      print_row "\$cyan" "Storage" "\${PROFILE_STORAGE}"
      print_row "\$magenta" "Distribution" "\${DISTRIBUTION}"
      print_row "\$red" "User" "\${CLOUD_INIT_USERNAME}"
      print_row "\$yellow" "SSH password auth" "\${CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH}"
      print_row "\$green" "Root SSH login" "\${CLOUD_INIT_DISABLE_ROOT_SSH}"
      print_row "\$cyan" "Boot summary" "\${CLOUD_INIT_BOOT_SUMMARY}"
      print_row "\$magenta" "Fail2ban" "\${CLOUD_INIT_ENABLE_FAIL2BAN}"
      print_row "\$red" "Unattended upgrades" "\${CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES}"
      print_row "\$yellow" "Hardware checks" "\${CLOUD_INIT_ENABLE_HARDWARE_CHECKS:-false}"
      print_row "\$green" "Sysctl hardening" "\${CLOUD_INIT_ENABLE_SYSCTL_HARDENING:-false}"
      print_row "\$cyan" "Firewall baseline" "\${CLOUD_INIT_ENABLE_FIREWALL_BASELINE:-false}"
      print_row "\$magenta" "SELinux enforcing" "\${CLOUD_INIT_ENABLE_SELINUX_ENFORCING:-false}"
      print_row "\$green" "Fancy MOTD" "\${CLOUD_INIT_FANCY_MOTD:-false}"
      print_row "\$cyan" "Telegraf" "\${CLOUD_INIT_INSTALL_TELEGRAF:-false}"
      k3s_config_state="not set"
      if [[ -n "\${CLOUD_INIT_K3S_CONFIG:-}" ]]; then
        k3s_config_state="present"
      fi
      print_row "\$magenta" "K3s config" "\${k3s_config_state}"
      print_row "\$red" "SSH port" "\${CLOUD_INIT_SSH_PORT:-22}"
      print_row "\$yellow" "SSH allow groups" "\${CLOUD_INIT_SSH_ALLOW_GROUPS:-default}"
      print_row "\$green" "Callback URL" "\${CALLBACK_URL}"
      print_border
      printf '%s%sAll configured steps finished.%s\n' "\$bold" "\$green" "\$reset"
      printf '%s%sYou can now continue from the portal.%s\n' "\$bold" "\$cyan" "\$reset"
  - path: /etc/update-motd.d/10-fancy
    permissions: "0755"
    content: |
      #!/bin/bash
      if [[ -r /etc/rack-node.env ]]; then
        . /etc/rack-node.env
      fi
      if [[ "\${CLOUD_INIT_FANCY_MOTD:-false}" != "true" ]]; then
        exit 0
      fi
      if command -v fortune >/dev/null 2>&1 && command -v cowsay >/dev/null 2>&1; then
        if command -v lolcat >/dev/null 2>&1; then
          fortune | cowsay | lolcat 2>/dev/null || fortune | cowsay
        else
          fortune | cowsay
        fi
      else
        echo "Rack node online: \$(hostname)"
      fi
  - path: /usr/local/bin/rack-telegraf-bootstrap
    permissions: "0755"
    content: |
      #!/bin/bash
      set -euo pipefail
      if [[ -r /etc/rack-node.env ]]; then
        . /etc/rack-node.env
      fi

      INFLUX_URL="\${CLOUD_INIT_TELEGRAF_INFLUX_URL:-}"
      INFLUX_TOKEN="\${CLOUD_INIT_TELEGRAF_INFLUX_TOKEN:-}"
      INFLUX_ORG="\${CLOUD_INIT_TELEGRAF_INFLUX_ORG:-}"
      INFLUX_BUCKET="\${CLOUD_INIT_TELEGRAF_INFLUX_BUCKET:-}"
      CONFIG_URL="\${CLOUD_INIT_TELEGRAF_CONFIG_URL:-}"

      mkdir -p /etc/telegraf

      if [[ -n "\$CONFIG_URL" ]]; then
        if [[ -n "\$INFLUX_TOKEN" ]]; then
          curl -fsSL -H "Authorization: Token \$INFLUX_TOKEN" "\$CONFIG_URL" -o /etc/telegraf/telegraf.conf
        else
          curl -fsSL "\$CONFIG_URL" -o /etc/telegraf/telegraf.conf
        fi
      elif [[ -n "\$INFLUX_URL" && -n "\$INFLUX_TOKEN" && -n "\$INFLUX_ORG" && -n "\$INFLUX_BUCKET" ]]; then
        cat > /etc/telegraf/telegraf.conf <<TELEGRAF_CONF
        [agent]
          interval = "10s"
          round_interval = true
          metric_batch_size = 1000
          metric_buffer_limit = 10000
          flush_interval = "10s"
          hostname = "\$(hostname)"

        [[outputs.influxdb_v2]]
          urls = ["\$INFLUX_URL"]
          token = "\$INFLUX_TOKEN"
          organization = "\$INFLUX_ORG"
          bucket = "\$INFLUX_BUCKET"

        [[inputs.cpu]]
          percpu = true
          totalcpu = true

        [[inputs.mem]]
        [[inputs.disk]]
          ignore_fs = ["tmpfs", "devtmpfs", "overlay", "squashfs"]
        [[inputs.diskio]]
        [[inputs.net]]
        [[inputs.system]]
      TELEGRAF_CONF
      else
        echo "Telegraf enabled but no config_url or complete Influx credentials provided; skipping config"
        exit 0
      fi

      # The config can hold the Influx token: root and the telegraf group only.
      chgrp telegraf /etc/telegraf/telegraf.conf 2>/dev/null || true
      chmod 0640 /etc/telegraf/telegraf.conf || true

      systemctl enable telegraf || true
      systemctl restart telegraf || true

phone_home:
  url: "${CALLBACK_URL}"
  post: [instance_id, hostname, fqdn]
  tries: 15

runcmd:
  - [ systemctl, enable, ssh ]
  - [ systemctl, restart, ssh ]
EOF
