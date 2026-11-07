"""The three board roles carry the same K3s and Tailscale logic; these tests stop the copies drifting apart."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "callback_site"))

import k3s_lightweight as lw  # noqa: E402
ROLES = {"pi5": ("raspberry_pi5", "Pi 5"), "pi4": ("raspberry_pi4", "Pi 4"), "pi3bplus": ("raspberry_pi_3bplus", "3B+")}
# Tasks that exist on one board only, on purpose.
PI5_ONLY = {"Resolve Longhorn inputs from provisioning payload", "Ensure Longhorn prerequisite packages are installed",
            "Ensure iscsid service is enabled and running", "Ensure open-iscsi service is enabled and running",
            "Prepare the Longhorn storage disk", "Report the Longhorn storage path",
            "Ensure Longhorn storage path exists", "Check whether this node is a K3s server",
            "Install or update Longhorn via K3s HelmChart manifest"}
# The Docker module targets Pi 4 and Pi 5 only (see its groups in NODE_PROVISION_PROFILES).
DOCKER_TASKS = {
    "Resolve Docker inputs", "Install Docker engine", "Ensure the Docker config directory exists",
    "Ensure the Docker data directory exists", "Write Docker daemon settings", "Ensure Docker is enabled and running",
    "Restart Docker when its daemon settings change",
}
# The 3B+ has a light-worker role instead of worker, so these are named differently.
THREEB_RENAMES = {
    "Validate Pi 4 role": "Validate 3B+ role",
    "Validate Pi 4 control-plane settings": "Validate 3B+ control-plane settings",
    "Validate Pi 4 worker settings": "Validate 3B+ light-worker settings",
    "Validate Pi 4 max pods": "Validate 3B+ max pods",
    "Write structured K3s worker config": "Write structured K3s light-worker config",
    "Refuse implicit K3s server-to-worker conversion": "Refuse implicit K3s server-to-agent conversion",
    "Restart K3s agent after structured config change": "Restart K3s agent after light-worker config change",
}


def load_tasks(role_dir: str) -> list[dict]:
    return yaml.safe_load((REPO_ROOT / "roles" / role_dir / "tasks" / "main.yml").read_text(encoding="utf-8"))


def canonical(task: dict, directory: str, label: str) -> str:
    """Task text with the board's variable prefix and label replaced, so copies compare equal."""
    text = yaml.safe_dump(task, sort_keys=True, width=10**9)
    text = text.replace(directory + "_", "BOARD_").replace(label, "BOARD")
    return re.sub(r"\s+", " ", text)


class RoleParityTests(unittest.TestCase):
    def test_pi4_is_the_pi5_role_without_longhorn(self) -> None:
        pi5 = {t["name"]: canonical(t, "raspberry_pi5", "Pi 5") for t in load_tasks("raspberry_pi5") if "name" in t}
        pi4 = {t["name"].replace("Pi 4", "Pi 5"): canonical(t, "raspberry_pi4", "Pi 4") for t in load_tasks("raspberry_pi4") if "name" in t}
        self.assertEqual(set(pi4), set(pi5) - PI5_ONLY, "Pi 4 and Pi 5 should differ only by the Longhorn tasks")
        for name in pi4:
            self.assertEqual(pi4[name], pi5[name], f"'{name}' differs between the Pi 5 and Pi 4 roles")

    def test_pi4_keeps_the_pi5_task_order(self) -> None:
        pi5 = [t["name"] for t in load_tasks("raspberry_pi5") if "name" in t and t["name"] not in PI5_ONLY]
        pi4 = [t["name"].replace("Pi 4", "Pi 5") for t in load_tasks("raspberry_pi4") if "name" in t]
        self.assertEqual(pi4, pi5)

    def test_3bplus_has_every_pi4_task_apart_from_its_renames(self) -> None:
        pi4 = {THREEB_RENAMES.get(t["name"], t["name"]) for t in load_tasks("raspberry_pi4") if "name" in t and t["name"] not in DOCKER_TASKS}
        three = [t["name"] for t in load_tasks("raspberry_pi_3bplus") if "name" in t]
        self.assertEqual(len(three), len(set(three)), "duplicate task names")
        self.assertEqual(set(three), pi4, "the 3B+ role has drifted from the Pi 4 role (missing or extra tasks)")

    def test_tailscale_and_k3s_tasks_match_the_pi4_role_on_the_3bplus(self) -> None:
        pi4 = {t["name"]: t for t in load_tasks("raspberry_pi4") if "name" in t}
        three = {t["name"]: t for t in load_tasks("raspberry_pi_3bplus") if "name" in t}
        shared = [
            name for name, task in pi4.items()
            if set(task.get("tags") or []) & {"baseline", "tailscale"} and name not in ("Resolve structured K3s settings",)
        ]
        self.assertTrue(shared)
        for name in shared:
            self.assertEqual(
                canonical(three[name], "raspberry_pi_3bplus", "3B+"), canonical(pi4[name], "raspberry_pi4", "Pi 4"),
                f"'{name}' differs between the Pi 4 and 3B+ roles",
            )

    def test_every_role_has_its_own_defaults_with_the_board_tag(self) -> None:
        for profile, (directory, _label) in ROLES.items():
            defaults = yaml.safe_load((REPO_ROOT / "roles" / directory / "defaults" / "main.yml").read_text(encoding="utf-8"))
            self.assertIn(lw.BOARDS[profile]["tailscale_tag"], defaults[f"{directory}_tailscale_tags"], profile)
            for key in ("manage_system", "tailscale_manage", "tailscale_auth_key", "tailscale_hostname"):
                self.assertIn(f"{directory}_{key}", defaults, f"{profile} role defaults miss {key}")
