"""Host SD flasher agent and the dashboard's delegation to it."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import sd_flasher
import services.sd_devices
import services.sd_prepare

TOKEN = "t" * 40
DEVICE = {"name": "sdz", "path": "/dev/sdz", "size": "58G", "model": "USB", "transport": "usb", "mountpoints": []}
FAKE_SCRIPT = """#!/bin/sh
echo "ARGS $*"
echo "PORT=${CLOUD_INIT_SSH_PORT:-}"
echo "LEAK=${SD_FLASHER_TEST_SECRET:-none}"
echo "CACHE=${IMAGE_CACHE_PATH:-}"
exit "${FAKE_EXIT:-0}"
"""


class SdFlasherTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="sdflasher-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        script = self.tmp / "prepare.sh"
        script.write_text(FAKE_SCRIPT, encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        self.config = sd_flasher.Config(TOKEN, script, self.cache, self.tmp / "work")
        for target in (
            patch.object(sd_flasher, "_local_sd_devices", return_value=[DEVICE]),
            patch.object(sd_flasher, "health_errors", return_value=[]),
        ):
            target.start()
            self.addCleanup(target.stop)

    def start_agent(self) -> str:
        server = sd_flasher.ThreadingHTTPServer(("127.0.0.1", 0), sd_flasher.make_handler(self.config))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, url: str, method: str = "GET", body: object = None, token: str = TOKEN) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def job_spec(self, **overrides: object) -> dict:
        spec = {
            "profile": "pi5", "hostname": "rack-01", "device": "/dev/sdz",
            "callback_url": "http://192.168.1.5:8088", "ssh_public_key": "ssh-ed25519 AAAA test",
            "image_url": "", "image_sha256": "", "image_name": "", "env": {"CLOUD_INIT_SSH_PORT": "2222"},
        }
        spec.update(overrides)
        return spec

    def test_validation_only_accepts_listed_devices_and_safe_fields(self) -> None:
        job, errors = sd_flasher.validate_job(self.job_spec(), [DEVICE], self.config)
        self.assertEqual(errors, [])
        self.assertEqual(job["device"], "/dev/sdz")
        bad = {
            "device": "/dev/sda",
            "env": {"LD_PRELOAD": "/tmp/x.so"},
            "image_name": "../../etc/shadow",
            "hostname": "-bad-",
            "callback_url": "file:///etc/passwd",
            "profile": "x",
        }
        for key, value in bad.items():
            _job, errors = sd_flasher.validate_job(self.job_spec(**{key: value}), [DEVICE], self.config)
            self.assertTrue(errors, key)
        _job, errors = sd_flasher.validate_job(self.job_spec(image_name="missing.img.xz"), [DEVICE], self.config)
        self.assertIn("not in the flasher image cache", errors[0])

    def test_command_uses_flasher_cache_and_does_not_leak_host_environment(self) -> None:
        (self.cache / "cached.img.xz").write_bytes(b"x")
        with patch.dict(os.environ, {"SD_FLASHER_TEST_SECRET": "leak"}):
            job, _ = sd_flasher.validate_job(self.job_spec(image_name="cached.img.xz"), [DEVICE], self.config)
            cmd, env = sd_flasher.build_command(job, Path("/k.pub"), self.config)
            self.assertIn("--image-file", cmd)
            self.assertEqual(cmd[cmd.index("--image-file") + 1], str(self.cache / "cached.img.xz"))
            self.assertNotIn("SD_FLASHER_TEST_SECRET", env)
            job, _ = sd_flasher.validate_job(
                self.job_spec(image_url="https://example.com/a.img.xz", image_name="new.img.xz"), [DEVICE], self.config)
            cmd, env = sd_flasher.build_command(job, Path("/k.pub"), self.config)
        self.assertIn("--image-url", cmd)
        self.assertNotIn("--image-file", cmd)
        self.assertEqual(env["IMAGE_CACHE_PATH"], str(self.cache / "new.img.xz"))
        self.assertEqual(env["IMAGE_CACHE_DIR"], str(self.cache))

    def test_api_requires_token_and_runs_a_job(self) -> None:
        url = self.start_agent()
        self.assertEqual(self.call(f"{url}/devices", token="wrong")[0], 401)
        self.assertEqual(self.call(f"{url}/devices")[1]["devices"], [DEVICE])
        status, body = self.call(f"{url}/jobs", "POST", self.job_spec(device="/dev/sda"))
        self.assertEqual(status, 400)
        with patch.dict(os.environ, {"SD_FLASHER_TEST_SECRET": "leak"}):
            status, body = self.call(f"{url}/jobs", "POST", self.job_spec())
            self.assertEqual(status, 202)
            job = {}
            for _ in range(100):
                job = self.call(f"{url}/jobs/{body['job_id']}")[1]["job"]
                if job["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(job["status"], "completed")
        self.assertIn("--device /dev/sdz", job["output"])
        self.assertIn("PORT=2222", job["output"])
        self.assertIn("LEAK=none", job["output"])

    def test_dashboard_delegates_prepare_job_to_flasher(self) -> None:
        url = self.start_agent()
        job_id = "job-remote"
        form = self.job_spec(distribution="standard", cloud_init_options={"ssh_port": "2222"})
        with services.sd_prepare.JOBS_LOCK:
            services.sd_prepare.JOBS[job_id] = {"status": "queued", "stdout": "", "stderr": "", "exit_code": None}
        self.addCleanup(lambda: services.sd_prepare.JOBS.pop(job_id, None))
        with patch.dict(os.environ, {"SD_FLASHER_URL": url, "SD_FLASHER_TOKEN": TOKEN}), \
                patch("os.geteuid", return_value=1000), \
                patch.object(services.sd_prepare, "REMOTE_POLL_SECONDS", 0.05), \
                patch_in("_archive_ssh_public_key", return_value={"archive_path": "test-archive"}), \
                patch_in("_record_image_history") as history:
            self.assertEqual(services.sd_devices._list_sd_devices(), [DEVICE])
            services.sd_prepare._run_prepare_job(job_id, form)
        # Running as non-root, a local run would fail; this output comes from the agent's script.
        job = services.sd_prepare.JOBS[job_id]
        self.assertEqual(job["status"], "completed", job)
        self.assertIn("Delegated to the host SD flasher", job["stdout"])
        self.assertIn("PORT=2222", job["stdout"])
        history.assert_called_once()

    def test_device_endpoint_explains_missing_flasher(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        with patch.dict(os.environ, {"SD_FLASHER_URL": f"http://127.0.0.1:{closed_port}", "SD_FLASHER_TOKEN": TOKEN}):
            payload = self.client.get("/api/sd/devices").get_json()
        self.assertEqual(payload["devices"], [])
        self.assertIn("SD flasher unreachable", payload["errors"][0])
        self.assertIn("scripts/run_sd_flasher.sh", payload["errors"][0])
        with patch.dict(os.environ, {"SD_FLASHER_URL": "", "CALLBACK_RUNTIME": "container"}):
            errors = services.sd_prepare._ensure_prereqs()
        self.assertTrue(any("runs in a container" in error for error in errors))

    def test_device_events_use_real_sse_line_breaks(self) -> None:
        message = services.sd_devices._device_event_message([DEVICE])
        self.assertTrue(message.startswith("event: devices\ndata: {"))
        self.assertTrue(message.endswith("\n\n"))

    def test_token_file_accepts_compose_env_format(self) -> None:
        token_file = self.tmp / "sd-flasher.env"
        token_file.write_text(f"# shared\nSD_FLASHER_TOKEN={TOKEN}\n", encoding="utf-8")
        with patch.dict(os.environ, {"SD_FLASHER_TOKEN": ""}):
            self.assertEqual(sd_flasher.read_token(type("A", (), {"token_file": str(token_file)})()), TOKEN)
