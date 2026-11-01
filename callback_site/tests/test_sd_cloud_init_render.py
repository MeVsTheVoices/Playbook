from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare_sd_cloud_init.sh"
AUTH_KEY = "tskey-auth-kTEST123CNTRL-abcdef"
K3S_SERVER_CONFIG = "server: https://100.65.24.76:6443\ntoken: SECRETTOKEN\nnode-name: render-test\n"
K3S_WORKER_CONFIG = "server: https://100.65.24.76:6443\ntoken: SECRETTOKEN\nnode-label:\n  - role=worker\n"


def _harness_text() -> str:
    """The user-data rendering section of the real script, without any device access."""
    text = SCRIPT.read_text(encoding="utf-8")
    func = re.search(r"^validate_ssh_public_key_lines\(\) \{\n.*?^\}\n", text, re.S | re.M)
    body = re.search(r"^CLOUD_INIT_USERNAME=.*?(?=^cat > \"\$META_DATA_OUT\")", text, re.S | re.M)
    assert func and body, "user-data rendering section not found in prepare_sd_cloud_init.sh"
    return (
        "set -euo pipefail\n"
        + func.group(0)
        + 'PROFILE="${R_PROFILE:-pi3bplus}"\nHOSTNAME="render-test"\nDISTRIBUTION="${R_DIST:-k3s}"\n'
        'CALLBACK_URL="http://192.168.174.1:8088/api/cloud-init/callback"\n'
        'SSH_KEY_CONTENT="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest test"\n'
        'DEFAULT_K3S_SERVER_HOST="100.65.24.76"\nVALIDATE_CLOUD_INIT_SCHEMA=false\n'
        'DEFAULT_K3S_CONFIG="${CLOUD_INIT_K3S_CONFIG}"\n'
        'USER_DATA_OUT="$R_OUT"\nINSTANCE_ID="render-test-1"\n'
        + body.group(0)
    )


