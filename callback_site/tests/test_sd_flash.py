"""Exercise flash safety with fake disk commands; never write a real device."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class SdFlashTests(unittest.TestCase):
    def run_flash(self, mode: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        script = Path(__file__).resolve().parents[2] / "scripts/lib/flash.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools = root / "bin"
            tools.mkdir()
            (root / "image").write_bytes(b"" if mode == "empty" else b"raw-image")
            snapshot = {"blockdevices": [{"name": "/dev/fake", "mountpoints": [None], "children": [
                {"name": "/dev/fake1", "mountpoints": ["/media/boot with spaces"]},
                {"name": "/dev/fake2", "mountpoints": ["/media/root"]},
            ]}]}
            (root / "snapshot").write_text(json.dumps(snapshot))
            commands = {
                "lsblk": """#!/bin/sh
if [ "$1" = -dnbo ]; then
  [ "$MODE" = capacity-fail ] && exit 2
  case "$MODE" in partition) echo 'part 100';; oversized) echo 'disk 4';; *) echo 'disk 100';; esac
  exit 0
fi
if [ "$1" != --json ]; then exit 0; fi
[ "$MODE" = inspect-fail ] && exit 2
[ "$MODE" = system-disk ] && { printf '{"blockdevices":[{"name":"/dev/fake","mountpoints":["/"]}]}'; exit 0; }
if [ -f "$TEST_ROOT/unmounted" ] && [ "$MODE" != still-mounted ]; then
  printf '{"blockdevices":[{"name":"/dev/fake","mountpoints":[null]}]}'
else
  cat "$TEST_ROOT/snapshot"
fi
""",
                "umount": """#!/bin/sh
