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

    def run_play(self, tasks: list[dict], variables: dict) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            play_file = Path(tmp) / "play.yml"
            play_file.write_text(
                yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": tasks}]),
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(ANSIBLE), "-i", "localhost,", "-c", "local", str(play_file)],
                capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
            )
        return result.returncode, result.stdout + result.stderr

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
