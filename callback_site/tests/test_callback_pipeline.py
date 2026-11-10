"""Generated Pi callback clients through HTTP intake and deployment guards."""

import os
from pathlib import Path
import subprocess
import tempfile
import threading
from unittest.mock import patch

from werkzeug.serving import make_server

try:
    from .test_support import BaseCallbackSiteTest, app, patch_in
    from . import test_sd_cloud_init_render as render_tests
except ImportError:
    from test_support import BaseCallbackSiteTest, app, patch_in
    import test_sd_cloud_init_render as render_tests

from package_options import package_names
from services import network, node_archive, sd_prepare


class CallbackPipelineTests(BaseCallbackSiteTest):
    def test_container_requires_an_explicit_public_callback_address(self):
        with patch.dict(os.environ, {
            "CALLBACK_RUNTIME": "container", "CALLBACK_PUBLIC_URL": "",
            "CALLBACK_PUBLIC_HOST": "", "CALLBACK_PUBLIC_PORT": "9821",
        }):
            with self.assertLogs("services.network", level="WARNING"):
                self.assertEqual(network._default_callback_url(), "")
            with patch.dict(os.environ, {"CALLBACK_PUBLIC_HOST": "192.0.2.10"}):
                self.assertEqual(network._default_callback_url(),
                                 "http://192.0.2.10:9821/api/cloud-init/callback")

    def test_invalid_callback_urls_cannot_start_a_flash_even_without_a_saved_key(self):
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as thread:
            with patch.dict(os.environ, {"CALLBACK_RUNTIME": "container"}), patch.object(
                network, "_detect_local_ip", return_value="172.18.0.2",
            ):
                for url in (
                    "http://localhost:8088/api/cloud-init/callback",
                    "http://127.0.0.1:8088/api/cloud-init/callback",
                    "http://[::1]:8088/api/cloud-init/callback",
                    "http://0.0.0.0:8088/api/cloud-init/callback",
                    "http://172.18.0.2:8088/api/cloud-init/callback",
                    "http://example.test:99999/callback",
                    "http://operator@example.test/callback",
                    "http://example.test/'bad",
                ):
                    with self.subTest(url=url):
                        response = self.client.post("/api/sd/prepare", json={
                            "profile": "pi5", "hostname": "callback-guard", "device": "/dev/fake",
                            "ssh_public_key": "ssh-ed25519 test", "callback_url": url,
                        })
                        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
                        self.assertTrue(response.get_json()["errors"])
            thread.assert_not_called()

    def test_nvim_package_is_rejected_with_the_correct_package_name(self):
        self.assertEqual(package_names("neovim jq neovim"), ["neovim", "jq"])
        with self.assertRaisesRegex(ValueError, "use 'neovim'"):
            package_names(["nvim"])
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as thread:
            response = self.client.post("/api/sd/prepare", json={
                "profile": "pi5", "hostname": "package-guard", "device": "/dev/fake",
                "ssh_public_key": "ssh-ed25519 test",
                "callback_url": "http://192.0.2.10:8088/api/cloud-init/callback",
                "cloud_init_options": {"packages": ["nvim"]},
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("use 'neovim'", " ".join(response.get_json()["errors"]))
        thread.assert_not_called()

    def test_successful_redeployment_restores_archived_hostname_but_failed_flash_does_not(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            node_archive, "ARCHIVE_FILE", Path(tmp) / "archive.json",
        ):
            name = "pi5-redeploy-proof"
            self.client.post("/api/cloud-init/callback", json={"hostname": name, "profile": "pi5"})
            node_archive.set_archived(name, True)
            job_id = "callback-redeploy-proof"
            sd_prepare.JOBS[job_id] = {}
            self.addCleanup(sd_prepare.JOBS.pop, job_id, None)
            sd_prepare._finish_job(job_id, "", "flash failed", 1, hostname=name)
            self.assertTrue(node_archive.archived_nodes())
            sd_prepare._finish_job(job_id, "card verified", "", 0, hostname=name)
            self.assertEqual(node_archive.archived_nodes(), [])
            self.assertEqual(sd_prepare.JOBS[job_id]["status"], "completed")
            self.assertIn(name, {
                event["hostname"] for event in self.client.get("/api/cloud-init/events").get_json()["events"]
            })
            node_archive.set_archived(name, True)
            with patch("services.sd_prepare.set_archived", side_effect=OSError("disk full")):
                sd_prepare._finish_job(job_id, "card verified", "", 0, hostname=name)
            self.assertEqual(sd_prepare.JOBS[job_id]["status"], "failed")
            self.assertIn("Card written", sd_prepare.JOBS[job_id]["stderr"])
            self.assertTrue(node_archive.archived_nodes())

    def test_pi5_default_image_and_callback_are_forwarded_to_the_host_flasher(self):
        form = {
            "profile": "pi5", "hostname": "pi5-remote-pipeline-proof",
            "device": "/dev/fake", "ssh_public_key": "ssh-ed25519 test",
            "callback_url": "http://192.0.2.10:8088/api/cloud-init/callback",
            "ssh_public_key_url": "http://192.0.2.10:8088/api/ssh/public/admin",
            "cloud_init_options": {},
        }
        job_id = "pi5-remote-pipeline-proof"
        sd_prepare.JOBS[job_id] = {}
        self.addCleanup(sd_prepare.JOBS.pop, job_id, None)
        with patch.object(sd_prepare.sd_flasher_client, "enabled", return_value=True), \
             patch.object(sd_prepare.sd_flasher_client, "submit_job", return_value="remote-proof") as submit, \
             patch.object(sd_prepare.sd_flasher_client, "get_job", return_value={
                 "status": "completed", "exit_code": 0, "output": "card and seed readback verified",
             }), patch.object(sd_prepare.time, "sleep"), \
             patch.object(sd_prepare, "_archive_ssh_public_key", return_value={}), \
             patch.object(sd_prepare, "_record_image_history"), \
             patch.object(sd_prepare, "set_archived") as restore:
            sd_prepare._run_prepare_job(job_id, form)
        spec = submit.call_args.args[0]
        self.assertEqual(spec["callback_url"], form["callback_url"])
        self.assertEqual(spec["env"]["CLOUD_INIT_SSH_PUBLIC_KEY_URL"], form["ssh_public_key_url"])
        self.assertEqual(len(spec["image_sha256"]), 64)
        self.assertIn("arm64", spec["image_url"])
        self.assertTrue(spec["image_name"])
        self.assertEqual(sd_prepare.JOBS[job_id]["status"], "completed")
        restore.assert_called_once_with(form["hostname"], False)

    def test_generated_pi5_clients_deliver_real_http_callbacks_and_report_failures(self):
        renderer = render_tests.SdCloudInitRenderTests()
        code, data, error = renderer.render(R_PROFILE="pi5", R_DIST="standard")
        self.assertEqual(code, 0, error)
        files = renderer.files(data)
        unit = files["/etc/systemd/system/rack-power-restored-callback.service"]["content"]
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=30", unit)
        self.assertIn("StartLimitIntervalSec=0", unit)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                name = "pi5-http-pipeline-proof"
                url = f"http://127.0.0.1:{server.server_port}/api/cloud-init/callback"
                env_file = root / "node.env"
                env_file.write_text(
                    f"CALLBACK_URL={url}\nHOSTNAME={name}\nINSTANCE_ID={name}-first\nPROFILE=pi5\n"
                )
                scripts = {
                    "rack-local-ip": "#!/bin/sh\necho 192.0.2.55\n",
                    "rack-node-report": "#!/bin/sh\necho '{}'\n",
                    "rack-boot-log": f"#!/bin/sh\ntouch {root / 'boot-log-ran'}\n",
                    "rack-callback-payload": files["/usr/local/bin/rack-callback-payload"]["content"],
                    "rack-power-restored-callback": files["/usr/local/bin/rack-power-restored-callback"]["content"],
                    "rack-hardware-check": files["/usr/local/bin/rack-hardware-check"]["content"],
                }
                for filename, content in scripts.items():
                    target = root / filename
                    target.write_text(content.replace("/etc/rack-node.env", str(env_file)).replace(
                        "/usr/local/bin/", f"{root}/",
                    ).replace("http://192.168.174.1:8088/api/cloud-init/callback", url))
                    target.chmod(0o755)
                    result = subprocess.run(["bash", "-n", str(target)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                for filename in ("rack-power-restored-callback", "rack-hardware-check"):
                    result = subprocess.run([str(root / filename)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                events = self.client.get("/api/cloud-init/events").get_json()["events"]
                restored = next(event for event in events if event["hostname"] == name)
                self.assertEqual(restored["profile"], "pi5")
                self.assertEqual(restored["stage"], "power_restore")
                self.assertEqual(restored["instance_id"], f"{name}-first")
                self.assertEqual(restored["source_ip"], "192.0.2.55")
                self.assertTrue(any(event["stage"] == "hardware-check" for event in events))
                summary = self.client.get("/api/monitor/summary").get_json()["nodes"]
                self.assertTrue(next(node for node in summary if node["name"] == name)["last_callback"])
                (root / "boot-log-ran").unlink()
                curl = root / "curl"
                curl.write_text("#!/bin/sh\necho callback-unreachable >&2\nexit 22\n")
                curl.chmod(0o755)
                result = subprocess.run([str(root / "rack-power-restored-callback")], env={
                    **os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                }, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 22)
                self.assertIn("callback-unreachable", result.stderr)
                self.assertFalse((root / "boot-log-ran").exists())
        finally:
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()
