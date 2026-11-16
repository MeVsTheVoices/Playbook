"""Basic Node Ops maintenance modules, exercised without touching host services."""

from pathlib import Path
import json
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
    def run_role(
        self, profile: str, variables: dict, check: bool = False,
        environment: dict | None = None, extra_vars: dict | None = None,
    ) -> subprocess.CompletedProcess:
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
            command = [ansible, "-i", "localhost,", "-c", "local", str(play)]
            if check:
                command.append("--check")
            if extra_vars:
                command.extend(["--extra-vars", json.dumps(extra_vars)])
            return subprocess.run(command, cwd=ROOT, env={**os.environ, **(environment or {})},
                                  text=True, capture_output=True, timeout=60)

    def test_package_role_installs_and_uses_native_full_upgrade_arguments_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls.jsonl"
            (Path(tmp) / "maintenance_test_package.py").write_text("""
from ansible.module_utils.basic import AnsibleModule
import json, os
module = AnsibleModule(argument_spec={
    "name": {"type": "list", "elements": "str"},
    "state": {"type": "str"},
    "upgrade": {"type": "bool", "default": False},
    "update_cache": {"type": "bool"},
}, supports_check_mode=True)
with open(os.environ["PACKAGE_TEST_LOG"], "a") as out:
    out.write(json.dumps(module.params) + "\\n")
if os.environ.get("PACKAGE_TEST_FAIL") == "1":
    module.fail_json(msg="simulated package update failure")
module.exit_json(changed=False)
""")
            environment = {"ANSIBLE_LIBRARY": tmp, "PACKAGE_TEST_LOG": str(log)}
            variables = {"ansible_package_use": "maintenance_test_package", "provision_inputs": {"packages": "curl, git curl"}}
            result = self.run_role("package-manager", variables, environment=environment)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(len(calls), 1)
            self.assertEqual((calls[0]["name"], calls[0]["state"], calls[0]["upgrade"]), (["curl", "git"], "present", False))
            self.assertNotIn("TASK [node_maintenance : Reboot", result.stdout)
            variables["provision_inputs"] = {"action": "update-reboot"}
            for manager in ("apt", "dnf", "dnf5", "yum", "zypper", "pacman", "apk"):
                with self.subTest(manager=manager):
                    log.write_text("")
                    result = self.run_role("package-manager", variables, check=True, environment=environment,
                                           extra_vars={"ansible_facts": {"system": "Linux", "pkg_mgr": manager}})
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    self.assertEqual(len(calls), 1)
                    if manager in ("pacman", "apk"):
                        self.assertTrue(calls[0]["upgrade"])
                        self.assertIsNone(calls[0]["name"])
                    else:
                        self.assertEqual((calls[0]["name"], calls[0]["state"]), (["*"], "latest"))
                    self.assertIn("Reboot after successful updates", result.stdout)
                    self.assertIn("skipping: [localhost]", result.stdout)
            failed = self.run_role("package-manager", variables, check=True,
                                   environment={**environment, "PACKAGE_TEST_FAIL": "1"})
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("simulated package update failure", failed.stdout)
            self.assertNotIn("Reboot after successful updates", failed.stdout)

    def test_package_role_rejects_non_linux_and_unsafe_names_before_package_actions(self) -> None:
        for inputs, system in (
            ({"packages": "curl"}, "FreeBSD"), ({"packages": "--bad"}, "Linux"),
            ({"packages": "curl=1.0"}, "Linux"), ({}, "Linux"),
            ({"action": "reboot"}, "Linux"),
        ):
            with self.subTest(inputs=inputs, system=system):
                result = self.run_role("package-manager", {"provision_inputs": inputs}, check=True,
                                       extra_vars={"ansible_facts": {"system": system, "pkg_mgr": "apt"}})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Install selected packages", result.stdout)

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