class SdCloudInitRenderTests(unittest.TestCase):
    def render(self, **env: str) -> tuple[int, dict, str]:
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.sh"
            harness.write_text(_harness_text(), encoding="utf-8")
            out = Path(tmp) / "user-data"
            run_env = {"PATH": os.environ["PATH"], "R_OUT": str(out), "CLOUD_INIT_K3S_CONFIG": K3S_SERVER_CONFIG, **env}
            result = subprocess.run(["bash", str(harness)], capture_output=True, text=True, env=run_env, timeout=60)
            data = yaml.safe_load(out.read_text(encoding="utf-8")) if result.returncode == 0 and out.exists() else {}
            return result.returncode, data, result.stderr

    @staticmethod
    def files(data: dict) -> dict:
        return {f["path"]: f for f in data["write_files"]}

    @staticmethod
    def runcmd(data: dict) -> list[str]:
        return [c if isinstance(c, str) else " ".join(c) for c in data["runcmd"]]

    def test_every_embedded_script_parses(self) -> None:
        code, data, err = self.render(
            CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY,
            CLOUD_INIT_INSTALL_TELEGRAF="true", CLOUD_INIT_ENABLE_HARDWARE_CHECKS="true", CLOUD_INIT_FANCY_MOTD="true",
        )
        self.assertEqual(code, 0, err)
        checked = 0
        for path, entry in self.files(data).items():
            content = entry.get("content", "")
            if content.startswith("#!/bin/bash"):
                result = subprocess.run(["bash", "-n"], input=content, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, f"{path}: {result.stderr[:300]}")
                checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_final_callback_runs_at_boot_with_real_values_not_literal_escapes(self) -> None:
        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        command = next(c for c in self.runcmd(data) if "rack-callback-payload" in c and "curl" in c)
        self.assertNotIn("\\$", command, "literal backslash-dollar would run on the node as text")
        self.assertIn(". /etc/rack-node.env", command)
        self.assertIn('"$CALLBACK_URL"', command)
        env = self.files(data)["/etc/rack-node.env"]["content"]
        self.assertIn("CALLBACK_URL=http://192.168.174.1:8088/api/cloud-init/callback", env)
        self.assertIn("INSTANCE_ID=render-test-1", env)

    def test_boot_summary_runs_cleanly_with_the_baked_env(self) -> None:
        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        files = self.files(data)
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "node.env"
            env_file.write_text(files["/etc/rack-node.env"]["content"], encoding="utf-8")
            script = Path(tmp) / "summary.sh"
            script.write_text(
                files["/usr/local/bin/rack-bootstrap-summary"]["content"].replace("/etc/rack-node.env", str(env_file)),
                encoding="utf-8",
            )
            result = subprocess.run(["bash", str(script)], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://192.168.174.1:8088/api/cloud-init/callback", result.stdout)

    def test_k3s_token_and_auth_key_never_land_in_the_env_file(self) -> None:
        code, data, err = self.render(CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY)
        self.assertEqual(code, 0, err)
        files = self.files(data)
        env = files["/etc/rack-node.env"]["content"]
        self.assertNotIn("SECRETTOKEN", env)
        self.assertNotIn("tskey", env)
        self.assertEqual(files["/etc/rack-node.env"]["permissions"], "0600")
        self.assertEqual(files["/etc/rack-tailscale-authkey"]["permissions"], "0600")
        self.assertEqual(files["/etc/rancher/k3s/config.yaml"]["permissions"], "0600", "config holds the cluster token")

    def test_tailscale_runs_before_k3s_install(self) -> None:
        code, data, err = self.render(CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY)
        self.assertEqual(code, 0, err)
        cmds = self.runcmd(data)
        ts = next(i for i, c in enumerate(cmds) if "rack-tailscale-bootstrap" in c)
        k3s = next(i for i, c in enumerate(cmds) if "get.k3s.io" in c)
        self.assertLess(ts, k3s)

    def test_default_tags_follow_the_profile(self) -> None:
        for profile, expected in (("pi3bplus", "tag:rpi-3bplus,tag:homelab"), ("pi5", "tag:rpi5,tag:homelab")):
            code, data, err = self.render(R_PROFILE=profile, CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY)
            self.assertEqual(code, 0, err)
            env = self.files(data)["/etc/rack-node.env"]["content"]
            self.assertIn(f"CLOUD_INIT_TAILSCALE_TAGS={expected.replace(',', chr(92) + ',')}", env)

    def k3s_install_command(self, **env: str) -> str:
        code, data, err = self.render(**env)
        self.assertEqual(code, 0, err)
        return next(c for c in self.runcmd(data) if "get.k3s.io" in c)

    def test_explicit_mode_beats_the_config_text_guess(self) -> None:
        light_worker_config = "server: https://100.65.24.76:6443\nnode-label:\n  - role=light-worker\n"
        # role=light-worker does not match the old 'role=worker' guess, so the mode must be explicit.
        guessed = self.k3s_install_command(CLOUD_INIT_K3S_CONFIG=light_worker_config)
        self.assertIn("server --with-node-id", guessed)
        explicit = self.k3s_install_command(CLOUD_INIT_K3S_CONFIG=light_worker_config, CLOUD_INIT_K3S_MODE="agent")
        self.assertIn("agent --with-node-id", explicit)
        self.assertNotIn("server --with-node-id", explicit)
        forced_server = self.k3s_install_command(
            CLOUD_INIT_K3S_CONFIG="server: https://x:6443\nnode-label:\n  - role=worker\n", CLOUD_INIT_K3S_MODE="server"
        )
        self.assertIn("server --with-node-id", forced_server)

    def test_unset_mode_still_guesses_from_worker_markers(self) -> None:
        self.assertIn("agent --with-node-id", self.k3s_install_command(CLOUD_INIT_K3S_CONFIG=K3S_WORKER_CONFIG))
        self.assertIn("server --with-node-id", self.k3s_install_command(CLOUD_INIT_K3S_CONFIG=K3S_SERVER_CONFIG))

    def test_invalid_mode_is_rejected(self) -> None:
        code, _data, err = self.render(CLOUD_INIT_K3S_MODE="controller")
        self.assertNotEqual(code, 0)
        self.assertIn("CLOUD_INIT_K3S_MODE", err)

    def test_agent_mode_env_tells_the_tailscale_script_to_skip_advertise_address(self) -> None:
        code, data, err = self.render(
            CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY, CLOUD_INIT_K3S_MODE="agent",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("RACK_K3S_MODE=agent", self.files(data)["/etc/rack-node.env"]["content"])

    def test_tailscale_disabled_adds_no_tailscale_artifacts(self) -> None:
        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        self.assertNotIn("/usr/local/bin/rack-tailscale-bootstrap", self.files(data))
        self.assertFalse(any("rack-tailscale" in c for c in self.runcmd(data)))

    def test_bad_tailscale_inputs_are_rejected_before_anything_is_written(self) -> None:
        cases = (
            {"CLOUD_INIT_TAILSCALE_ENABLE": "true"},  # enabled with no key
            {"CLOUD_INIT_TAILSCALE_ENABLE": "true", "CLOUD_INIT_TAILSCALE_AUTH_KEY": "not-a-key"},
            {"CLOUD_INIT_TAILSCALE_ENABLE": "true", "CLOUD_INIT_TAILSCALE_AUTH_KEY": AUTH_KEY, "CLOUD_INIT_TAILSCALE_TAGS": "tag:ok;rm -rf /"},
            {"CLOUD_INIT_TAILSCALE_ENABLE": "true", "CLOUD_INIT_TAILSCALE_AUTH_KEY": AUTH_KEY, "CLOUD_INIT_TAILSCALE_ROUTES": "10.0.0.0/8 && id"},
            {"CLOUD_INIT_TAILSCALE_ENABLE": "true", "CLOUD_INIT_TAILSCALE_AUTH_KEY": AUTH_KEY, "CLOUD_INIT_TAILSCALE_HOSTNAME": "bad host"},
        )
        for env in cases:
            with self.subTest(env=env):
                code, _data, _err = self.render(**env)
                self.assertNotEqual(code, 0)


class TailscaleBootstrapBehaviorTests(unittest.TestCase):
    """Run the generated first-boot script against stand-in binaries."""

    def run_bootstrap(self, *, mode_config: str, mode: str, up_rc: int, existing: str = "") -> tuple[int, str, bool]:
        helper = SdCloudInitRenderTests()
        code, data, err = helper.render(
            CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY,
            CLOUD_INIT_K3S_CONFIG=mode_config,
        )
        self.assertEqual(code, 0, err)
        files = helper.files(data)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bindir = tmp_path / "bin"
            bindir.mkdir()
            for name, body in {
                "tailscale": f'#!/bin/bash\ncase "$1" in up) exit {up_rc};; ip) echo 100.70.57.110;; esac\n',
                "systemctl": "#!/bin/bash\nexit 0\n",
                "sleep": "#!/bin/bash\nexit 0\n",
            }.items():
                path = bindir / name
                path.write_text(body, encoding="utf-8")
                path.chmod(path.stat().st_mode | stat.S_IEXEC)
            env_file = tmp_path / "node.env"
            env_file.write_text(files["/etc/rack-node.env"]["content"], encoding="utf-8")
            key_file = tmp_path / "authkey"
            key_file.write_text(files["/etc/rack-tailscale-authkey"]["content"], encoding="utf-8")
            config = tmp_path / "config.yaml"
            config.write_text(files["/etc/rancher/k3s/config.yaml"]["content"] + existing, encoding="utf-8")
            script = tmp_path / "bootstrap.sh"
            script.write_text(files["/usr/local/bin/rack-tailscale-bootstrap"]["content"], encoding="utf-8")
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True, text=True, timeout=60,
                env={
                    "PATH": f"{bindir}:{os.environ['PATH']}",
                    "RACK_NODE_ENV": str(env_file), "RACK_TS_KEY_FILE": str(key_file),
                    "RACK_K3S_CONFIG_PATH": str(config),
                },
            )
            return result.returncode, config.read_text(encoding="utf-8"), key_file.exists()

    def test_server_gets_node_ip_and_advertise_address_and_key_is_removed(self) -> None:
        rc, config, key_left = self.run_bootstrap(mode_config=K3S_SERVER_CONFIG, mode="server", up_rc=0)
        self.assertEqual(rc, 0)
        data = yaml.safe_load(config)
        self.assertEqual(data["node-ip"], "100.70.57.110")
        self.assertEqual(data["advertise-address"], "100.70.57.110")
        self.assertFalse(key_left, "the auth key file must not stay on disk")

    def test_agent_gets_node_ip_only(self) -> None:
        rc, config, _ = self.run_bootstrap(mode_config=K3S_WORKER_CONFIG, mode="agent", up_rc=0)
        self.assertEqual(rc, 0)
        data = yaml.safe_load(config)
        self.assertEqual(data["node-ip"], "100.70.57.110")
        self.assertNotIn("advertise-address", data)

    def test_stale_addresses_are_replaced_not_duplicated(self) -> None:
        rc, config, _ = self.run_bootstrap(mode_config=K3S_SERVER_CONFIG, mode="server", up_rc=0, existing="node-ip: 192.168.1.9\nadvertise-address: 192.168.1.9\n")
        self.assertEqual(rc, 0)
        self.assertEqual(config.count("node-ip:"), 1)
        self.assertEqual(yaml.safe_load(config)["node-ip"], "100.70.57.110")

    def test_failed_join_never_aborts_cloud_init_and_leaves_config_untouched(self) -> None:
        rc, config, key_left = self.run_bootstrap(mode_config=K3S_SERVER_CONFIG, mode="server", up_rc=1)
        self.assertEqual(rc, 0, "a Tailscale failure must not stop cloud-init before K3s installs")
        self.assertNotIn("node-ip", config)
        self.assertFalse(key_left)


if __name__ == "__main__":
    unittest.main()
