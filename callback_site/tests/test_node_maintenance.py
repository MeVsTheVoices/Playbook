"""Basic Node Ops maintenance modules, exercised without touching host services."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile

import yaml

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

ROOT = Path(__file__).resolve().parents[2]


class NodeMaintenanceTests(BaseCallbackSiteTest):
    def run_role(self, profile: str, variables: dict, check: bool = False) -> subprocess.CompletedProcess:
        ansible = shutil.which("ansible-playbook")
        if not ansible:
            self.skipTest("ansible-playbook is required for local maintenance tests")
        with tempfile.TemporaryDirectory() as tmp:
            play = Path(tmp) / "play.yml"
            play.write_text(yaml.safe_dump([{
                "hosts": "all", "gather_facts": False,
                "vars": {"provision_profile": profile, **variables},
                "roles": [{"role": str(ROOT / "roles/node_maintenance")}],
            }]))
            return subprocess.run(
                [ansible, "-i", "localhost,", "-c", "local", str(play)] + (["--check"] if check else []),
                cwd=ROOT, env=os.environ.copy(), text=True, capture_output=True, timeout=60,
            )

    def test_profiles_expose_basic_controls(self) -> None:
        from services.node_state import _profile_map
        profiles = _profile_map()
        self.assertEqual(profiles["cgroups"]["tags"], ["cgroups"])
        self.assertNotIn("playbook_server_active", profiles["cgroups"]["groups"])
        self.assertIn("playbook_server_active", profiles["systemd"]["groups"])
        specs = {spec["id"]: spec for spec in profiles["systemd"]["input_specs"]}
        self.assertTrue(specs["service_name"]["required"])
        self.assertEqual(specs["service_state"]["options"], ["started", "stopped"])
        tasks = yaml.safe_load((ROOT / "roles/node_maintenance/tasks/systemd.yml").read_text())
        self.assertEqual(set(tasks[-1]["ansible.builtin.systemd"]), {"name", "state", "enabled"})

    def test_cgroups_preserves_arguments_checks_without_writing_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp) / "cmdline.txt"
            original = "console=serial0,115200 root=PARTUUID=abc rootwait cgroup_memory=0 cgroup_enable=memory\n"
            boot.write_text(original)
            variables = {"group_names": ["rpi5_active"], "node_maintenance_cmdline_paths": [
                str(Path(tmp) / "missing.txt"), str(boot),
            ]}
            preview = self.run_role("cgroups", variables, check=True)
            self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
            self.assertEqual(boot.read_text(), original)
            applied = self.run_role("cgroups", variables)
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            expected = "console=serial0,115200 root=PARTUUID=abc rootwait cgroup_enable=cpuset cgroup_enable=memory cgroup_memory=1\n"
            self.assertEqual(boot.read_text(), expected)
            again = self.run_role("cgroups", variables)
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertIn("changed=0", again.stdout)
            self.assertEqual(boot.read_text(), expected)

    def test_cgroups_rejects_multiline_or_missing_boot_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            boot = Path(tmp) / "cmdline.txt"
            variables = {"group_names": ["rpi4_active"], "node_maintenance_cmdline_paths": [str(boot)]}
            missing = self.run_role("cgroups", variables)
            self.assertNotEqual(missing.returncode, 0)
            self.assertFalse(boot.exists())
            boot.write_text("root=abc\nanother=line\n")
            invalid = self.run_role("cgroups", variables)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(boot.read_text(), "root=abc\nanother=line\n")

    def test_systemd_rejects_unsafe_units_and_actions_before_managing_services(self) -> None:
        for inputs in (
            {"service_name": "ssh.service;reboot", "service_state": "started", "service_enabled": "true"},
            {"service_name": "ssh.service", "service_state": "restarted", "service_enabled": "true"},
            {"service_name": "ssh.service", "service_state": "started", "service_enabled": "invalid"},
        ):
            with self.subTest(inputs=inputs):
                result = self.run_role("systemd", {"provision_inputs": inputs}, check=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Apply selected service runtime and boot state", result.stdout)

    def test_unselected_role_does_not_run_maintenance(self) -> None:
        result = self.run_role("baseline", {})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("changed=0", result.stdout)
