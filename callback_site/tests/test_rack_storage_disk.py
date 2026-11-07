"""rack_storage_disk: disk selection, fstab edits and by-id lookup, using the lsblk layout of a real Pi 5 with a blank NVMe."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("rack_storage_disk", REPO_ROOT / "library" / "rack_storage_disk.py")
disk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(disk)

GIB = 1024 ** 3


def sd_card() -> dict:
    return {
        "name": "/dev/mmcblk0", "type": "disk", "size": 62 * GIB, "rm": False, "ro": False,
        "children": [
            {"name": "/dev/mmcblk0p1", "type": "part", "fstype": "vfat", "mountpoints": ["/boot/firmware"]},
            {"name": "/dev/mmcblk0p2", "type": "part", "fstype": "ext4", "mountpoints": ["/"]},
        ],
    }


def blank_nvme(name: str = "/dev/nvme0n1") -> dict:
    return {"name": name, "type": "disk", "size": 128 * GIB, "rm": False, "ro": False, "fstype": None, "mountpoints": [None]}


def prepared_nvme(mounted_at: str | None = None) -> dict:
    return {
        "name": "/dev/nvme0n1", "type": "disk", "size": 128 * GIB, "rm": False, "ro": False, "mountpoints": [None],
        "children": [{
            "name": "/dev/nvme0n1p1", "type": "part", "fstype": "ext4", "label": "longhorn",
            "uuid": "aaaa-bbbb", "mountpoints": [mounted_at],
        }],
    }


LOOP = {"name": "/dev/loop0", "type": "loop", "size": 40_000_000, "rm": False, "ro": True, "fstype": "squashfs", "mountpoints": ["/snap/snapd/1"]}
PATH = "/var/lib/longhorn"


class PlanTests(unittest.TestCase):
    def test_auto_picks_the_single_blank_disk_and_skips_the_sd_card(self) -> None:
        plan = disk.make_plan([LOOP, sd_card(), blank_nvme()], "auto", PATH, "longhorn")
        self.assertEqual(plan["action"], "format")
        self.assertEqual(plan["disk"]["name"], "/dev/nvme0n1")

    def test_auto_fails_when_no_disk_is_blank(self) -> None:
        with self.assertRaisesRegex(disk.PlanError, "no blank disk"):
            disk.make_plan([LOOP, sd_card()], "auto", PATH, "longhorn")

    def test_auto_refuses_to_guess_between_two_blank_disks(self) -> None:
        with self.assertRaisesRegex(disk.PlanError, "more than one blank disk"):
            disk.make_plan([sd_card(), blank_nvme(), blank_nvme("/dev/sda")], "auto", PATH, "longhorn")

    def test_removable_disks_are_not_auto_picked(self) -> None:
        stick = {**blank_nvme("/dev/sda"), "rm": "1"}
        with self.assertRaisesRegex(disk.PlanError, "no blank disk"):
            disk.make_plan([sd_card(), stick], "auto", PATH, "longhorn")

    def test_a_disk_prepared_earlier_is_mounted_not_reformatted(self) -> None:
        plan = disk.make_plan([sd_card(), prepared_nvme()], "auto", PATH, "longhorn")
        self.assertEqual(plan["action"], "mount")
        self.assertEqual(plan["node"]["uuid"], "aaaa-bbbb")

    def test_an_existing_mount_is_reported_as_present(self) -> None:
        plan = disk.make_plan([sd_card(), prepared_nvme(PATH)], "auto", PATH, "longhorn")
        self.assertEqual(plan["action"], "present")

    def test_a_mount_from_another_disk_than_the_requested_one_is_an_error(self) -> None:
        with self.assertRaisesRegex(disk.PlanError, "already mounted"):
            disk.make_plan([sd_card(), prepared_nvme(PATH), blank_nvme("/dev/sda")], "/dev/sda", PATH, "longhorn")

    def test_a_named_disk_with_data_is_refused(self) -> None:
        with self.assertRaisesRegex(disk.PlanError, "refusing to touch"):
            disk.make_plan([sd_card(), blank_nvme()], "/dev/mmcblk0", PATH, "longhorn")

    def test_a_named_disk_that_does_not_exist_is_an_error(self) -> None:
        with self.assertRaisesRegex(disk.PlanError, "not a block device"):
            disk.make_plan([sd_card()], "/dev/nope", PATH, "longhorn")

    def test_a_disk_with_a_filesystem_under_another_label_is_refused(self) -> None:
        other = prepared_nvme()
        other["children"][0]["label"] = "photos"
        with self.assertRaisesRegex(disk.PlanError, "refusing to touch"):
            disk.make_plan([sd_card(), other], "/dev/nvme0n1", PATH, "longhorn")

    def test_old_lsblk_single_mountpoint_key_is_understood(self) -> None:
        self.assertEqual(disk.mounts_of({"mountpoint": "/boot"}), ["/boot"])
        self.assertEqual(disk.mounts_of({"mountpoint": None}), [])


class FstabTests(unittest.TestCase):
    OPTIONS = "defaults,noatime,nofail"
    LINE = f"UUID=aaaa-bbbb {PATH} ext4 {OPTIONS} 0 2"

    def test_adds_one_entry_by_uuid(self) -> None:
        text, changed = disk.update_fstab("LABEL=writable / ext4 defaults 0 1\n", "aaaa-bbbb", PATH, "ext4", self.OPTIONS)
        self.assertTrue(changed)
        self.assertEqual(text.splitlines(), ["LABEL=writable / ext4 defaults 0 1", self.LINE])

    def test_a_second_run_changes_nothing(self) -> None:
        first, _ = disk.update_fstab("", "aaaa-bbbb", PATH, "ext4", self.OPTIONS)
        second, changed = disk.update_fstab(first, "aaaa-bbbb", PATH, "ext4", self.OPTIONS)
        self.assertFalse(changed)
        self.assertEqual(first, second)

    def test_replaces_a_stale_entry_for_the_same_mount_point(self) -> None:
        text, changed = disk.update_fstab(f"UUID=old {PATH} ext4 defaults 0 2\n# keep me\n", "aaaa-bbbb", PATH, "ext4", self.OPTIONS)
        self.assertTrue(changed)
        self.assertEqual(text.splitlines(), ["# keep me", self.LINE])

    def test_missing_trailing_newline_is_not_a_change(self) -> None:
        _, changed = disk.update_fstab(self.LINE, "aaaa-bbbb", PATH, "ext4", self.OPTIONS)
        self.assertFalse(changed)


class ByIdTests(unittest.TestCase):
    def test_prefers_a_named_link_over_the_eui_one(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "nvme0n1p1"
            target.touch()
            by_id = Path(root) / "by-id"
            by_id.mkdir()
            for name in ("nvme-eui.0000000000000000541d610000000000-part1", "nvme-FIKWOT_FX520_128GB_2605-part1"):
                os.symlink(target, by_id / name)
            found = disk.find_by_id(os.path.realpath(target), str(by_id))
            self.assertEqual(Path(found).name, "nvme-FIKWOT_FX520_128GB_2605-part1")

    def test_returns_none_without_a_link(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(disk.find_by_id("/dev/vda1", root))


class RoleUsageTests(unittest.TestCase):
    def test_the_pi5_role_only_passes_parameters_the_module_accepts(self) -> None:
        tasks = yaml.safe_load((REPO_ROOT / "roles" / "raspberry_pi5" / "tasks" / "main.yml").read_text(encoding="utf-8"))
        task = next(t for t in tasks if t.get("name") == "Prepare the Longhorn storage disk")
        documented = set(yaml.safe_load(disk.DOCUMENTATION)["options"])
        self.assertLessEqual(set(task["rack_storage_disk"]), documented)


if __name__ == "__main__":
    unittest.main()
