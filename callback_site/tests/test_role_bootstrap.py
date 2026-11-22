"""Ansible role bootstrap: clock wait, installer retries, K3s install on a fresh node, Docker module."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = REPO_ROOT / ".venv" / "bin" / "ansible-playbook"

BOARDS = {
    "pi5": {"role_dir": "raspberry_pi5", "worker": "worker", "docker": True},
    "pi4": {"role_dir": "raspberry_pi4", "worker": "worker", "docker": True},
    "pi3bplus": {"role_dir": "raspberry_pi_3bplus", "worker": "light-worker", "docker": False},
}

SHARED_TASKS = (
    "Wait for the system clock to synchronise",
    "Let apt wait for the dpkg lock",
    "Download Tailscale installation script",
    "Run Tailscale installer when binary is missing",
    "Ensure the K3s config directory exists",
    "Read the K3s server's version",
    "Install K3s when this node does not run it yet",
    "Wait for the joined K3s control-plane API and datastore",
    "Resolve Telegraf inputs",
    "Validate Telegraf settings",
    "Install Telegraf",
    "Ensure the Telegraf config directory exists",
    "Write Telegraf config from the Influx settings",
    "Download Telegraf config from the config URL",
    "Give Telegraf its token for downloaded configs",
    "Check that the Telegraf config parses",
    "Ensure Telegraf is enabled and running",
    "Restart Telegraf when its config changes",
)
DOCKER_TASKS = (
    "Resolve Docker inputs",
    "Install Docker engine",
    "Ensure the Docker config directory exists",
    "Ensure the Docker data directory exists",
    "Write Docker daemon settings",
    "Ensure Docker is enabled and running",
    "Restart Docker when its daemon settings change",
)
INSTALL = "Install K3s when this node does not run it yet"


def load_tasks(role_dir: str) -> list[dict]:
    return yaml.safe_load((REPO_ROOT / "roles" / role_dir / "tasks" / "main.yml").read_text(encoding="utf-8"))


def by_name(tasks: list[dict]) -> dict[str, dict]:
    return {t["name"]: t for t in tasks if isinstance(t, dict) and "name" in t}


class RoleBootstrapStructureTests(unittest.TestCase):
    def test_every_role_carries_the_shared_bootstrap_tasks(self) -> None:
        for board, info in BOARDS.items():
            names = by_name(load_tasks(info["role_dir"]))
            for task in SHARED_TASKS + (DOCKER_TASKS if info["docker"] else ()):
                with self.subTest(board=board, task=task):
                    self.assertIn(task, names)
            if not info["docker"]:
                self.assertNotIn("Install Docker engine", names, "the Docker module does not target the 3B+")

    def test_clock_and_apt_lock_come_before_any_download(self) -> None:
        for board, info in BOARDS.items():
            order = [t.get("name") for t in load_tasks(info["role_dir"])]
            with self.subTest(board=board):
                self.assertLess(order.index("Wait for the system clock to synchronise"), order.index("Update apt cache"))
                self.assertLess(order.index("Let apt wait for the dpkg lock"), order.index("Update apt cache"))
                self.assertLess(order.index("Wait for the system clock to synchronise"), order.index("Download Tailscale installation script"))

    def test_tailscale_download_and_install_are_retried(self) -> None:
        for board, info in BOARDS.items():
            names = by_name(load_tasks(info["role_dir"]))
            for name in ("Download Tailscale installation script", "Run Tailscale installer when binary is missing"):
                with self.subTest(board=board, task=name):
                    self.assertGreaterEqual(names[name].get("retries", 0), 3)
                    self.assertIn("until", names[name])

    def test_k3s_install_runs_after_its_config_and_directory_exist(self) -> None:
        for board, info in BOARDS.items():
            order = [t.get("name") for t in load_tasks(info["role_dir"])]
            writes = [i for i, n in enumerate(order) if str(n).startswith("Write structured K3s")]
            with self.subTest(board=board):
                self.assertEqual(len(writes), 2)
                self.assertLess(order.index("Ensure the K3s config directory exists"), min(writes))
                self.assertGreater(order.index(INSTALL), max(writes))

    def test_playbook_passes_ansible_syntax_check(self) -> None:
        if not ANSIBLE.exists():
            self.skipTest("repo ansible-playbook not available")
        result = subprocess.run(
            [str(ANSIBLE), "-i", "inventory.yml", "site.yml", "--syntax-check"],
            capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_control_plane_readiness_checks_the_local_server_after_install(self) -> None:
        for board, info in BOARDS.items():
            tasks = load_tasks(info["role_dir"])
            names = by_name(tasks)
            name = "Wait for the joined K3s control-plane API and datastore"
            with self.subTest(board=board):
                readiness = names[name]
                argv = readiness["ansible.builtin.command"]["argv"]
                self.assertIn("--kubeconfig=/etc/rancher/k3s/k3s.yaml", argv)
                self.assertIn("--server=https://127.0.0.1:6443", argv)
                self.assertIn("--request-timeout=5s", argv)
                self.assertIn("--raw=/readyz", argv)
                self.assertIn("not ansible_check_mode", readiness["when"])
                self.assertIn(f"{info['role_dir']}_k3s_role | default('') == 'control-plane'", readiness["when"])
                self.assertNotIn("failed_when", readiness)
                self.assertFalse(readiness["changed_when"])
                order = [task.get("name") for task in tasks]
                self.assertGreater(order.index(name), order.index(INSTALL))


class RoleBootstrapRenderTests(unittest.TestCase):
    """Run the real task definitions through Ansible, swapping only the side-effecting module."""

    def setUp(self) -> None:
        if not ANSIBLE.exists():
            self.skipTest("repo ansible-playbook not available")

    def run_play(self, tasks: list[dict], variables: dict, check: bool = False) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            play_file = Path(tmp) / "play.yml"
            play_file.write_text(
                yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": tasks}]),
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(ANSIBLE), "-i", "localhost,", "-c", "local", str(play_file)] + (["--check"] if check else []),
                capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
            )
        return result.returncode, result.stdout + result.stderr

    def test_tailscale_inputs_are_applied_on_connected_nodes(self) -> None:
        for board, info in BOARDS.items():
            role = info["role_dir"]
            names = by_name(load_tasks(role))
            resolve = copy.deepcopy(names["Resolve Tailscale provisioning inputs"])
            apply = copy.deepcopy(names["Apply explicit Tailscale inputs on connected nodes"])
            apply["ansible.builtin.command"]["argv"] = apply["ansible.builtin.command"]["argv"].replace(
                "['tailscale', 'set']", "['/bin/echo', 'set']",
            )
            apply["register"] = "applied"
            variables = {
                "rack_manage_with_ansible": True,
                "provision_profile": "tailscale",
                "provision_inputs": {
                    "hostname": "rack-custom", "enable_ssh": "true",
                    "advertise_exit_node": "false", "advertise_tags": "tag:rpi5,tag:homelab",
                },
                f"{role}_tailscale_manage": True,
                f"{role}_tailscale_tailnet": "example.ts.net",
                f"{role}_tailscale_tags": ["tag:default"],
                f"{role}_tailscale_hostname": "default-name",
                f"{role}_tailscale_enable_ssh": False,
                f"{role}_tailscale_advertise_exit_node": True,
                f"{role}_tailscale_needs_up": False,
                f"{role}_tailscale_force_reconfigure": False,
            }
            show = {"ansible.builtin.debug": {"var": "applied.stdout"}}
            with self.subTest(board=board):
                code, output = self.run_play([resolve, apply, show], variables)
                self.assertEqual(code, 0, output[-1500:])
                for arg in ("--hostname=rack-custom", "--ssh=true", "--advertise-exit-node=false", "--advertise-tags=tag:rpi5,tag:homelab"):
                    self.assertIn(arg, output)

    def test_tailscale_unspecified_preferences_are_not_reset(self) -> None:
        role = BOARDS["pi5"]["role_dir"]
        names = by_name(load_tasks(role))
        resolve = copy.deepcopy(names["Resolve Tailscale provisioning inputs"])
        apply = copy.deepcopy(names["Apply explicit Tailscale inputs on connected nodes"])
        apply["ansible.builtin.command"]["argv"] = apply["ansible.builtin.command"]["argv"].replace(
            "['tailscale', 'set']", "['/bin/echo', 'set']",
        )
        apply["register"] = "applied"
        variables = {
            "rack_manage_with_ansible": True, "provision_profile": "tailscale",
            "provision_inputs": {"hostname": "only-hostname"},
            f"{role}_tailscale_manage": True, f"{role}_tailscale_tailnet": "",
            f"{role}_tailscale_tags": ["tag:default"], f"{role}_tailscale_hostname": "default",
            f"{role}_tailscale_enable_ssh": False, f"{role}_tailscale_advertise_exit_node": False,
            f"{role}_tailscale_needs_up": False, f"{role}_tailscale_force_reconfigure": False,
        }
        code, output = self.run_play([resolve, apply, {"ansible.builtin.debug": {"var": "applied.stdout"}}], variables)
        self.assertEqual(code, 0, output[-1500:])
        self.assertIn("set --hostname=only-hostname", output)
        self.assertNotIn("--ssh=", output)
        self.assertNotIn("--advertise-exit-node=", output)
        self.assertNotIn("--advertise-tags=", output)

    def test_new_tailscale_connection_uses_inputs_or_group_defaults(self) -> None:
        role = BOARDS["pi5"]["role_dir"]
        names = by_name(load_tasks(role))
        resolve = copy.deepcopy(names["Resolve Tailscale provisioning inputs"])
        up = copy.deepcopy(names["Bring node online with desired tailscale flags"])
        up["ansible.builtin.command"] = up["ansible.builtin.command"].replace("tailscale up", "/bin/echo up", 1)
        up["no_log"] = False
        up["register"] = "applied"
        variables = {
            "rack_manage_with_ansible": True, f"{role}_tailscale_manage": True,
            f"{role}_tailscale_tailnet": "", f"{role}_tailscale_tags": ["tag:default"],
            f"{role}_tailscale_hostname": "default-name", f"{role}_tailscale_enable_ssh": True,
            f"{role}_tailscale_advertise_exit_node": True, f"{role}_tailscale_auth_key_effective": "fake-key",
            f"{role}_tailscale_accept_dns": True, f"{role}_tailscale_advertise_routes": [],
            f"{role}_tailscale_needs_up": True, f"{role}_tailscale_force_reconfigure": False,
        }
        for inputs, expected, absent in (
            (None, ("--hostname=default-name", "--ssh", "--advertise-exit-node"), ()),
            ({"hostname": "custom", "enable_ssh": "false", "advertise_exit_node": "false", "auth_key": "secret-should-not-log"},
             ("--hostname=custom",), ("--ssh", "--advertise-exit-node", "secret-should-not-log")),
        ):
            with self.subTest(inputs=inputs):
                current = dict(variables)
                if inputs is not None:
                    current["provision_inputs"] = inputs
                code, output = self.run_play([resolve, up, {"ansible.builtin.debug": {"var": "applied.stdout"}}], current)
                self.assertEqual(code, 0, output[-1500:])
                for value in expected:
                    self.assertIn(value, output)
                for value in absent:
                    self.assertNotIn(value, output)

    def test_connected_tailscale_does_not_require_new_credentials(self) -> None:
        for board, info in BOARDS.items():
            role = info["role_dir"]
            names = by_name(load_tasks(role))
            tasks = [copy.deepcopy(names[name]) for name in (
                "Request OAuth access token for Tailscale API",
                "Create short-lived auth key from OAuth token",
                "Ensure auth key exists for tailscale up",
            )]
            variables = {
                f"{role}_tailscale_manage": True, f"{role}_tailscale_needs_up": False,
                f"{role}_tailscale_force_reconfigure": False, f"{role}_tailscale_oauth_enabled": True,
                f"{role}_tailscale_auth_key": "", f"{role}_tailscale_auth_key_effective": "",
            }
            with self.subTest(board=board):
                code, output = self.run_play(tasks, variables)
                self.assertEqual(code, 0, output[-1500:])
                self.assertNotIn("failed=1", output)

    def test_tailscale_check_mode_reports_planned_preferences(self) -> None:
        role = BOARDS["pi5"]["role_dir"]
        names = by_name(load_tasks(role))
        tasks = [copy.deepcopy(names["Report explicit Tailscale inputs in check mode"])]
        variables = {
            "provision_profile": "tailscale", "provision_inputs": {"enable_ssh": "false"},
            f"{role}_tailscale_manage": True, f"{role}_tailscale_needs_up": False,
            f"{role}_tailscale_force_reconfigure": False,
        }
        code, output = self.run_play(tasks, variables, check=True)
        self.assertEqual(code, 0, output[-1500:])
        self.assertIn("current values are not compared", output)
        self.assertIn("changed=1", output)

    def test_baseline_role_runs_without_optional_inputs(self) -> None:
        task = {"ansible.builtin.include_role": {"name": str(REPO_ROOT / "roles" / "node_baseline")}}
        code, output = self.run_play([task], {"provision_inputs": {}}, check=True)
        self.assertEqual(code, 0, output[-1500:])

    def test_baseline_inputs_validate_and_only_enable_checked_options(self) -> None:
        names = by_name(load_tasks("node_baseline"))
        base = [copy.deepcopy(names["Resolve Baseline module inputs"]), copy.deepcopy(names["Validate Baseline module inputs"])]
        for enabled in (False, True):
            tasks = copy.deepcopy(base)
            for name in ("Install unattended upgrades", "Enable unattended upgrades", "Install fail2ban", "Enable fail2ban", "Run hardware detection"):
                task = copy.deepcopy(names[name])
                for key in list(task):
                    if key.startswith("ansible.builtin."):
                        del task[key]
                task.pop("changed_when", None)
                task["ansible.builtin.debug"] = {"msg": "executed " + name}
                tasks.append(task)
            inputs = {
                "timezone": "Europe/London", "ntp_servers": "time.example.org, 192.0.2.1",
                "packages": "git,curl", "enable_unattended_upgrades": str(enabled).lower(),
                "enable_fail2ban": str(enabled).lower(), "enable_hardware_checks": str(enabled).lower(),
            }
            with self.subTest(enabled=enabled):
                code, output = self.run_play(tasks, {"provision_inputs": inputs})
                self.assertEqual(code, 0, output[-1500:])
                self.assertEqual("executed Install fail2ban" in output, enabled)
                self.assertEqual("executed Enable unattended upgrades" in output, enabled)
                self.assertEqual("executed Run hardware detection" in output, enabled)

        for inputs in ({"timezone": "../etc"}, {"ntp_servers": "good.example,bad\nNTP=evil"}, {"packages": "git,-o"}):
            with self.subTest(inputs=inputs):
                code, output = self.run_play(base, {"provision_inputs": inputs})
                self.assertNotEqual(code, 0, output)
                self.assertIn("Invalid Baseline timezone, NTP server, or package name", output)

    def render_install(self, board: str, role: str, server_version: dict | None = None, pinned: str = "") -> dict:
        role_dir = BOARDS[board]["role_dir"]
        task = copy.deepcopy(by_name(load_tasks(role_dir))[INSTALL])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "install.json"
            tasks = [
                {
                    "ansible.builtin.set_fact": {"rendered_cmd": task["ansible.builtin.shell"], "rendered_creates": task["args"]["creates"]},
                    "vars": task["vars"],
                },
                {"ansible.builtin.copy": {"dest": str(out), "content": "{{ {'cmd': rendered_cmd, 'creates': rendered_creates} | to_json }}"}},
            ]
            code, output = self.run_play(tasks, {
                f"{role_dir}_k3s_role": role,
                f"{role_dir}_k3s_version": pinned,
                f"{role_dir}_k3s_server_version": {"json": server_version} if server_version else {},
            })
            self.assertEqual(code, 0, output[-800:])
            return json.loads(out.read_text(encoding="utf-8"))

    def test_server_install_matches_the_servers_version_and_joins_as_a_server(self) -> None:
        for board, info in BOARDS.items():
            with self.subTest(board=board):
                rendered = self.render_install(board, "control-plane", {"gitVersion": "v1.36.4+k3s1"})
                self.assertIn("INSTALL_K3S_VERSION=v1.36.4+k3s1 ", rendered["cmd"])
                self.assertIn("INSTALL_K3S_EXEC='server --with-node-id --config /etc/rancher/k3s/config.yaml'", rendered["cmd"])
                self.assertEqual(rendered["creates"], "/etc/systemd/system/k3s.service")

    def test_worker_install_is_an_agent_and_a_pinned_version_wins(self) -> None:
        for board, info in BOARDS.items():
            with self.subTest(board=board):
                rendered = self.render_install(board, info["worker"], {"gitVersion": "v1.36.4+k3s1"}, pinned="v1.35.0+k3s1")
                self.assertIn("INSTALL_K3S_VERSION=v1.35.0+k3s1 ", rendered["cmd"])
                self.assertIn("INSTALL_K3S_EXEC='agent --with-node-id --config /etc/rancher/k3s/config.yaml'", rendered["cmd"])
                self.assertEqual(rendered["creates"], "/etc/systemd/system/k3s-agent.service")

    def test_unknown_server_version_falls_back_to_the_stable_channel(self) -> None:
        rendered = self.render_install("pi5", "worker")
        self.assertNotIn("INSTALL_K3S_VERSION", rendered["cmd"])

    def test_control_plane_readiness_requires_ok_and_a_successful_exit(self) -> None:
        name = "Wait for the joined K3s control-plane API and datastore"
        for stdout, exit_code, expected in (("ok", 0, True), ("not ready", 0, False), ("ok", 1, False)):
            with self.subTest(stdout=stdout, exit_code=exit_code):
                task = copy.deepcopy(by_name(load_tasks("raspberry_pi5"))[name])
                task["ansible.builtin.command"]["argv"] = [
                    "/bin/sh", "-c", f"printf '%s' '{stdout}'; exit {exit_code}",
                ]
                task["retries"] = 1
                task["delay"] = 0
                code, output = self.run_play([task], {
                    "rack_manage_with_ansible": True,
                    "provision_profile": "kubernetes",
                    "raspberry_pi5_k3s_role": "control-plane",
                })
                self.assertEqual(code == 0, expected, output[-1000:])

    def render_docker_daemon(self, board: str, inputs: dict) -> dict | None:
        role_dir = BOARDS[board]["role_dir"]
        names = by_name(load_tasks(role_dir))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "daemon.json"
            write = copy.deepcopy(names["Write Docker daemon settings"])
            write.pop("tags", None)
            write.pop("register", None)
            write["ansible.builtin.copy"] = {"dest": str(out), "content": write["ansible.builtin.copy"]["content"]}
            tasks = [copy.deepcopy(names["Resolve Docker inputs"]), write]
            for task in tasks:
                task.pop("tags", None)
            code, output = self.run_play(tasks, {"rack_manage_with_ansible": True, "provision_profile": "docker", "provision_inputs": inputs})
            self.assertEqual(code, 0, output[-800:])
            return json.loads(out.read_text(encoding="utf-8")) if out.exists() else None

    def test_docker_daemon_settings_only_carry_what_was_entered(self) -> None:
        for board in ("pi5", "pi4"):
            with self.subTest(board=board):
                both = self.render_docker_daemon(board, {"docker_data_root": "/srv/docker", "registry_mirror": "https://mirror.lab:5000"})
                self.assertEqual(both, {"data-root": "/srv/docker", "registry-mirrors": ["https://mirror.lab:5000"]})
                mirror_only = self.render_docker_daemon(board, {"docker_data_root": "", "registry_mirror": "https://mirror.lab:5000"})
                self.assertEqual(mirror_only, {"registry-mirrors": ["https://mirror.lab:5000"]})
                self.assertIsNone(self.render_docker_daemon(board, {"docker_data_root": "", "registry_mirror": ""}), "blank inputs must not overwrite a hand-edited daemon.json")

    TELEGRAF_BASE = {"influx_endpoint": "https://influx.example:8086/", "influx_bucket": "telegraf", "influx_org": "homelab", "influx_access_key": "access"}

    def telegraf_tasks(self, board: str, names: tuple[str, ...]) -> list[dict]:
        by = by_name(load_tasks(BOARDS[board]["role_dir"]))
        tasks = []
        for name in names:
            task = copy.deepcopy(by[name])
            task.pop("tags", None)
            task.pop("no_log", None)
            tasks.append(task)
        return tasks

    def render_telegraf_conf(self, board: str, inputs: dict) -> dict:
        tasks = self.telegraf_tasks(board, ("Resolve Telegraf inputs", "Write Telegraf config from the Influx settings"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "telegraf.conf"
            tasks[1]["ansible.builtin.copy"] = {"dest": str(out), "content": tasks[1]["ansible.builtin.copy"]["content"]}
            tasks[1].pop("register", None)
            code, output = self.run_play(tasks, {
                "rack_manage_with_ansible": True, "provision_profile": "telegraf", "inventory_hostname": "rack-node-01", "provision_inputs": inputs,
            })
            self.assertEqual(code, 0, output[-800:])
            return tomllib.loads(out.read_text(encoding="utf-8"))

    def test_telegraf_config_is_valid_toml_with_the_chosen_token_form(self) -> None:
        for board in BOARDS:
            with self.subTest(board=board):
                plain = self.render_telegraf_conf(board, {**self.TELEGRAF_BASE, "influx_secret_key": "secret", "influx_token_mode": "access_key"})
                output = plain["outputs"]["influxdb_v2"][0]
                self.assertEqual(output["urls"], ["https://influx.example:8086"], "a trailing slash is dropped")
                self.assertEqual((output["token"], output["organization"], output["bucket"]), ("access", "homelab", "telegraf"))
                self.assertEqual(plain["agent"]["hostname"], "rack-node-01")
                self.assertIn("cpu", plain["inputs"])

                combo = self.render_telegraf_conf(board, {**self.TELEGRAF_BASE, "influx_secret_key": "secret", "influx_token_mode": "access_key:secret_key"})
                self.assertEqual(combo["outputs"]["influxdb_v2"][0]["token"], "access:secret")

    def test_telegraf_values_with_quotes_cannot_break_the_config(self) -> None:
        conf = self.render_telegraf_conf("pi5", {**self.TELEGRAF_BASE, "influx_org": 'ho"me\\lab'})
        self.assertEqual(conf["outputs"]["influxdb_v2"][0]["organization"], 'ho"me\\lab')

    def run_telegraf_validation(self, board: str, inputs: dict) -> int:
        tasks = self.telegraf_tasks(board, ("Resolve Telegraf inputs", "Validate Telegraf settings"))
        code, _output = self.run_play(tasks, {"rack_manage_with_ansible": True, "provision_profile": "telegraf", "provision_inputs": inputs})
        return code

    def test_telegraf_validation_accepts_a_config_url_or_complete_influx_settings(self) -> None:
        for board in BOARDS:
            with self.subTest(board=board):
                self.assertEqual(self.run_telegraf_validation(board, self.TELEGRAF_BASE), 0)
                self.assertEqual(self.run_telegraf_validation(board, {"telegraf_config_url": "https://influx.example/api/v2/telegrafs/abc"}), 0)
                self.assertNotEqual(self.run_telegraf_validation(board, {**self.TELEGRAF_BASE, "influx_access_key": ""}), 0, "no token")
                self.assertNotEqual(self.run_telegraf_validation(board, {**self.TELEGRAF_BASE, "influx_endpoint": "influx.example:8086"}), 0, "endpoint must be http(s)")
                self.assertNotEqual(self.run_telegraf_validation(board, {"telegraf_config_url": "ftp://x"}), 0)
                self.assertNotEqual(self.run_telegraf_validation(board, {}), 0)


if __name__ == "__main__":
    unittest.main()
