from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
import hashlib
import io
import runpy
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError
from pathlib import Path

import yaml
try:
    from .sd_script import read_sd_script
except ImportError:
    from sd_script import read_sd_script

AUTH_KEY = "tskey-auth-kTEST123CNTRL-abcdef"
K3S_SERVER_CONFIG = "server: https://100.65.24.76:6443\ntoken: SECRETTOKEN\nnode-name: render-test\n"
K3S_WORKER_CONFIG = "server: https://100.65.24.76:6443\ntoken: SECRETTOKEN\nnode-label:\n  - role=worker\n"


def _harness_text() -> str:
    """The user-data rendering section of the real script, without any device access."""
    text = read_sd_script()
    func = re.search(r"^validate_ssh_public_key_lines\(\) \{\n.*?^\}\n", text, re.S | re.M)
    toolkit_func = re.search(r"^toolkit_render\(\) \{\n.*?^\}\n", text, re.S | re.M)
    server_func = re.search(r"^server_render\(\) \{\n.*?^\}\n", text, re.S | re.M)
    url_func = re.search(r"^validate_http_url\(\) \{\n.*?^\}\n", text, re.S | re.M)
    body = re.search(r"^CLOUD_INIT_USERNAME=.*?(?=^cat > \"\$META_DATA_OUT\")", text, re.S | re.M)
    assert func and toolkit_func and server_func and url_func and body, "user-data rendering section not found in prepare_sd_cloud_init.sh"
    return (
        "set -euo pipefail\n"
        + func.group(0)
        + toolkit_func.group(0)
        + server_func.group(0)
        + url_func.group(0)
        + 'PROFILE="${R_PROFILE:-pi3bplus}"\nHOSTNAME="render-test"\nDISTRIBUTION="${R_DIST:-k3s}"\n'
        + f'SCRIPT_DIR="{Path(__file__).resolve().parents[2] / "scripts"}"\n'
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

    def test_server_bootstrap_precedes_self_callback_without_pi_packages(self) -> None:
        code, data, err = self.render(R_PROFILE="playbook-server", R_DIST="standard",
                                      CLOUD_INIT_K3S_CONFIG="", CLOUD_INIT_SERVER_INSTALL_FLASHER="false")
        self.assertEqual(code, 0, err)
        files = self.files(data)
        self.assertIn("PLAYBOOK_INSTALL_FLASHER=false", files["/etc/playbook-server.env"]["content"])
        self.assertIn("docker.io", data["packages"])
        self.assertNotIn("raspi-config", data["packages"])
        commands = self.runcmd(data)
        activate = commands.index("/usr/local/bin/playbook-server-activate")
        callback = next(index for index, command in enumerate(commands) if "rack-callback-payload" in command)
        self.assertLess(activate, callback)

    def test_misc_packages_and_neovim_are_opt_in_on_all_general_profiles(self) -> None:
        for profile in ("pi5", "pi4", "pi3bplus"):
            with self.subTest(profile=profile):
                code, data, err = self.render(R_PROFILE=profile, R_DIST="standard")
                self.assertEqual(code, 0, err)
                self.assertNotIn("neovim", data["packages"])
                self.assertNotIn("/usr/local/bin/rack-neovim-bootstrap", self.files(data))
                code, data, err = self.render(
                    R_PROFILE=profile, CLOUD_INIT_CONFIGURE_NEOVIM="true",
                    CLOUD_INIT_INSTALL_CLI_TOOLS="true", CLOUD_INIT_EXTRA_PACKAGES="strace jq git strace",
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(len(data["packages"]), len(set(data["packages"])))
                self.assertTrue({"neovim", "strace", "tmux", "ripgrep", "fd-find", "fzf",
                                 "jq", "unzip", "bash-completion"}.issubset(data["packages"]))
                self.assertIn(["/usr/local/bin/rack-neovim-bootstrap", "jdizzle"], data["runcmd"])
                self.assertNotIn("update-alternatives", "\n".join(self.runcmd(data)))
        for env in ({"CLOUD_INIT_EXTRA_PACKAGES": "--help"}, {"CLOUD_INIT_EXTRA_PACKAGES": "git;reboot"},
                    {"CLOUD_INIT_EXTRA_PACKAGES": "*"},
                    {"CLOUD_INIT_CONFIGURE_NEOVIM": "yes"}):
            code, _, err = self.render(**env)
            self.assertNotEqual(code, 0)
            self.assertIn("Invalid", err)

    def test_neovim_bootstrap_preserves_existing_configs_and_is_repeatable(self) -> None:
        code, data, err = self.render(CLOUD_INIT_CONFIGURE_NEOVIM="true")
        self.assertEqual(code, 0, err)
        files = self.files(data)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            source = root / "init.vim"
            source.write_text(files["/usr/share/rack/neovim/init.vim"]["content"])
            helper = root / "bootstrap"
            helper.write_text(files["/usr/local/bin/rack-neovim-bootstrap"]["content"].replace(
                "/usr/share/rack/neovim/init.vim", str(source)))
            for name, content in {
                "getent": f'#!/bin/sh\nprintf "rack-admin:x:1000:1000::%s:/bin/bash\\n" "{home}"\n',
                "runuser": '#!/bin/sh\nshift 3\nexec "$@"\n',
            }.items():
                tool = root / name
                tool.write_text(content)
                tool.chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}"}
            config = home / ".config/nvim"
            for existing in ("init.lua", "init.vim"):
                config.mkdir(parents=True, exist_ok=True)
                target = config / existing
                target.write_text("custom config")
                result = subprocess.run(["bash", str(helper), "rack-admin"], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("preserving existing", result.stdout)
                self.assertEqual(target.read_text(), "custom config")
                if existing == "init.lua":
                    self.assertFalse((config / "init.vim").exists())
                target.unlink()
            for _ in range(2):
                result = subprocess.run(["bash", str(helper), "rack-admin"], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((config / "init.vim").read_text(), source.read_text())
            (config / "init.vim").unlink()
            (config / "init.lua").symlink_to(root / "missing")
            result = subprocess.run(["bash", str(helper), "rack-admin"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((config / "init.vim").exists())
            source.unlink()
            (config / "init.lua").unlink()
            result = subprocess.run(["bash", str(helper), "rack-admin"], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr)

    @unittest.skipUnless(shutil.which("nvim"), "Neovim is not installed on the test host")
    def test_neovim_defaults_load_in_a_real_editor(self) -> None:
        code, data, err = self.render(CLOUD_INIT_CONFIGURE_NEOVIM="true")
        self.assertEqual(code, 0, err)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "init.vim"
            config.write_text(self.files(data)["/usr/share/rack/neovim/init.vim"]["content"])
            output = root / "settings"
            env = {**os.environ, "HOME": tmp, "XDG_DATA_HOME": str(root / "data"),
                   "XDG_CONFIG_HOME": str(root / "config"), "XDG_STATE_HOME": str(root / "state")}
            result = subprocess.run([
                "nvim", "--headless", "-u", str(config), "-n",
                "-c", f"call writefile([string(&number), string(&expandtab), string(&shiftwidth), string(&undofile)], '{output}')",
                "-c", "qa!",
            ], env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(output.read_text().splitlines(), ["1", "1", "4", "1"])
            undo = root / "data/nvim/undo"
            self.assertTrue(undo.is_dir())
            self.assertEqual(stat.S_IMODE(undo.stat().st_mode), 0o700)

    def test_all_pi_profiles_enable_passwordless_admin_sudo_by_default(self) -> None:
        for profile in ("pi5", "pi4", "pi3bplus", "network-toolkit"):
            for distribution in ("standard", "k3s"):
                if profile == "network-toolkit" and distribution == "k3s":
                    continue
                with self.subTest(profile=profile, distribution=distribution):
                    env = {"R_PROFILE": profile, "R_DIST": distribution, "CLOUD_INIT_USERNAME": "rack-admin"}
                    if profile == "network-toolkit":
                        env.update({
                            "CLOUD_INIT_TOOLKIT_TERMINAL_PASSWORD_HASH": "$6$testsalt$" + "a" * 86,
                            "CLOUD_INIT_TOOLKIT_WIFI_SSID": "test-uplink",
                            "CLOUD_INIT_TOOLKIT_WIFI_PASSWORD": "test-password",
                            "CLOUD_INIT_TOOLKIT_WIFI_COUNTRY": "US",
                            "CLOUD_INIT_TAILSCALE_AUTH_KEY": AUTH_KEY,
                        })
                    code, data, err = self.render(**env)
                    self.assertEqual(code, 0, err)
                    user = data["users"][1]
                    self.assertEqual(user["name"], "rack-admin")
                    self.assertEqual(user["sudo"], "ALL=(ALL) NOPASSWD: ALL")
                    self.assertIn("sudo", user["groups"])
                    command = data["runcmd"][0]
                    self.assertEqual(command[:2], ["bash", "-c"])
                    self.assertIn('rule="rack-admin ALL=(ALL) NOPASSWD: ALL"', command[2])
                    self.assertIn("/etc/sudoers.d/010_rack-admin-nopasswd", command[2])
                    self.assertIn("visudo -cf -", command[2])
                    self.assertIn("runuser -u rack-admin -- sudo -n true", command[2])
                    self.assertNotIn("network-console", command[2])
                    self.assertNotIn("rm ", command[2])
                    result = subprocess.run(["bash", "-n"], input=command[2], text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_admin_rule_install_aborts_if_validation_fails(self) -> None:
        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        with tempfile.TemporaryDirectory() as tmp:
            for tool in ("visudo", "install", "runuser"):
                path = Path(tmp) / tool
                path.write_text(
                    '#!/bin/sh\nprintf "%s\\n" "' + tool + '" >> "$CALL_LOG"\n'
                    'test "' + tool + '" != visudo\n'
                )
                path.chmod(0o755)
            log = Path(tmp) / "calls"
            result = subprocess.run(data["runcmd"][0], capture_output=True, text=True,
                                    env={"PATH": tmp, "CALL_LOG": str(log)}, executable="/bin/bash")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(log.read_text().splitlines(), ["visudo"])

    def test_website_key_fetch_is_pinned_and_precedes_tailscale(self) -> None:
        url = "http://192.168.174.1:8088/api/ssh/public/admin"
        code, data, err = self.render(
            CLOUD_INIT_SSH_PUBLIC_KEY_URL=url,
            CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY,
        )
        self.assertEqual(code, 0, err)
        files = self.files(data)
        self.assertIn("CLOUD_INIT_SSH_PUBLIC_KEY_URL=" + url, files["/etc/rack-node.env"]["content"])
        embedded = data["users"][1]["ssh_authorized_keys"][0]
        digest = hashlib.sha256(embedded.encode()).hexdigest()
        self.assertIn("CLOUD_INIT_SSH_PUBLIC_KEY_SHA256=" + digest, files["/etc/rack-node.env"]["content"])
        commands = self.runcmd(data)
        self.assertLess(next(i for i,c in enumerate(commands) if "rack-ssh-key-bootstrap" in c),
                        commands.index("/usr/local/bin/rack-tailscale-bootstrap"))
        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        self.assertNotIn("/usr/local/bin/rack-ssh-key-bootstrap", self.files(data))

    def test_website_key_fetch_preserves_keys_and_rejects_failure_or_substitution(self) -> None:
        code, data, err = self.render(CLOUD_INIT_SSH_PUBLIC_KEY_URL="https://callback.test/api/ssh/public/admin")
        self.assertEqual(code, 0, err)
        script = self.files(data)["/usr/local/bin/rack-ssh-key-bootstrap"]["content"]
        key = data["users"][1]["ssh_authorized_keys"][0].encode()
        digest = hashlib.sha256(key).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            helper = home / "helper.py"
            helper.write_text(script)
            directory = home / ".ssh"
            directory.mkdir()
            target = directory / "authorized_keys"
            target.write_bytes(b"existing-key")
            account = SimpleNamespace(pw_dir=tmp, pw_uid=os.getuid(), pw_gid=os.getgid())
            argv = [str(helper), "https://callback.test/api/ssh/public/admin", digest, "admin"]
            with patch("sys.argv", argv), patch("pwd.getpwnam", return_value=account), patch("os.chown"), patch("urllib.request.urlopen") as fetch:
                for _ in range(2):
                    fetch.return_value.__enter__.return_value.read.return_value = key + b"\n"
                    runpy.run_path(str(helper), run_name="__main__")
                self.assertEqual(target.read_bytes(), b"existing-key\n" + key + b"\n")
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                before = target.read_bytes()
                for response in (b"attacker-key", b"x" * 65537):
                    fetch.return_value.__enter__.return_value.read.return_value = response
                    with self.assertRaises(SystemExit):
                        runpy.run_path(str(helper), run_name="__main__")
                    self.assertEqual(target.read_bytes(), before)
                fetch.side_effect = URLError("unreachable")
                with patch("time.sleep"), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    with self.assertRaises(SystemExit):
                        runpy.run_path(str(helper), run_name="__main__")
                self.assertIn("public-key fetch failed", stderr.getvalue())
                self.assertEqual(target.read_bytes(), before)

    def test_every_embedded_script_parses(self) -> None:
        code, data, err = self.render(
            CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY,
            CLOUD_INIT_INSTALL_TELEGRAF="true", CLOUD_INIT_ENABLE_HARDWARE_CHECKS="true", CLOUD_INIT_FANCY_MOTD="true",
            CLOUD_INIT_CONFIGURE_NEOVIM="true",
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

    def test_hardware_callback_includes_architecture(self) -> None:
        code, data, err = self.render(CLOUD_INIT_ENABLE_HARDWARE_CHECKS="true")
        self.assertEqual(code, 0, err)
        script = self.files(data)["/usr/local/bin/rack-hardware-check"]["content"]
        self.assertIn('ARCHITECTURE="$(uname -m)"', script)
        self.assertIn('ARCHITECTURE="$ARCHITECTURE" ', script)
        payload_script = script.split("python3 <<'PY'\n", 1)[1].split("\nPY", 1)[0]
        for architecture in ("aarch64", "x86_64"):
            with self.subTest(architecture=architecture):
                result = subprocess.run(
                    ["python3", "-"], input=payload_script, text=True, capture_output=True,
                    env={**os.environ, "ARCHITECTURE": architecture}, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["hardware"]["architecture"], architecture)

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

    def test_bootstrap_reboot_is_deferred_until_cloud_init_finishes(self) -> None:
        for distribution in ("standard", "k3s"):
            for reboot in ("true", "false"):
                for callback in ("true", "false"):
                    with self.subTest(distribution=distribution, reboot=reboot, callback=callback):
                        code, data, err = self.render(
                            R_DIST=distribution,
                            CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP=reboot,
                            CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK=callback,
                        )
                        self.assertEqual(code, 0, err)
                        commands = self.runcmd(data)
                        self.assertFalse(any("reboot" in command or "shutdown" in command for command in commands))
                        if reboot == "true":
                            self.assertEqual(data["power_state"], {
                                "mode": "reboot", "delay": "now", "timeout": 600, "condition": True,
                            })
                        else:
                            self.assertNotIn("power_state", data)
                        service_commands = [c for c in commands if "rack-power-restored-callback.service" in c]
                        self.assertEqual(len(service_commands), 1 if callback == "true" else 0)
                        if service_commands:
                            self.assertEqual(commands[-1], service_commands[0])
                            self.assertIn("systemctl enable --now", service_commands[0])

    def test_power_restore_service_setup_reports_systemctl_failures(self) -> None:
        code, data, err = self.render(CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK="true")
        self.assertEqual(code, 0, err)
        command = next(c for c in data["runcmd"] if "rack-power-restored-callback.service" in c[-1])
        with tempfile.TemporaryDirectory() as tmp:
            systemctl = Path(tmp) / "systemctl"
            systemctl.write_text(
                '#!/bin/sh\n'
                'if [ "$1" = "$FAIL_COMMAND" ]; then echo "systemctl failed: $1" >&2; exit 1; fi\n',
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            for failure in ("daemon-reload", "enable"):
                with self.subTest(failure=failure):
                    result = subprocess.run(
                        command, capture_output=True, text=True, timeout=10,
                        env={"PATH": f"{tmp}:{os.environ['PATH']}", "FAIL_COMMAND": failure},
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"systemctl failed: {failure}", result.stderr)

    def test_boot_log_script_posts_logs_to_the_callback_site_and_is_wired_in(self) -> None:
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        files = self.files(data)
        self.assertIn("rack-boot-log runcmd", " ".join(self.runcmd(data)))
        self.assertIn("/usr/local/bin/rack-boot-log power_restore", files["/usr/local/bin/rack-power-restored-callback"]["content"])

        received: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                received["path"] = self.path
                received["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        script = files["/usr/local/bin/rack-boot-log"]["content"].replace("/etc/rack-node.env", "/nonexistent-rack-node.env")
        result = subprocess.run(
            ["bash", "-c", script, "rack-boot-log", "runcmd"],
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ["PATH"],
                "CALLBACK_URL": f"http://127.0.0.1:{server.server_address[1]}/api/cloud-init/callback",
                "INSTANCE_ID": "render-test-1",
                "PROFILE": "pi5",
            },
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received.get("path"), "/api/cloud-init/boot-log")
        body = received["body"]
        self.assertEqual((body["stage"], body["instance_id"], body["profile"]), ("runcmd", "render-test-1", "pi5"))
        self.assertTrue(body["hostname"])
        self.assertIn("=== node ===", body["log"])
        self.assertIn("=== journalctl -b (last 1500) ===", body["log"])

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

    def test_clock_sync_runs_before_any_https_install(self) -> None:
        code, data, err = self.render(CLOUD_INIT_TAILSCALE_ENABLE="true", CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY)
        self.assertEqual(code, 0, err)
        cmds = [" ".join(c) if isinstance(c, list) else str(c) for c in self.runcmd(data)]
        clock = next(i for i, c in enumerate(cmds) if "NTPSynchronized" in c)
        self.assertLess(clock, next(i for i, c in enumerate(cmds) if "rack-tailscale-bootstrap" in c))
        self.assertLess(clock, next(i for i, c in enumerate(cmds) if "get.k3s.io" in c))

    def test_clock_sync_apt_hook_waits_at_package_stage_without_blocking_early_boot(self) -> None:
        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        self.assertNotIn("bootcmd", data)
        files = self.files(data)
        command = files["/usr/local/bin/rack-clock-wait"]["content"]
        self.assertEqual(files["/usr/local/bin/rack-clock-wait"]["permissions"], "0755")
        self.assertIn('APT::Update::Pre-Invoke { "/usr/local/bin/rack-clock-wait"; };',
                      files["/etc/apt/apt.conf.d/00-rack-clock"]["content"])
        self.assertTrue(data["package_update"])
        self.assertIn("seq 1 90", command)
        self.assertIn("sleep 2", command)
        self.assertIn("exit 1", command)
        cmds = [" ".join(c) if isinstance(c, list) else str(c) for c in self.runcmd(data)]
        cleanup = cmds.index("rm -f /etc/apt/apt.conf.d/00-rack-clock")
        self.assertGreater(cleanup, next(i for i, c in enumerate(cmds) if "NTPSynchronized" in c))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in {
                "timedatectl": '#!/bin/sh\n[ "$1" = set-ntp ] && exit "$NTP_RC"\nprintf "%s\\n" "$SYNCED"\n',
                "sleep": "#!/bin/sh\nexit 0\n",
                "seq": "#!/bin/sh\nprintf '1\\n2\\n'\n",
                "rm": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RM_LOG"\n',
            }.items():
                path = root / name
                path.write_text(content)
                path.chmod(0o755)
            rm_log = root / "rm.log"
            for synced, ntp_rc, expected in (("yes", "0", 0), ("yes", "1", 0), ("no", "0", 1)):
                rm_log.unlink(missing_ok=True)
                result = subprocess.run(
                    ["/bin/sh", "-c", command], capture_output=True, text=True,
                    env={"PATH": tmp, "SYNCED": synced, "NTP_RC": ntp_rc, "RM_LOG": str(rm_log)},
                    executable="/bin/sh",
                )
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected:
                    self.assertIn("clock not synced after 180s", result.stderr)
                    self.assertFalse(rm_log.exists(), "the hook stays until the clock syncs")
                else:
                    self.assertEqual(rm_log.read_text(), "-f /etc/apt/apt.conf.d/00-rack-clock\n")

    def test_default_tags_follow_the_profile(self) -> None:
        for profile, expected in (("pi3bplus", "tag:rpi-3bplus,tag:homelab"), ("pi5", "tag:rpi5,tag:homelab"), ("pi4", "tag:rpi4,tag:homelab")):
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

    def test_k3s_version_pins_the_install_for_agents_and_servers(self) -> None:
        for mode in ("agent", "server"):
            with self.subTest(mode=mode):
                pinned = self.k3s_install_command(CLOUD_INIT_K3S_MODE=mode, CLOUD_INIT_K3S_VERSION="v1.36.4+k3s1")
                self.assertIn("INSTALL_K3S_VERSION=v1.36.4+k3s1 INSTALL_K3S_EXEC=", pinned)
                unpinned = self.k3s_install_command(CLOUD_INIT_K3S_MODE=mode)
                self.assertNotIn("INSTALL_K3S_VERSION", unpinned)

    def test_unpinned_k3s_version_is_called_out(self) -> None:
        _code, _data, err = self.render(CLOUD_INIT_K3S_MODE="agent")
        self.assertIn("K3s version is not pinned", err)
        _code, _data, err = self.render(CLOUD_INIT_K3S_MODE="agent", CLOUD_INIT_K3S_VERSION="v1.36.4+k3s1")
        self.assertNotIn("K3s version is not pinned", err)

    def test_bad_k3s_version_is_rejected(self) -> None:
        for bad in ("1.36.4", "latest", "v1.36.4+k3s1; id", "v1.36.4"):
            with self.subTest(version=bad):
                code, _data, err = self.render(CLOUD_INIT_K3S_VERSION=bad)
                self.assertNotEqual(code, 0)
                self.assertIn("CLOUD_INIT_K3S_VERSION", err)

    def test_server_install_refuses_an_sd_card_unless_allowed_and_agents_never_do(self) -> None:
        server = self.k3s_install_command(CLOUD_INIT_K3S_MODE="server")
        self.assertIn("/dev/mmcblk*", server)
        self.assertIn("exit 1", server)
        self.assertLess(server.index("/dev/mmcblk*"), server.index("get.k3s.io"), "the guard must run before the install")
        allowed = self.k3s_install_command(CLOUD_INIT_K3S_MODE="server", CLOUD_INIT_K3S_ALLOW_SD_ETCD="true")
        self.assertNotIn("/dev/mmcblk*", allowed)
        self.assertNotIn("/dev/mmcblk*", self.k3s_install_command(CLOUD_INIT_K3S_MODE="agent"))

    def test_the_sd_guard_script_actually_refuses_and_allows(self) -> None:
        command = self.k3s_install_command(CLOUD_INIT_K3S_MODE="server")
        script = command.removeprefix("sh -c ")
        guard = script.split("mkdir -p", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp)
            findmnt = fake_bin / "findmnt"
            for device, expected in (("/dev/mmcblk0p2", 1), ("/dev/sda2", 0), ("/dev/nvme0n1p2", 0)):
                findmnt.write_text(f"#!/bin/sh\necho {device}\n", encoding="utf-8")
                findmnt.chmod(findmnt.stat().st_mode | stat.S_IEXEC)
                result = subprocess.run(
                    ["bash", "-c", guard], capture_output=True, text=True, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"}, timeout=10
                )
                self.assertEqual(result.returncode, expected, f"{device}: {result.stderr}")
                if expected:
                    self.assertIn("refusing", result.stderr)

    def test_ubuntu_images_that_need_the_vxlan_module_get_it(self) -> None:
        cases = (
            ("https://cdimage.ubuntu.com/releases/22.04/release/ubuntu-22.04.4-preinstalled-server-arm64+raspi.img.xz", True),
            ("https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz", False),
            ("https://downloads.raspberrypi.com/raspios_arm64/images/2026-06-18-raspios-trixie-arm64.img.xz", False),
        )
        for url, expected in cases:
            with self.subTest(url=url):
                code, data, err = self.render(IMAGE_URL=url)
                self.assertEqual(code, 0, err)
                self.assertEqual("linux-modules-extra-raspi" in data.get("packages", []), expected)
        code, data, err = self.render(IMAGE_URL=cases[0][0], R_DIST="standard")
        self.assertEqual(code, 0, err)
        self.assertNotIn("linux-modules-extra-raspi", data.get("packages", []), "plain installs do not need it")

    def test_the_boot_partition_gets_the_memory_cgroup_flags_once(self) -> None:
        script = read_sd_script()
        snippet = re.search(r"^if \[\[ \"\$DISTRIBUTION\" == \"k3s\" \]\]; then\n  for cmdline_file in .*?^fi\n", script, re.S | re.M)
        self.assertIsNotNone(snippet, "cgroup step not found in the script")
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp) / "boot"
            boot.mkdir()
            cmdline = boot / "cmdline.txt"
            cmdline.write_text("console=serial0,115200 root=PARTUUID=abc rootfstype=ext4 rootwait\n", encoding="utf-8")
            for distribution, expected_flags in (("standard", 0), ("k3s", 1)):
                cmdline.write_text("console=serial0,115200 root=PARTUUID=abc rootfstype=ext4 rootwait\n", encoding="utf-8")
                for _ in range(2):  # running twice must not duplicate the flags
                    result = subprocess.run(
                        ["bash", "-c", f'TMP_DIR="{tmp}"; DISTRIBUTION={distribution}\n{snippet.group(0)}'],
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                text = cmdline.read_text(encoding="utf-8")
                self.assertEqual(text.count("cgroup_memory=1"), expected_flags, distribution)
                self.assertEqual(text.count("cgroup_enable=memory"), expected_flags, distribution)
                self.assertEqual(len(text.strip().splitlines()), 1, "cmdline.txt must stay a single line")

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

    def run_bootstrap(self, *, mode_config: str, mode: str, up_rc: int, existing: str = "", installs_on_attempt: int | None = None) -> tuple[int, str, bool]:
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
            ts_body = f'#!/bin/bash\ncase "$1" in up) exit {up_rc};; ip) echo 100.70.57.110;; esac\n'
            stubs = {
                "systemctl": "#!/bin/bash\nexit 0\n",
                "sleep": "#!/bin/bash\nexit 0\n",
            }
            if installs_on_attempt is None:
                stubs["tailscale"] = ts_body
            else:
                # Tailscale starts absent; the fake installer only works from the Nth curl call.
                counter = tmp_path / "curl-calls"
                install = f"printf '%s' {shlex.quote(ts_body)} > {bindir}/tailscale; chmod +x {bindir}/tailscale"
                stubs["curl"] = (
                    f"#!/bin/bash\nn=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}\n"
                    f"if [ $n -lt {installs_on_attempt} ]; then exit 22; fi\necho {shlex.quote(install)}\n"
                )
                for tool in ("sh", "tr", "mv", "chmod", "sed", "tail", "head", "seq", "cat", "rm"):
                    (bindir / tool).symlink_to(shutil.which(tool))
            for name, body in stubs.items():
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
                ["bash" if installs_on_attempt is None else shutil.which("bash"), str(script)],
                capture_output=True, text=True, timeout=60,
                env={
                    "PATH": str(bindir) if installs_on_attempt is not None else f"{bindir}:{os.environ['PATH']}",
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

    def test_install_is_retried_until_the_network_is_ready(self) -> None:
        rc, config, _ = self.run_bootstrap(mode_config=K3S_WORKER_CONFIG, mode="agent", up_rc=0, installs_on_attempt=3)
        self.assertEqual(rc, 0)
        self.assertEqual(yaml.safe_load(config)["node-ip"], "100.70.57.110")

    def test_install_that_never_works_still_lets_cloud_init_continue(self) -> None:
        rc, config, _ = self.run_bootstrap(mode_config=K3S_WORKER_CONFIG, mode="agent", up_rc=0, installs_on_attempt=99)
        self.assertEqual(rc, 0)
        self.assertNotIn("node-ip", config)

    def test_failed_join_never_aborts_cloud_init_and_leaves_config_untouched(self) -> None:
        rc, config, key_left = self.run_bootstrap(mode_config=K3S_SERVER_CONFIG, mode="server", up_rc=1)
        self.assertEqual(rc, 0, "a Tailscale failure must not stop cloud-init before K3s installs")
        self.assertNotIn("node-ip", config)
        self.assertFalse(key_left)


if __name__ == "__main__":
    unittest.main()