echo "umount $2" >> "$TEST_ROOT/calls"
[ "$MODE" = busy ] && exit 1
[ "$2" = /dev/fake2 ] && touch "$TEST_ROOT/unmounted"
exit 0
""",
                "dd": '#!/bin/sh\necho dd >> "$TEST_ROOT/calls"\n',
                "sync": '#!/bin/sh\necho sync >> "$TEST_ROOT/calls"\n',
                "blockdev": '#!/bin/sh\necho blockdev >> "$TEST_ROOT/calls"\n[ "$MODE" = flush-fail ] && exit 1\nexit 0\n',
                "cmp": '#!/bin/sh\necho cmp >> "$TEST_ROOT/calls"\n[ "$MODE" = readback-fail ] && exit 1\nexit 0\n',
                "partprobe": '#!/bin/sh\necho partprobe >> "$TEST_ROOT/calls"\n[ "$MODE" = reread-fail ] && exit 1\nexit 0\n',
                "udevadm": '#!/bin/sh\necho udevadm >> "$TEST_ROOT/calls"\n',
                "mount": '#!/bin/sh\necho mount >> "$TEST_ROOT/calls"\nexit 1\n',
            }
            for name, content in commands.items():
                path = tools / name
                path.write_text(content)
                path.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; PROFILE=pi5; DEVICE=/dev/fake; AUTO_CONFIRM=true; '
                 'TMP_DIR="$TEST_ROOT"; IMG_TO_WRITE="$TEST_ROOT/image"; source "$1"', "test", str(script)],
                env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}",
                     "TEST_ROOT": tmp, "MODE": mode}, capture_output=True, text=True, timeout=10,
            )
            calls = (root / "calls").read_text().splitlines() if (root / "calls").exists() else []
            return result, calls

    def test_unmount_failure_aborts_before_overwriting(self) -> None:
        result, calls = self.run_flash("busy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertNotIn("dd", calls)

    def test_remaining_mount_aborts_before_overwriting(self) -> None:
        result, calls = self.run_flash("still-mounted")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still has mounted filesystems", result.stderr)
        self.assertNotIn("dd", calls)

    def test_inspection_failure_aborts_before_overwriting(self) -> None:
        result, calls = self.run_flash("inspect-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("dd", calls)

    def test_unmounts_all_partitions_and_refreshes_after_flush(self) -> None:
        result, calls = self.run_flash("ok")
        self.assertEqual(calls, ["umount /dev/fake1", "umount /dev/fake2", "dd", "sync", "blockdev", "cmp", "partprobe", "udevadm"])
        self.assertIn("Could not find boot partition", result.stderr)

    def test_partition_refresh_failure_prevents_mount(self) -> None:
        result, calls = self.run_flash("reread-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("udevadm", calls)
        self.assertNotIn("mount", calls)

    def test_system_disk_and_partition_are_rejected_before_unmounting(self) -> None:
        for mode in ("system-disk", "partition", "oversized", "empty", "capacity-fail"):
            with self.subTest(mode=mode):
                result, calls = self.run_flash(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_flush_or_readback_failure_prevents_injection(self) -> None:
        for mode in ("flush-fail", "readback-fail"):
            with self.subTest(mode=mode):
                result, calls = self.run_flash(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("partprobe", calls)
                self.assertNotIn("mount", calls)
                if mode == "readback-fail":
                    self.assertIn("card is not ready", result.stderr)

    def test_readback_compares_every_image_byte_but_not_unused_card_space(self) -> None:
        script = (Path(__file__).resolve().parents[2] / "scripts/lib/flash.sh").read_text()
        check = script[script.index('echo "Verifying the entire'):script.index('if ! partprobe')]
        for contents, valid in ((b"raw-imageunused-card-space", True), (b"raw-imagX", False), (b"raw", False)):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                image = root / "image"
                target = root / "fake-device"
                image.write_bytes(b"raw-image")
                target.write_bytes(contents)
                tool = root / "blockdev"
                tool.write_text("#!/bin/sh\nexit 0\n")
                tool.chmod(0o755)
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail; IMG_TO_WRITE="$1"; DEVICE="$2"; IMAGE_BYTES=9; ' + check,
                     "test", str(image), str(target)],
                    env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode == 0, valid, result.stderr)
                if not valid:
                    self.assertIn("Image readback verification failed", result.stderr)

    def test_device_lock_refuses_a_second_writer(self) -> None:
        script = (Path(__file__).resolve().parents[2] / "scripts/prepare_sd_cloud_init.sh").read_text()
        lock = script[script.index('# Serialize writers'):script.index('# shellcheck source=lib/image.sh')]
        with tempfile.TemporaryDirectory() as tmp:
            device = Path(tmp) / "fake-device"
            device.touch()
            lock_dir = Path(tmp) / "locks"
            lock_dir.mkdir()
            lock = lock.replace('DEVICE_LOCK_DIR=/run/playbook-sd-locks', f'DEVICE_LOCK_DIR="{lock_dir}"')
            lock_id = subprocess.check_output(["stat", "-Lc", "%t-%T", str(device)], text=True).strip()
            with (lock_dir / f"{lock_id}.lock").open("w") as held:
                import fcntl
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail; DEVICE="$1"; ' + lock, "test", str(device)],
                    capture_output=True, text=True, timeout=10,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing concurrent writes", result.stderr)
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; DEVICE="$1"; ' + lock, "test", str(device)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # A disk lock held by udev must not prevent our independent writer lock.
            with device.open("rb") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail; DEVICE="$1"; ' + lock, "test", str(device)],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            alias = Path(tmp) / "device-alias"
            alias.symlink_to(device)
            with (lock_dir / f"{lock_id}.lock").open("w") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail; DEVICE="$1"; ' + lock, "test", str(alias)],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing concurrent writes", result.stderr)

    def test_render_and_schema_validation_precede_flash(self) -> None:
        try:
            from .sd_script import read_sd_script
        except ImportError:
            from sd_script import read_sd_script
        script = read_sd_script()
        self.assertLess(script.index('cloud-init schema --config-file'), script.index('dd if='))
        self.assertLess(script.index('toolkit_render network >'), script.index('dd if='))
        self.assertLess(script.index('Invalid CLOUD_INIT_USERNAME'), script.index('dd if='))

    def test_invalid_schema_aborts_without_touching_device(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts/lib/validate.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = root / "cloud-init"
            tool.write_text("#!/bin/sh\nexit 1\n")
            tool.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", 'set -euo pipefail; TMP_DIR="$TEST_ROOT"; META_DATA_OUT="$TMP_DIR/meta-data"; '
                 'USER_DATA_OUT="$TMP_DIR/user-data"; INSTANCE_ID=test; HOSTNAME=test; PROFILE=pi3bplus; '
                 'VALIDATE_CLOUD_INIT_SCHEMA=true; source "$1"; echo device-touched', "test", str(script)],
                env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "TEST_ROOT": tmp},
                capture_output=True, text=True, timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("schema validation failed", result.stderr)
            self.assertNotIn("device-touched", result.stdout)

    def test_seed_is_verified_after_unmounting_and_reopening_read_only(self) -> None:
        script = Path(__file__).resolve().parents[2] / "scripts/lib/finish.sh"
        for corrupt in ("none", "user-data", "meta-data", "network-config"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tools = root / "bin"
                tools.mkdir()
                (root / "boot/boot/firmware").mkdir(parents=True)
                for name in ("user-data", "meta-data", "network-config"):
                    (root / name).write_text(f"valid-{name}\n")
                for name, body in {
                    "sync": 'echo sync >> "$TEST_ROOT/calls"',
                    "umount": 'echo umount >> "$TEST_ROOT/calls"',
                    "blockdev": 'echo blockdev >> "$TEST_ROOT/calls"',
                    "mount": 'echo "$*" >> "$TEST_ROOT/calls"; [ "$CORRUPT" = none ] || : > "$TEST_ROOT/boot/$CORRUPT"',
                }.items():
                    tool = tools / name
                    tool.write_text("#!/bin/sh\n" + body + "\n")
                    tool.chmod(0o755)
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail; TMP_DIR="$TEST_ROOT"; USER_DATA_OUT="$TMP_DIR/user-data"; '
                     'META_DATA_OUT="$TMP_DIR/meta-data"; PROFILE=network-toolkit; DISTRIBUTION=standard; '
                     'HOSTNAME=test; DEVICE=/dev/fake; BOOT_PART=/dev/fake1; source "$1"', "test", str(script)],
                    env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "TEST_ROOT": tmp, "CORRUPT": corrupt},
                    capture_output=True, text=True, timeout=10,
                )
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(calls[:4], ["sync", "umount", "blockdev", f"-o ro /dev/fake1 {root}/boot"])
                self.assertEqual(result.returncode == 0, corrupt == "none", result.stderr)
                self.assertEqual("SD card is ready" in result.stdout, corrupt == "none")
