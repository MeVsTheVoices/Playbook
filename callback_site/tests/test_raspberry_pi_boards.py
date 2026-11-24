"""One raspberry_pi role serves every board; these tests guard the board vars files and how site.yml picks them."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "callback_site"))

import k3s_lightweight as lw  # noqa: E402

ROLE = REPO_ROOT / "roles" / "raspberry_pi"
# Board-only task groups, gated by a flag in each board's vars file.
GATED = {"docker": "raspberry_pi_supports_docker", "longhorn": "raspberry_pi_supports_longhorn"}


def board_vars(profile: str) -> dict:
    return yaml.safe_load((ROLE / "vars" / f"{profile}.yml").read_text(encoding="utf-8"))


class RaspberryPiBoardTests(unittest.TestCase):
    def test_every_board_vars_file_defines_the_same_keys(self) -> None:
        files = sorted(p.stem for p in (ROLE / "vars").glob("*.yml"))
        self.assertEqual(set(files), set(lw.BOARDS))
        keys = {profile: set(board_vars(profile)) for profile in files}
        for profile in files:
            self.assertEqual(keys[profile], keys[files[0]], f"{profile} vars differ from {files[0]}")

    def test_board_vars_match_the_python_registry(self) -> None:
        for profile, board in lw.BOARDS.items():
            data = board_vars(profile)
            self.assertEqual(data["raspberry_pi_worker_role"], board["role"], profile)
            self.assertEqual(data["raspberry_pi_board_tailscale_tags"], [board["tailscale_tag"]], profile)
            self.assertEqual(data["raspberry_pi_k3s_taint_default"] == "true", bool(board["taint"]), profile)
            self.assertEqual(data["raspberry_pi_k3s_max_pods_default"], board["max_pods"], profile)
            self.assertEqual(data["raspberry_pi_k3s_disable_default"] == "true", bool(board["disable_defaults"]), profile)
        self.assertTrue(board_vars("pi5")["raspberry_pi_supports_longhorn"])
        self.assertFalse(board_vars("pi4")["raspberry_pi_supports_longhorn"])
        self.assertFalse(board_vars("pi3bplus")["raspberry_pi_supports_docker"])

    def test_board_vars_are_never_user_settings(self) -> None:
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
        for profile in lw.BOARDS:
            self.assertFalse(set(board_vars(profile)) & set(defaults), f"{profile} vars would override group_vars")

    def test_role_loads_the_board_first_for_every_tag(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
        self.assertIn("raspberry_pi_board", str(tasks[0]["ansible.builtin.assert"]["that"]))
        self.assertEqual(tasks[1]["ansible.builtin.include_vars"]["file"], "{{ raspberry_pi_board }}.yml")
        self.assertEqual(tasks[0]["tags"], ["always"])
        self.assertEqual(tasks[1]["tags"], ["always"])

    def test_board_only_tasks_are_gated_first(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
        for task in tasks:
            for tag, flag in GATED.items():
                if (task.get("tags") or []) == [tag]:
                    self.assertEqual(task["when"][0], f"{flag} | bool", task["name"])

    def test_site_plays_assign_the_expected_boards(self) -> None:
        plays = {play["hosts"]: play for play in yaml.safe_load((REPO_ROOT / "site.yml").read_text(encoding="utf-8"))}
        expected = {"rpi5_active": "pi5", "rpi4_active": "pi4", "rpi_3bplus_active": "pi3bplus", "network_toolkit_active": "pi3bplus"}
        for hosts, profile in expected.items():
            roles = plays[hosts]["roles"]
            self.assertEqual(roles[0]["role"], "raspberry_pi", hosts)
            self.assertEqual(roles[0]["vars"]["raspberry_pi_board"], profile, hosts)
        self.assertEqual([r["role"] for r in plays["network_toolkit_active"]["roles"]], ["raspberry_pi", "network_toolkit"])
        for profile, board in lw.BOARDS.items():
            self.assertEqual(expected[board["group"]], profile)


if __name__ == "__main__":
    unittest.main()
