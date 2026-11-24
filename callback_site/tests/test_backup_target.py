"""S3 backup target module: the real role tasks render Garage's config and unit, and refuse key changes Garage cannot survive."""

from __future__ import annotations

import copy
import subprocess
import tempfile
from pathlib import Path

import yaml

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

from domain_defaults import NODE_PROVISION_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCESS = "GK" + "ab" * 16
SECRET = "cd" * 32
TASKS = (
    "Resolve backup target inputs",
    "Validate backup target inputs",
    "Read the existing backup target keys",
    "Refuse to change the backup target keys",
    "Write the backup target settings",
    "Write the backup target keys",
    "Write the backup target service",
)


class BackupTargetRoleTests(BaseCallbackSiteTest):
    def render(self, inputs: dict, existing_env: str | None = None) -> tuple[int, str, dict]:
        ansible = REPO_ROOT / ".venv" / "bin" / "ansible-playbook"
        if not ansible.exists():
            self.skipTest("repo ansible-playbook not available")
        role_tasks = yaml.safe_load((REPO_ROOT / "roles" / "rack_node" / "tasks" / "main.yml").read_text(encoding="utf-8"))
        by_name = {t["name"]: t for t in role_tasks if isinstance(t, dict) and "name" in t}
        defaults = yaml.safe_load((REPO_ROOT / "roles" / "rack_node" / "defaults" / "main.yml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            env_file = out / "garage.env"
            if existing_env is not None:
                env_file.write_text(existing_env, encoding="utf-8")
            selected = []
            for name in TASKS:
                task = copy.deepcopy(by_name[name])
                task.pop("tags", None)
                task.pop("no_log", None)
                if "ansible.builtin.slurp" in task:
                    task["ansible.builtin.slurp"]["src"] = str(env_file)
                if "ansible.builtin.copy" in task:
                    copy_args = task["ansible.builtin.copy"]
                    task["ansible.builtin.copy"] = {"dest": str(out / Path(copy_args["dest"]).name), "content": copy_args["content"]}
                selected.append(task)
            play = [{
                "hosts": "localhost",
                "gather_facts": False,
                "vars": {
                    **{k: v for k, v in defaults.items() if k.startswith("rack_node_garage_")},
                    "rack_node_supports_docker": True,
                    "rack_manage_with_ansible": True,
                    "provision_profile": "backup-target",
                    "rack_node_garage_tailscale_ip": {"stdout_lines": ["100.79.199.26"]},
                    "provision_inputs": {"garage_access_key": ACCESS, "garage_secret_key": SECRET, **inputs},
                },
                "tasks": selected,
            }]
            (out / "play.yml").write_text(yaml.safe_dump(play), encoding="utf-8")
            result = subprocess.run(
                [str(ansible), "-i", "localhost,", "-c", "local", str(out / "play.yml")],
                capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
            )
            files = {p.name: p.read_text(encoding="utf-8") for p in out.iterdir() if p.name != "play.yml"}
        return result.returncode, result.stdout + result.stderr, files

    def test_renders_garage_on_the_tailscale_address_with_keys_only_in_the_env_file(self) -> None:
        code, output, files = self.render({"garage_data_path": "/mnt/backup"})
        self.assertEqual(code, 0, output[-800:])
        self.assertIn('api_bind_addr = "100.79.199.26:3900"', files["garage.toml"])
        self.assertIn('s3_region = "garage"', files["garage.toml"])
        self.assertIn("-v /mnt/backup/meta:/var/lib/garage/meta", files["garage.service"])
        self.assertIn("--single-node --default-bucket", files["garage.service"])
        self.assertIn(f"GARAGE_DEFAULT_SECRET_KEY={SECRET}", files["garage.env"])
        self.assertIn("GARAGE_DEFAULT_BUCKET=k3s-etcd-snapshots", files["garage.env"])
        self.assertNotIn(SECRET, files["garage.service"] + files["garage.toml"])

        code, output, files = self.render({"garage_listen_address": "fd7a:115c::1"})
        self.assertEqual(code, 0, output[-800:])
        self.assertIn('api_bind_addr = "[fd7a:115c::1]:3900"', files["garage.toml"])

    def test_rejects_malformed_keys_and_refuses_to_swap_existing_ones(self) -> None:
        for bad in ({"garage_access_key": "AKIA123"}, {"garage_secret_key": "short"}, {"garage_bucket": "Bad_Bucket"}, {"garage_data_path": "relative"}):
            code, output, _files = self.render(bad)
            self.assertNotEqual(code, 0, bad)
            self.assertIn("needs Docker", output)

        same = f"GARAGE_DEFAULT_ACCESS_KEY={ACCESS}\nGARAGE_DEFAULT_SECRET_KEY={SECRET}\n"
        self.assertEqual(self.render({}, existing_env=same)[0], 0)
        code, output, _files = self.render({}, existing_env=same.replace(SECRET, "ef" * 32))
        self.assertNotEqual(code, 0)
        self.assertIn("already has a different access key or secret", output)

    def test_module_is_opt_in_for_docker_capable_boards_with_a_hidden_secret(self) -> None:
        profile = next(p for p in NODE_PROVISION_PROFILES if p["id"] == "backup-target")
        self.assertEqual(sorted(profile["groups"]), ["linux_x64_active", "rpi4_active", "rpi5_active"])
        specs = {spec["id"]: spec for spec in profile["input_specs"]}
        self.assertTrue(specs["garage_secret_key"]["sensitive"])
        self.assertFalse(specs["garage_access_key"].get("sensitive"))
