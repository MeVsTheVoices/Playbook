"""Generic x86_64 Ansible profile and removable UEFI installer workflow."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "callback_site"
for import_path in (ROOT, APP):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

import yaml
import domain_defaults
from services import node_inventory
from services.installer_config import validate as validate_installer
from services.node_state import _default_profiles_for_group
from services.sd_prepare import _sanitize_sd_prepare_payload


class GenericLinuxX64Tests(unittest.TestCase):
    def test_node_ops_profile_maps_to_a_safe_ansible_baseline(self):
        profile = next(p for p in domain_defaults.NODE_PROVISION_PROFILES if p["id"] == "linux-x64")
        self.assertEqual(profile["groups"], ["linux_x64_active"])
        self.assertIn("upgrade_packages", {spec["id"] for spec in profile["input_specs"]})
        self.assertFalse(next(spec for spec in profile["input_specs"] if spec["id"] == "upgrade_packages")["default"])
        self.assertEqual(_default_profiles_for_group("linux_x64_active"), ["linux-x64"])
        inventory = yaml.safe_load((ROOT / "inventory.yml").read_text())
        self.assertEqual(inventory["all"]["children"]["linux_x64"]["children"]["linux_x64_active"]["hosts"], {})
        self.assertTrue((ROOT / "roles/generic_linux_x64/defaults/main.yml").is_file())
        tasks = yaml.safe_load((ROOT / "roles/generic_linux_x64/tasks/main.yml").read_text())
        self.assertTrue(any("Install baseline packages" in task.get("name", "") for task in tasks))
        self.assertTrue(any("Optionally upgrade installed packages" in task.get("name", "") for task in tasks))
        self.assertFalse(any("k3s" in str(task).lower() for task in tasks))

    def test_x64_uefi_callback_enters_the_generic_node_ops_group(self):
        callbacks = {
            "lab-nuc": {
                "profile": "uefi-x86_64",
                "source_ip": "192.0.2.30",
                "source_ips": ["192.0.2.30"],
            }
        }
        with patch.object(node_inventory, "_inventory_nodes", return_value=[]), \
             patch.object(node_inventory, "_latest_callback_details_by_host", return_value=callbacks), \
             patch.object(node_inventory, "_load_node_provisioning", return_value={
                 "node_configs": {}, "node_inputs": {}, "results": {}, "manual_nodes": {},
             }), \
             patch.object(node_inventory, "active_nodes", side_effect=lambda nodes: nodes):
            node = node_inventory._node_ops_inventory_view()[0]
        self.assertEqual(node["group"], "linux_x64_active")
        self.assertEqual(node["callback_profile"], "uefi-x86_64")

    def test_x64_uefi_usb_pins_a_supported_image_and_requires_authorization(self):
        entry = next(item for item in domain_defaults.IMAGE_CATALOG if item.get("id") == "uefi-ubuntu-noble-amd64")
        self.assertEqual(entry["profiles"], ["uefi-x86_64"])
        self.assertEqual(entry["workflow"], "uefi-installer")
        payload = {
            "workflow": "uefi-installer",
            "catalog_id": entry["id"],
            "profile": "uefi-x86_64",
            "install_authorized": True,
            "distribution": "standard",
            "hostname": "lab-nuc",
            "image_url": entry["image_url"],
            "image_sha256": entry["image_sha256"],
            "cloud_init_options": {"username": "rackadmin", "timezone": "UTC", "locale": "en_US.UTF-8"},
        }
        selected = validate_installer(payload)
        self.assertEqual(selected["image_sha256"], entry["image_sha256"])
        cleaned = _sanitize_sd_prepare_payload(payload)
        self.assertEqual(cleaned["workflow"], "uefi-installer")
        self.assertEqual(cleaned["catalog_id"], entry["id"])
        for invalid in (
            {**payload, "install_authorized": False},
            {**payload, "distribution": "k3s"},
            {**payload, "cloud_init_options": {"k3s_role": "worker"}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_installer(invalid)

    def test_ui_explains_the_two_usb_destruction_stages_and_post_install_handoff(self):
        template = (APP / "templates/index.html").read_text()
        workflow = (APP / "static/home/catalog_filters.js").read_text()
        devices = (APP / "static/home/devices.js").read_text()
        submit = (APP / "static/home/init.js").read_text()
        self.assertIn("Generic Linux x86_64 (UEFI USB installer)", template)
        self.assertIn("Target removable device (SD card or USB stick)", template)
        self.assertIn("USB itself is erased when created", workflow)
        self.assertIn("?workflow=uefi-installer", devices)
        self.assertIn("erases the target's sole eligible internal disk", workflow)
        self.assertIn("generic Linux x86_64 baseline", workflow)
        self.assertNotIn("installer || flashSubmissionPending", workflow)
        self.assertNotIn("not available in this dashboard", submit)


if __name__ == "__main__":
    unittest.main()
