"""Ansible config, vault layout, role tags and the SD prepare script."""

from __future__ import annotations

import configparser
import yaml
from pathlib import Path

try:
    from .test_support import BaseCallbackSiteTest, APP_DIR
except ImportError:
    from test_support import BaseCallbackSiteTest, APP_DIR
try:
    from .sd_script import read_sd_script
except ImportError:
    from sd_script import read_sd_script


class RepoConfigTests(BaseCallbackSiteTest):
    def test_docker_image_includes_the_node_ops_playbook(self) -> None:
        dockerfile = (APP_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY ansible.cfg inventory.yml site.yml /app/", dockerfile)

    def test_shell_scripts_use_unix_line_endings(self) -> None:
        root = APP_DIR.parent
        attributes = (root / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attributes)
        for script in (root / "scripts").rglob("*.sh"):
            with self.subTest(script=script.name):
                self.assertNotIn(b"\r\n", script.read_bytes())

    def test_pi_cloud_init_examples_default_to_passwordless_sudo(self) -> None:
        root = Path(__file__).resolve().parents[2]
        example = yaml.safe_load((root / "cloud-init/pi5-user-data.example.yml").read_text())
        self.assertEqual(example["users"][1]["sudo"], "ALL=(ALL) NOPASSWD: ALL")
        self.assertIn("010_ops-nopasswd", example["runcmd"][0][2])
        legacy = yaml.safe_load((root / "pi_config/user-data").read_text())
        self.assertEqual(legacy["users"], ["default"])
        self.assertEqual(legacy["system_info"]["default_user"]["sudo"], "ALL=(ALL) NOPASSWD: ALL")

    def test_ansible_cfg_disables_vault_prompting(self) -> None:
        config = configparser.ConfigParser()
        config.read(Path(__file__).resolve().parents[2] / "ansible.cfg")
        defaults = config["defaults"]
        self.assertEqual(defaults.get("ask_vault_pass", "").lower(), "false")
        self.assertNotIn("vault_identity_list", defaults)

    def test_group_vars_all_vault_file_is_plain_yaml(self) -> None:
        vault_path = Path(__file__).resolve().parents[2] / "group_vars" / "all" / "vault.yml"
        self.assertTrue(vault_path.exists())
        vault_text = vault_path.read_text(encoding="utf-8")
        self.assertNotIn("$ANSIBLE_VAULT", vault_text)
        self.assertIn("vault_maas_api_key", vault_text)

    def test_pi_roles_tag_tasks_for_profile_scoped_runs(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        for role in ("raspberry_pi5", "raspberry_pi_3bplus"):
            tasks_text = (repo_root / "roles" / role / "tasks" / "main.yml").read_text(encoding="utf-8")
            self.assertIn("- tailscale", tasks_text, msg=f"{role} must tag tailscale tasks")
            self.assertIn("- baseline", tasks_text, msg=f"{role} must tag baseline tasks")
            self.assertIn("- kubernetes", tasks_text, msg=f"{role} must tag kubernetes tasks")
            self.assertIn("Resolve Tailscale provisioning inputs", tasks_text, msg=f"{role} must consume provision_inputs")
            if role == "raspberry_pi5":
                self.assertIn("provision_inputs.k3s_config", tasks_text, msg="raspberry_pi5 must apply worker config overrides")
                self.assertIn("Refuse implicit K3s server-to-worker conversion", tasks_text)
                self.assertNotIn("provision_inputs.pod_cidr", tasks_text)
            else:
                self.assertIn("provision_inputs.server_endpoint", tasks_text, msg="raspberry_pi_3bplus must use structured K3s inputs")
                self.assertIn("provision_inputs.k3s_config", tasks_text, msg="raspberry_pi_3bplus must support full config overrides")
                self.assertIn("Set kernel values required by K3s protected defaults", tasks_text)
                self.assertIn("Refuse implicit K3s agent-to-server conversion", tasks_text)
                self.assertNotIn("node-role.kubernetes.io/control-plane=true", tasks_text)

    def test_prepare_script_escapes_embedded_runtime_vars(self) -> None:
        script_text = read_sd_script()
        self.assertIn('\\$PI_MODEL', script_text)
        self.assertIn('\\$RAM_MB', script_text)
        self.assertIn('PAYLOAD_JSON="\\$(PI_MODEL=\\"\\$PI_MODEL\\"', script_text)
        self.assertIn("python3 <<'PY'", script_text)
        self.assertIn('print(json.dumps(payload, separators=(",", ":")))', script_text)
        self.assertIn('--data-binary "\\$PAYLOAD_JSON"', script_text)
        self.assertIn('SOURCE_IP="\\$(/usr/local/bin/rack-local-ip', script_text)
        self.assertIn('PAYLOAD_JSON="\\$(INSTANCE_ID="\\${INSTANCE_ID:-\\$(hostname)}"', script_text)
        self.assertIn('if [[ -n "\\${CALLBACK_URL:-}" ]]; then', script_text)
        self.assertIn('-d "\\${PAYLOAD_JSON}" "\\${CALLBACK_URL}" || true', script_text)
        self.assertIn('INFLUX_URL="\\${CLOUD_INIT_TELEGRAF_INFLUX_URL:-}"', script_text)
        self.assertIn('CLOUD_INIT_USERNAME="${CLOUD_INIT_USERNAME:-jdizzle}"', script_text)
        self.assertIn('CLOUD_INIT_USER_PASSWORD="${CLOUD_INIT_USER_PASSWORD:-}"', script_text)
        self.assertIn('CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH="${CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH:-false}"', script_text)
        self.assertIn('CLOUD_INIT_DISABLE_ROOT_SSH="${CLOUD_INIT_DISABLE_ROOT_SSH:-false}"', script_text)
        self.assertIn('CLOUD_INIT_BOOT_SUMMARY="${CLOUD_INIT_BOOT_SUMMARY:-true}"', script_text)
        self.assertIn('CLOUD_INIT_ENABLE_FAIL2BAN="${CLOUD_INIT_ENABLE_FAIL2BAN:-false}"', script_text)
        self.assertIn('CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES="${CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES:-false}"', script_text)
        self.assertIn('CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK="${CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK:-true}"', script_text)
        self.assertIn('validate_http_url "--callback-url" "$CALLBACK_URL"', script_text)
        self.assertIn('validate_ssh_public_key_lines "--ssh-key-file" "$SSH_KEY_CONTENT"', script_text)
        self.assertIn('Invalid CLOUD_INIT_USERNAME value', script_text)
        self.assertIn('Invalid CLOUD_INIT_SSH_PORT value', script_text)
        self.assertIn('Invalid CLOUD_INIT_SSH_ALLOW_GROUPS value', script_text)
        self.assertIn('validate_ssh_public_key_lines "CLOUD_INIT_EXTRA_SSH_KEYS" "$CLOUD_INIT_EXTRA_SSH_KEYS"', script_text)
        self.assertIn('role=worker', script_text)
        self.assertIn('/etc/fail2ban/jail.d/rack-sshd.local', script_text)
        self.assertIn('systemctl enable --now fail2ban || true', script_text)
        self.assertIn('PermitRootLogin no', script_text)
        self.assertIn('PasswordAuthentication no', script_text)
        self.assertIn("if [ -f /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys ]", script_text)
        self.assertIn('require_cmd openssl', script_text)
        self.assertIn("openssl passwd -6 -stdin", script_text)
        self.assertIn('--retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors --connect-timeout 5 --max-time 20', script_text)
        self.assertIn('rack callback POST failed after retries to $CALLBACK_URL', script_text)
        self.assertIn('if [[ "$CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK" == "true" ]]; then', script_text)
        self.assertIn('if [[ "\\${CLOUD_INIT_BOOT_SUMMARY}" != "true" ]]; then', script_text)
        self.assertIn('printf \'%s+--------------------------------------------------------------+%s\\n\' "\\$cyan" "\\$reset"', script_text)
        self.assertIn('print_row "\\$red" "Hostname" "\\$(hostname)"', script_text)
        self.assertIn('/usr/local/bin/rack-bootstrap-summary', script_text)
        self.assertIn('VALIDATE_CLOUD_INIT_SCHEMA="${CLOUD_INIT_VALIDATE_SCHEMA:-true}"', script_text)
        self.assertIn('cloud-init schema --config-file "$USER_DATA_OUT"', script_text)
        self.assertIn("cloud-init devel schema --config-file \"$USER_DATA_OUT\"", script_text)
        self.assertIn('usb_max_current_enable=1', script_text)
        self.assertIn('cat >> "$USER_DATA_OUT" <<RUNCMD_EOF', script_text)
        self.assertIn('final_message: "Cloud-init finished for ${HOSTNAME}"', script_text)

    def test_prepare_script_retries_callback_on_boot_after_power_restore(self) -> None:
        script_text = read_sd_script()
        self.assertIn('rack-power-restored-callback', script_text)
        self.assertIn('WantedBy=multi-user.target', script_text)
        self.assertIn('stage": "power_restore"', script_text)
