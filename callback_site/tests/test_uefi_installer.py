"""Validate destructive destination policy and unattended installer generation."""

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil
from unittest.mock import patch
import sys

try:
    from . import test_support
except ImportError:
    import test_support

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from domain_defaults import IMAGE_CATALOG
from flash_helper.installer import seed, patch_grub
from flash_helper.installer_target import choose_disks
from services.installer_config import validate
from services.sd_prepare import _sanitize_sd_prepare_payload


class UefiInstallerTests(unittest.TestCase):
    def form(self, family="ubuntu"):
        entry = next(entry for entry in IMAGE_CATALOG if entry.get("installer_family") == family)
        return {
            "workflow": "uefi-installer", "catalog_id": entry["id"], "profile": entry["profiles"][0],
            "install_authorized": True, "hostname": "rack-uefi", "distribution": "standard",
            "callback_url": "http://example.invalid/api/cloud-init/callback",
            "ssh_public_key": "ssh-ed25519 dGVzdA==", "cloud_init_options": {},
        }

    def test_catalog_includes_both_architectures_and_official_pinned_checksums(self):
        entries = [entry for entry in IMAGE_CATALOG if entry.get("workflow") == "uefi-installer"]
        for distro, release in (("Ubuntu", "22.04 LTS"), ("Ubuntu", "24.04 LTS"), ("Ubuntu", "26.04 LTS"),
                               ("Rocky Linux", "9"), ("Rocky Linux", "10")):
            selected = [entry for entry in entries if entry["distro"] == distro and entry["release"] == release]
            self.assertEqual({entry["arch"] for entry in selected}, {"amd64", "arm64"})
            for entry in selected:
                self.assertRegex(entry["image_sha256"], r"^[a-f0-9]{64}$")
                self.assertTrue(entry["image_url"].endswith(".iso"))
                self.assertTrue(all(profile.startswith("uefi-") for profile in entry["profiles"]))

    def test_explicit_authorization_and_catalog_workflow_match_are_required(self):
        for change in ({"install_authorized": False}, {"install_authorized": "true"}, {"workflow": "pi"},
                       {"profile": "pi5"}, {"image_url": "https://example.invalid/evil.iso"},
                       {"distribution": "k3s"}, {"cloud_init_options": {"install_telegraf": True}}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    validate({**self.form(), **change})
        cleaned = _sanitize_sd_prepare_payload(self.form())
        self.assertNotIn("config_errors", cleaned)
        self.assertEqual(cleaned["cloud_init_options"]["username"], "rackadmin")
        self.assertIn("image_sha256", cleaned)

    def test_helper_accepts_uefi_contract_and_pins_the_catalog_source(self):
        from flash_helper import server
        form = self.form()
        entry = validate(form)
        form.update(job_id="a" * 32, device="/dev/disk/by-id/usb-installer",
                    confirm_device="/dev/disk/by-id/usb-installer", device_identity="b" * 64)
        cleaned = server._validate(form)
        self.assertEqual(cleaned["workflow"], "uefi-installer")
        self.assertEqual(cleaned["image_sha256"], entry["image_sha256"])
        self.assertTrue(cleaned["install_authorized"])

    def test_portal_accepts_uefi_only_when_helper_and_both_authorizations_are_present(self):
        from services import flash_helper
        form = self.form()
        form.update(device="/dev/disk/by-id/usb-installer", confirm_device="/dev/disk/by-id/usb-installer",
                    device_identity="b" * 64)
        client = test_support.app.test_client()
        with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": "http://helper:8090"}), \
             patch.object(flash_helper, "devices", return_value=[
                 {"path": form["device"], "identity": form["device_identity"]}]), \
             patch.object(flash_helper, "call", return_value={"devices": [
                 {"path": form["device"], "identity": form["device_identity"]}]}), \
             patch("routes.sd._auto_generate_sd_option_profile", return_value="test-uefi"), \
             patch("routes.sd._retain_sd_session_secrets"), patch("routes.sd.threading.Thread") as thread:
            response = client.post("/api/sd/prepare", json=form)
            self.assertEqual(response.status_code, 200, response.get_json())
            prepared = thread.call_args.kwargs["args"][1]
            self.assertEqual(prepared["workflow"], "uefi-installer")
            self.assertEqual(prepared["image_sha256"], next(e for e in IMAGE_CATALOG if e["id"] == form["catalog_id"])["image_sha256"])
            invalid = client.post("/api/sd/prepare", json={**form, "install_authorized": False})
            self.assertEqual(invalid.status_code, 400)
        with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": ""}):
            response = client.post("/api/sd/prepare", json=form)
            self.assertEqual(response.status_code, 400)

    def snapshot(self):
        return {"blockdevices": [
            {"path": "/dev/sda", "type": "disk", "size": 8 * 1024**3, "tran": "usb", "rm": True,
             "children": [{"path": "/dev/sda1", "mountpoints": ["/cdrom"]}]},
            {"path": "/dev/nvme0n1", "type": "disk", "size": 128 * 1024**3, "tran": "nvme",
             "serial": "disk-one", "mountpoints": [], "children": [
                 {"path": "/dev/nvme0n1p1", "type": "part", "mountpoints": []}]},
        ]}

    def test_destination_is_exactly_one_internal_disk_and_never_installer(self):
        snapshot = self.snapshot()
        self.assertEqual(choose_disks(snapshot)["path"], "/dev/nvme0n1")
        for mutation in ("two-disks", "no-disk", "no-media", "mounted", "no-identity", "readonly", "usb"):
            test = copy.deepcopy(snapshot)
            disk = test["blockdevices"][1]
            if mutation == "two-disks":
                test["blockdevices"].append({**disk, "path": "/dev/sdb", "serial": "disk-two"})
            elif mutation == "no-disk":
                test["blockdevices"].pop()
            elif mutation == "no-media":
                test["blockdevices"].pop(0)
            elif mutation == "mounted":
                disk["children"][0]["mountpoints"] = ["/home"]
            elif mutation == "no-identity":
                disk["serial"] = ""
            elif mutation == "readonly":
                disk["ro"] = True
            else:
                disk["tran"] = "usb"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                choose_disks(test)

    def test_ubuntu_seed_has_no_interaction_and_disk_preflight_precedes_storage(self):
        form = self.form()
        validate(form)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed(form, "ubuntu", root)
            config = yaml.safe_load((root / "nocloud/user-data").read_text())["autoinstall"]
            self.assertEqual(config["interactive-sections"], [])
            self.assertEqual(config["shutdown"], "poweroff")
            self.assertIn("installer_target.py ubuntu", config["early-commands"][0])
            self.assertEqual(config["storage"]["layout"]["match"]["serial"], "PLAYBOOK-NO-DISK-UNTIL-PREFLIGHT")
            self.assertFalse(config["ssh"]["allow-pw"])
            self.assertEqual(config["ssh"]["authorized-keys"], ["ssh-ed25519 dGVzdA=="])

    def test_rocky_kickstart_runs_preflight_without_main_argument_bug(self):
        form = self.form("rocky")
        validate(form)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed(form, "rocky", root)
            kickstart = (root / "ks.cfg").read_text()
            self.assertIn("%pre --interpreter=/usr/bin/python3 --erroronfail", kickstart)
            self.assertIn("%include /tmp/playbook-storage.ks", kickstart)
            pre = kickstart.split("%pre --interpreter=/usr/bin/python3 --erroronfail\n")[1].split("%end")[0]
            compile(pre, "kickstart-pre", "exec")
            self.assertNotIn("sys.argv[1]", pre)
            self.assertTrue(kickstart.endswith("poweroff\n"))
            self.assertNotIn("reboot", kickstart)

    def test_grub_preserves_boot_arguments_but_removes_media_check(self):
        source = 'set default="1"\nset timeout=60\nlinuxefi /images/pxeboot/vmlinuz inst.stage2=hd:LABEL=Rocky rd.live.check quiet\n'
        patched = patch_grub(source, "rocky")
        self.assertIn("inst.ks=cdrom:/ks.cfg", patched)
        self.assertIn("inst.stage2=hd:LABEL=Rocky", patched)
        self.assertNotIn("rd.live.check", patched)
        self.assertNotIn('set default="1"', patched)
        self.assertIn("set default=0", patched)
        self.assertIn("set timeout=3", patched)
        ubuntu = patch_grub(" linux /casper/vmlinuz quiet ---\n", "ubuntu")
        self.assertIn(r"autoinstall ds=nocloud\;s=/cdrom/nocloud/ ---", ubuntu)
        with self.assertRaises(ValueError):
            patch_grub("unexpected layout", "ubuntu")

    def test_installer_preflight_handles_wrapped_and_unwrapped_ubuntu_configs(self):
        from flash_helper import installer_target
        snapshot = self.snapshot()
        cases = (
            ("ID_SERIAL=Samsung_SSD_970_EVO_disk-one\nID_SERIAL_SHORT=disk-one\n", {"serial": "Samsung_SSD_970_EVO_disk-one"}),
            ("ID_SERIAL=Odd*Disk?[1]\n", {"serial": "Odd[*]Disk[?][[]1]"}),
            ("ID_SERIAL_SHORT=disk-one\n", {"path": "/dev/nvme0n1"}),
        )
        for udev, expected in cases:
            for wrapped in (True, False):
                document = {"storage": {"layout": {"name": "direct", "match": {"serial": "BLOCKED"}}}}
                if wrapped:
                    document = {"autoinstall": document}
                target = unittest.mock.Mock()
                target.read_text.return_value = yaml.safe_dump(document)

                def run(command, udev=udev, **kwargs):
                    if command[0] == "udevadm":
                        self.assertEqual(command[-1], "--name=/dev/nvme0n1")
                        return unittest.mock.Mock(stdout=f"DEVNAME=/dev/nvme0n1\n{udev}")
                    return unittest.mock.Mock(stdout="{}")

                with self.subTest(udev=udev, wrapped=wrapped), \
                     patch.object(installer_target, "choose_disks", return_value=snapshot["blockdevices"][1]), \
                     patch.object(installer_target.subprocess, "run", side_effect=run), \
                     patch.object(installer_target, "Path", return_value=target):
                    installer_target.main("ubuntu")
                    output = yaml.safe_load(target.write_text.call_args.args[0])
                    storage = output.get("autoinstall", output)["storage"]
                    self.assertEqual(storage["layout"]["match"], expected)

    @unittest.skipUnless(shutil.which("xorriso"), "xorriso is required for actual ISO remaster validation")
    def test_iso_remaster_preserves_efi_boot_catalog_and_embeds_seeds(self):
        from flash_helper.installer import build_iso
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for family in ("ubuntu", "rocky"):
                media = root / family
                grub = media / ("boot/grub" if family == "ubuntu" else "EFI/BOOT")
                grub.mkdir(parents=True)
                (grub / "grub.cfg").write_text("set timeout=60\nlinux /vmlinuz quiet ---\n")
                if family == "ubuntu":
                    (grub / "loopback.cfg").write_text("linux /vmlinuz quiet ---\n")
                (media / "efi.img").write_bytes(b"\x00" * 1024 * 1024)
                source, output = root / (family + ".iso"), root / (family + "-out.iso")
                subprocess.run(["xorriso", "-as", "mkisofs", "-R", "-o", str(source),
                                "-e", "efi.img", "-no-emul-boot", str(media)],
                               check=True, capture_output=True)
                config = root / (family + "-seed")
                config.mkdir()
                form = self.form(family)
                validate(form)
                seed(form, family, config)
                build_iso(source, output, config, family)
                report = subprocess.run(["xorriso", "-indev", str(output), "-report_el_torito", "plain"],
                                        check=True, capture_output=True, text=True)
                self.assertIn("UEFI", report.stdout + report.stderr)
                extracted = root / (family + "-grub.cfg")
                subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(output), "-extract",
                                "/boot/grub/grub.cfg" if family == "ubuntu" else "/EFI/BOOT/grub.cfg",
                                str(extracted)], check=True, capture_output=True)
                self.assertIn("autoinstall" if family == "ubuntu" else "inst.ks=cdrom:/ks.cfg", extracted.read_text())
                extracted_seed = root / (family + "-extracted-seed")
                subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(output), "-extract",
                                "/nocloud/user-data" if family == "ubuntu" else "/ks.cfg",
                                str(extracted_seed)], check=True, capture_output=True)
                self.assertIn("autoinstall:" if family == "ubuntu" else "--erroronfail", extracted_seed.read_text())


if __name__ == "__main__":
    unittest.main()
