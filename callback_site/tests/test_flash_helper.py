"""Helper API, idle lifecycle, hardware policy and portal integration without disks."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flash_helper import devices, server
from services import flash_helper as client
from services import sd_prepare
from werkzeug.serving import make_server

TOKEN = "a" * 64
ALIAS = "/dev/disk/by-id/usb-test-reader"
IDENTITY = "b" * 64


def payload() -> dict:
    return {
        "job_id": "c" * 32, "profile": "pi5", "hostname": "helper-test",
        "distribution": "standard", "device": ALIAS, "confirm_device": ALIAS,
        "device_identity": IDENTITY, "ssh_public_key": "ssh-ed25519 dGVzdA==",
        "callback_url": "http://example.invalid/api/cloud-init/callback",
        "image_url": "https://example.invalid/image.img.xz", "image_sha256": "d" * 64,
        "cloud_init_options": {},
    }


class HelperApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lease = server.Lease(180)
        self.jobs = {}
        self.addCleanup(patch.stopall)
        patch.object(server, "JOBS", self.jobs).start()
        self.verify = patch.object(server, "verify").start()
        self.worker = patch.object(server, "_run_prepare_job").start()
        self.thread = patch.object(server.threading, "Thread").start()
        self.app = server.create_app(TOKEN, self.lease).test_client()
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def test_auth_required_and_probes_do_not_extend_idle_lease(self) -> None:
        self.lease.last_activity = 0
        self.assertEqual(self.app.get("/health").status_code, 200)
        self.assertEqual(self.app.get("/devices").status_code, 401)
        self.assertEqual(self.app.post("/jobs", json=payload()).status_code, 401)
        self.assertEqual(self.lease.last_activity, 0)
        with patch.object(server, "discover", return_value=[]):
            self.assertEqual(self.app.get("/devices", headers=self.headers).status_code, 200)
        self.assertGreater(self.lease.last_activity, 0)

    def test_idle_expiry_is_atomic_and_never_stops_a_writer(self) -> None:
        self.lease.last_activity = 0
        self.lease.active = True
        self.assertFalse(self.lease.should_stop())
        self.lease.stopping = True
        self.assertFalse(self.lease.should_stop())
        self.lease.active = False
        self.assertTrue(self.lease.should_stop())
        self.assertEqual(self.app.get("/devices", headers=self.headers).status_code, 503)

    def test_start_requires_identity_confirmation_and_bounded_input(self) -> None:
        for changes in (
            {"device_identity": ""}, {"confirm_device": "/dev/sda"}, {"image_sha256": ""},
            {"image_url": "file:///etc/passwd"}, {"command": "reboot"},
            {"workspace": "/etc"}, {"image_file": "/etc/passwd"}, {"hostname": "--help"},
            {"callback_url": ""}, {"cloud_init_options": []}, {"profile": "unknown"},
            {"ssh_public_key": 123}, {"device": None},
        ):
            with self.subTest(changes=changes):
                response = self.app.post("/jobs", headers=self.headers, json={**payload(), **changes})
                self.assertEqual(response.status_code, 400, response.get_json())
        self.thread.assert_not_called()

    def test_changed_device_rejected_before_start(self) -> None:
        self.verify.side_effect = ValueError("device changed")
        self.assertEqual(self.app.post("/jobs", headers=self.headers, json=payload()).status_code, 400)
        self.thread.assert_not_called()
        self.assertFalse(self.lease.active)

    def test_self_hosting_server_profile_is_accepted(self) -> None:
        response = self.app.post("/jobs", headers=self.headers,
                                 json={**payload(), "profile": "playbook-server"})
        self.assertEqual(response.status_code, 202, response.get_json())
        self.thread.assert_called_once()

    def test_single_writer_and_idempotent_submission(self) -> None:
        self.assertEqual(self.app.post("/jobs", headers=self.headers, json=payload()).status_code, 202)
        self.assertTrue(self.lease.active)
        self.assertEqual(self.app.post("/jobs", headers=self.headers, json=payload()).status_code, 200)
        self.assertEqual(self.app.post("/jobs", headers=self.headers, json={**payload(), "job_id": "e" * 32}).status_code, 409)
        self.thread.assert_called_once()
        self.assertEqual(self.app.get("/jobs/" + "c" * 32, headers=self.headers).get_json()["job"]["status"], "queued")
        self.assertEqual(self.app.get("/jobs/missing", headers=self.headers).status_code, 404)

    def test_worker_failure_is_explicit_and_releases_idle_lease(self) -> None:
        self.worker.side_effect = OSError("test failure")
        self.app.post("/jobs", headers=self.headers, json=payload())
        kwargs = self.thread.call_args.kwargs
        kwargs["target"](*kwargs["args"])
        self.assertEqual(self.jobs["c" * 32]["status"], "failed")
        self.assertFalse(self.lease.active)
        self.assertGreater(self.lease.last_activity, 0)

    def test_worker_start_failure_does_not_leave_an_active_lease(self) -> None:
        self.thread.return_value.start.side_effect = RuntimeError("no worker resources")
        response = self.app.post("/jobs", headers=self.headers, json=payload())
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.lease.active)
        self.assertEqual(self.jobs["c" * 32]["status"], "failed")

    def test_idle_deadline_and_completion_grace_are_exact(self) -> None:
        self.lease.last_activity = 100
        with patch.object(server.time, "monotonic", return_value=279.99):
            self.assertFalse(self.lease.should_stop())
        with patch.object(server.time, "monotonic", return_value=280):
            self.assertTrue(self.lease.should_stop())

    def test_idle_shutdown_cannot_race_a_slow_prepare_body(self) -> None:
        validate = server._validate
        def expire(payload):
            self.lease.stopping = True
            return validate(payload)
        with patch.object(server, "_validate", side_effect=expire):
            response = self.app.post("/jobs", headers=self.headers, json=payload())
        self.assertEqual(response.status_code, 503)
        self.thread.assert_not_called()
        self.assertFalse(self.lease.active)

    def test_discovery_errors_are_not_empty_successes(self) -> None:
        with patch.object(server, "discover", side_effect=OSError("cannot inspect disks")):
            response = self.app.get("/devices", headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])


class HelperDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.disk = {
            "path": "/dev/sdz", "type": "disk", "size": 16000000000, "ro": False,
            "rm": True, "tran": "usb", "maj:min": "8:240", "serial": "reader-1",
            "children": [{"path": "/dev/sdz1", "maj:min": "8:241", "mountpoints": []}],
        }
        self.mountinfo = "1 0 8:0 / / rw - ext4 /dev/sda rw\n"
        self.sequence = "42"
        self.swaps = "Filename Type Size Used Priority\n"
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"FLASH_ALLOWED_DEVICES": ALIAS, "FLASH_HOST_MOUNTINFO": "/proc/1/mountinfo"}).start()
        patch.object(Path, "exists", return_value=True).start()
        patch.object(Path, "resolve", return_value=Path("/dev/sdz")).start()
        self.lsblk = patch.object(devices.subprocess, "run").start()
        self.snapshot()

    def snapshot(self) -> None:
        self.lsblk.return_value = Mock(stdout=json.dumps({"blockdevices": [self.disk]}))
        def read(path, *args, **kwargs):
            return self.sequence if path.name == "diskseq" else self.swaps if str(path) == "/proc/swaps" else self.mountinfo
        patch.object(Path, "read_text", read).start()

    def test_identity_changes_on_replug_and_verification_is_fail_closed(self) -> None:
        identity = devices.discover()[0]["identity"]
        devices.verify(ALIAS, identity)
        self.sequence = "43"
        self.assertNotEqual(devices.discover()[0]["identity"], identity)
        with self.assertRaises(ValueError):
            devices.verify(ALIAS, identity)

    def test_protected_readonly_nonremovable_and_swap_disks_rejected(self) -> None:
        original = copy.deepcopy(self.disk)
        for change in (
            {"ro": True}, {"type": "part"}, {"rm": False, "tran": "sata"},
            {"children": [{"path": "/dev/dm-0", "maj:min": "8:0"}]},
            {"children": [{"path": "/dev/sdz1", "mountpoints": ["/home"]}]},
        ):
            with self.subTest(change=change):
                self.disk = {**original, **change}
                self.snapshot()
                self.assertEqual(devices.discover(), [])
        self.disk = original
        self.swaps += "/dev/sdz1 partition 123 0 -2\n"
        self.snapshot()
        self.assertEqual(devices.discover(), [])

    def test_container_refuses_host_mounted_target(self) -> None:
        self.mountinfo += "2 0 8:241 / /media/card rw - vfat /dev/sdz1 rw\n"
        with patch.dict(os.environ, {"FLASH_HOST_MOUNTINFO": "/host/mountinfo"}):
            with self.assertRaisesRegex(ValueError, "Unmount"):
                devices.discover()

    def test_native_helper_rejects_a_disk_with_an_active_swapfile(self) -> None:
        self.mountinfo += "2 0 8:241 / /media/card rw - ext4 /dev/sdz1 rw\n"
        self.swaps += "/media/card/swapfile file 123 0 -2\n"
        self.assertEqual(devices.discover(), [])

    def test_invalid_allowlist_unknown_kernel_identity_or_mounts_fail(self) -> None:
        for alias in ("", "/dev/sda", ALIAS + "-part1", "/dev/disk/by-id/../sda"):
            with self.subTest(alias=alias), patch.dict(os.environ, {"FLASH_ALLOWED_DEVICES": alias}):
                with self.assertRaises(ValueError):
                    devices.discover()
        self.sequence = ""
        with self.assertRaises(ValueError):
            devices.discover()
        self.sequence = "42"
        self.mountinfo = ""
        with self.assertRaises(ValueError):
            devices.discover()


class HelperPortalTests(BaseCallbackSiteTest):
    def test_image_selection_uses_pinned_checksums_and_never_skips_verification(self) -> None:
        url, checksum = client._image_source({"profile": "pi5"})
        self.assertIn("ubuntu-24.04", url)
        self.assertEqual(len(checksum), 64)
        self.assertEqual(client._image_source({"image_url": url})[1], checksum)
        with self.assertRaisesRegex(client.HelperError, "SHA-256"):
            client._image_source({"image_url": "https://example.invalid/custom.img"})

    def test_backend_and_helper_discovery_use_no_native_monitor(self) -> None:
        with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": "http://helper:8090"}), \
             patch.object(client, "devices", return_value=[{"path": ALIAS, "identity": IDENTITY}]), \
             patch("routes.sd._ensure_device_monitor_started") as monitor:
            self.assertEqual(self.client.get("/api/sd/backend").get_json()["discovery"], "poll")
            self.assertEqual(self.client.get("/api/sd/devices").get_json()["backend"], "helper")
            self.assertEqual(self.client.get("/api/sd/devices/webhook").status_code, 409)
            monitor.assert_not_called()
        with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": "http://helper:8090"}), \
             patch.object(client, "devices", side_effect=client.HelperError("starting")):
            self.assertEqual(self.client.get("/api/sd/devices").status_code, 503)

    def test_prepare_requires_confirmation_before_job_or_template_created(self) -> None:
        form = payload()
        form.pop("job_id")
        with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": "http://helper:8090"}), \
             patch.object(client, "call", return_value={"devices": []}), \
             patch("routes.sd.threading.Thread") as thread, \
             patch("routes.sd._auto_generate_sd_option_profile") as save:
            response = self.client.post("/api/sd/prepare", json=form)
        self.assertEqual(response.status_code, 409)
        thread.assert_not_called()
        save.assert_not_called()

    def test_real_http_helper_job_contract_and_log_proxy(self) -> None:
        lease = server.Lease(180)
        remote_jobs = {}
        def worker(job_id, form):
            remote_jobs[job_id].update(status="completed", stdout="verified fake disk", exit_code=0)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(server, "JOBS", remote_jobs), \
             patch.object(server, "verify"), patch.object(server, "_run_prepare_job", side_effect=worker), \
             patch("services.ssh_keys._archive_ssh_public_key"), patch("services.images._record_image_history"):
            token = Path(tmp) / "token"
            token.write_text(TOKEN)
            httpd = make_server("127.0.0.1", 0, server.create_app(TOKEN, lease), threaded=True)
            thread = threading.Thread(target=httpd.serve_forever)
            thread.start()
            try:
                with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": f"http://127.0.0.1:{httpd.server_port}",
                                            "CALLBACK_FLASH_HELPER_TOKEN_FILE": str(token)}):
                    form = payload()
                    job_id = form.pop("job_id")
                    sd_prepare.JOBS[job_id] = {"status": "queued"}
                    try:
                        sd_prepare._run_prepare_job(job_id, form)
                        self.assertEqual(sd_prepare.JOBS[job_id]["status"], "completed")
                        self.assertEqual(sd_prepare.JOBS[job_id]["stdout"], "verified fake disk")
                    finally:
                        sd_prepare.JOBS.pop(job_id, None)
            finally:
                httpd.shutdown()
                thread.join()
                httpd.server_close()


class HelperWakeTests(unittest.TestCase):
    def test_client_authorization_uses_loaded_token_without_disclosing_header(self) -> None:
        import secrets

        with tempfile.TemporaryDirectory() as tmp:
            token = secrets.token_hex(32)
            path = Path(tmp) / "token"
            path.write_text(token + "\n")
            with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": "http://helper:8090",
                                        "CALLBACK_FLASH_HELPER_TOKEN_FILE": str(path)}), \
                 patch.object(client, "build_opener") as opener:
                response = opener.return_value.open.return_value.__enter__.return_value
                response.read.return_value = b'{"ok":true,"devices":[]}'
                client.devices()
                request = opener.return_value.open.call_args.args[0]
                matches = request.get_header("Authorization") == "Bearer " + token
                self.assertTrue(matches, "Authorization must use the token loaded from its file.")

    def test_wake_creates_only_missing_or_finished_job(self) -> None:
        config = json.dumps({"data": {"job.json": json.dumps({"kind": "Job", "metadata": {"name": "rack-flasher"}})}})
        for existing, expected in (
            ("", ["get", "get", "create"]),
            ('{"status":{"active":1}}', ["get", "get"]),
            ('{"status":{"conditions":[{"type":"Complete","status":"True"}]}}', ["get", "get", "delete"]),
        ):
            replies = [config, existing] + [""] * (len(expected) - 2)
            with self.subTest(existing=existing), \
                 patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_TEMPLATE": "rack-flasher-template"}), \
                 patch.object(client, "_kubectl", side_effect=replies) as kubectl:
                client.wake()
                self.assertEqual([call.args[0] for call in kubectl.call_args_list], expected)

    def test_only_discovery_can_wake_and_never_retries_a_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text(TOKEN)
            with patch.dict(os.environ, {"CALLBACK_FLASH_HELPER_URL": "http://helper:8090",
                                        "CALLBACK_FLASH_HELPER_TOKEN_FILE": str(token)}), \
                 patch.object(client, "build_opener") as opener, patch.object(client, "wake") as wake:
                opener.return_value.open.side_effect = URLError("unavailable")
                with self.assertRaisesRegex(client.HelperError, "starting"):
                    client.devices()
                wake.assert_called_once()
                wake.reset_mock()
                with self.assertRaisesRegex(client.HelperError, "Do not retry"):
                    client.call("/jobs", payload())
                wake.assert_not_called()


class HelperScriptTests(unittest.TestCase):
    def test_write_descriptor_stays_bound_if_device_alias_is_reassigned(self) -> None:
        script = (ROOT / "scripts/lib/flash.sh").read_text()
        write = script[script.index('echo "Flashing image..."'):script.index('\nsync')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image, original, replacement, alias, snippet = (root / name for name in (
                "image", "original", "replacement", "alias", "write.sh",
            ))
            image.write_bytes(b"raw-image")
            original.write_bytes(b"oldtarget")
            replacement.write_bytes(b"newtarget")
            alias.symlink_to(original)
            snippet.write_text(write)
            result = subprocess.run([
                "bash", "-c",
                'set -euo pipefail; DEVICE="$1"; IMG_TO_WRITE="$2"; REPLACEMENT="$3"; '
                'SCRIPT_DIR=unused; FLASH_EXPECTED_IDENTITY=test; '
                'python3() { ln -sfn "$REPLACEMENT" "$DEVICE"; }; source "$4"',
                "test", str(alias), str(image), str(replacement), str(snippet),
            ], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(original.read_bytes(), image.read_bytes())
            self.assertEqual(replacement.read_bytes(), b"newtarget")

    def test_by_id_partition_names_resolve_for_sd_and_mmc_disks(self) -> None:
        script = (ROOT / "scripts/lib/flash.sh").read_text()
        partition = script[script.index("DEVICE_PATH="):script.index('if [[ ! -b "$BOOT_PART"')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, suffix in (("sdz", "1"), ("mmcblk0", "p1"), ("nvme0n1", "p1")):
                target = root / name
                target.touch()
                alias = root / ("usb-" + name)
                alias.symlink_to(target)
                result = subprocess.run(
                    ["bash", "-c", 'DEVICE="$1"; ' + partition + '\nprintf "%s" "$BOOT_PART"', "test", str(alias)],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, str(target) + suffix)


@unittest.skipUnless(shutil.which("helm"), "Helm is required for chart contract tests")
class HelperChartTests(unittest.TestCase):
    def test_disabled_chart_has_no_resources_and_enabled_chart_only_has_a_job_template(self) -> None:
        import yaml

        base = ["helm", "template", "rack", str(ROOT / "charts/flash-helper")]
        disabled = subprocess.run(base, capture_output=True, text=True, check=True, timeout=15)
        self.assertEqual(disabled.stdout.strip(), "")
        enabled = subprocess.run(base + [
            "--set", "enabled=true", "--set", "nodeName=linux-reader",
            "--set", "existingSecret=flash-token", "--set", "portal.serviceAccount=portal",
            "--set", f"allowedDevices[0]={ALIAS}",
        ], capture_output=True, text=True, check=True, timeout=15)
        resources = list(yaml.safe_load_all(enabled.stdout))
        self.assertNotIn("Job", [resource["kind"] for resource in resources])
        config = next(resource for resource in resources if resource["kind"] == "ConfigMap")
        job = json.loads(config["data"]["job.json"])
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeName"], "linux-reader")
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertFalse(pod["automountServiceAccountToken"])
        invalid = subprocess.run(base + ["--set", "enabled=true"], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("nodeName", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
