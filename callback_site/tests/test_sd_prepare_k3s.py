from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, callback_app
except ImportError:
    from test_support import BaseCallbackSiteTest, callback_app


class SdPrepareK3sTests(BaseCallbackSiteTest):
    def test_sd_prepare_endpoint_accepts_pi3bplus_profile(self) -> None:
        with patch.object(callback_app, "_ensure_prereqs", return_value=[]), patch.object(
            callback_app.threading, "Thread"
        ) as thread_cls:
            response = self.client.post(
                "/api/sd/prepare",
                json={
                    "profile": "pi3bplus",
                    "hostname": "rack-node-3b-01",
                    "device": "/dev/sdb",
                    "ssh_public_key": "ssh-ed25519 AAAA",
                    "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                    "distribution": "k3s",
                    "cloud_init_options": {},
                },
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)
        with callback_app.JOBS_LOCK:
            job = callback_app.JOBS.get(job_id)
            self.assertIsNotNone(job)
            request_payload = dict(job.get("request", {})) if job else {}
            callback_app.JOBS.pop(job_id, None)
        self.assertEqual(request_payload.get("profile"), "pi3bplus")
        thread_cls.assert_called_once()

    def test_sanitize_sd_prepare_payload_supports_k3s_distribution(self) -> None:
        cleaned = callback_app._sanitize_sd_prepare_payload(
            {
                "profile": "pi5",
                "hostname": "rack-node-k3s",
                "device": "/dev/sdb",
                "ssh_public_key": "ssh-ed25519 AAAA",
                "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                "distribution": "k3s",
                "cloud_init_options": {
                    "k3s_config": "# pragma: k3s-config-v1\nserver: https://control-plane.example:6443\n",
                },
                "ignored": "value",
            }
        )
        self.assertNotIn("ignored", cleaned)
        self.assertEqual(cleaned["distribution"], "k3s")
        self.assertIn("pragma: k3s-config-v1", cleaned["cloud_init_options"]["k3s_config"])

    def test_sanitize_sd_prepare_payload_autofills_k3s_config_when_missing(self) -> None:
        with patch.dict("os.environ", {"K3S_SERVER_HOST": ""}, clear=False), patch.object(callback_app, "_detect_local_ip", return_value="10.255.45.164"):
            cleaned = callback_app._sanitize_sd_prepare_payload(
                {
                    "profile": "pi5",
                    "hostname": "rack-node-k3s",
                    "device": "/dev/sdb",
                    "ssh_public_key": "ssh-ed25519 AAAA",
                    "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                    "distribution": "k3s",
                    "cloud_init_options": {},
                }
            )
        self.assertEqual(cleaned["distribution"], "k3s")
        self.assertIn("k3s_config", cleaned["cloud_init_options"])
        self.assertIn("pragma: k3s-config-v1", cleaned["cloud_init_options"]["k3s_config"])
        # The default joins over Tailscale, never the detected LAN address.
        self.assertIn("server: https://100.65.24.76:6443", cleaned["cloud_init_options"]["k3s_config"])
        self.assertNotIn("10.255.45.164", cleaned["cloud_init_options"]["k3s_config"])

    def test_default_k3s_config_server_host_env_override(self) -> None:
        with patch.dict("os.environ", {"K3S_SERVER_HOST": "control-plane.example"}, clear=False):
            self.assertIn("server: https://control-plane.example:6443", callback_app._default_k3s_config("rack-node"))

    def test_sanitize_sd_prepare_payload_infers_k3s_from_config_block(self) -> None:
        cleaned = callback_app._sanitize_sd_prepare_payload(
            {
                "profile": "pi5",
                "hostname": "rack-node-k3s",
                "device": "/dev/sdb",
                "ssh_public_key": "ssh-ed25519 AAAA",
                "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                "distribution": "standard",
                "cloud_init_options": {
                    "k3s_config": "# pragma: k3s-config-v1\nserver: https://10.255.45.164:6443",
                },
            }
        )
        self.assertEqual(cleaned["distribution"], "k3s")

    def test_sanitize_sd_prepare_payload_uses_user_hostname_for_default_k3s_node_name(self) -> None:
        cleaned = callback_app._sanitize_sd_prepare_payload(
            {
                "profile": "pi5",
                "hostname": "rack-node-k3s",
                "device": "/dev/sdb",
                "ssh_public_key": "ssh-ed25519 AAAA",
                "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                "distribution": "k3s",
                "cloud_init_options": {},
            }
        )
        config = cleaned["cloud_init_options"]["k3s_config"]
        self.assertIn("node-name: rack-node-k3s", config)
        self.assertEqual(1, sum(1 for line in config.splitlines() if line.strip().lower().startswith("node-name:")))

    def test_default_callback_url_respects_explicit_port_env(self) -> None:
        with patch.dict("os.environ", {"CALLBACK_PUBLIC_HOST": "callback.example", "CALLBACK_PUBLIC_PORT": "9821"}, clear=False):
            self.assertEqual(callback_app._default_callback_url(), "http://callback.example:9821/api/cloud-init/callback")

        with patch.dict("os.environ", {"CALLBACK_PUBLIC_URL": "http://callback.example:9821/api/cloud-init/callback"}, clear=False):
            self.assertEqual(callback_app._default_callback_url(), "http://callback.example:9821/api/cloud-init/callback")

        with patch.dict("os.environ", {"PORT": "9821"}, clear=False):
            with patch.object(callback_app, "_detect_local_ip", return_value="10.255.45.164"):
                self.assertEqual(callback_app._default_callback_url(), "http://10.255.45.164:9821/api/cloud-init/callback")

    def test_default_callback_url_preserves_scheme_from_public_host_url(self) -> None:
        with patch.dict("os.environ", {"CALLBACK_PUBLIC_HOST": "https://callback.example", "CALLBACK_PUBLIC_PORT": "9821"}, clear=False):
            self.assertEqual(callback_app._default_callback_url(), "https://callback.example:9821/api/cloud-init/callback")

        with patch.dict("os.environ", {"CALLBACK_PUBLIC_HOST": "https://callback.example:9443"}, clear=False):
            self.assertEqual(callback_app._default_callback_url(), "https://callback.example:9443/api/cloud-init/callback")

    def test_prepare_sd_script_uses_unique_k3s_node_ids(self) -> None:
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_sd_cloud_init.sh"
        script_text = script_path.read_text(encoding="utf-8")
        self.assertIn('--with-node-id', script_text)
        self.assertIn('node-name: ${HOSTNAME}', script_text)

    def test_sanitize_sd_prepare_payload_removes_stale_node_identity(self) -> None:
        cleaned = callback_app._sanitize_sd_prepare_payload(
            {
                "profile": "pi5",
                "hostname": "rack-node-01",
                "device": "/dev/sdb",
                "ssh_public_key": "ssh-ed25519 AAAA",
                "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                "distribution": "k3s",
                "cloud_init_options": {
                    "k3s_config": "# pragma: k3s-config-v1\nserver: https://10.255.45.164:6443\ntoken: test\nnode-name: jd-shorty-rack-pi5-4gb-k3s-1\nnode-ip: 10.255.45.94\n",
                },
            }
        )
        config = cleaned["cloud_init_options"]["k3s_config"]
        self.assertIn("server: https://", config)
        self.assertIn("node-name: rack-node-01", config)
        self.assertNotIn("node-ip:", config)

    def test_prepare_sd_script_escapes_runtime_callback_payload_vars(self) -> None:
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_sd_cloud_init.sh"
        script_text = script_path.read_text(encoding="utf-8")
        self.assertIn('/usr/local/bin/rack-callback-payload', script_text)
        self.assertIn('-d "\\${PAYLOAD_JSON}"', script_text)

    def test_prepare_sd_script_escapes_runtime_callback_assignments(self) -> None:
        # The final callback runs on the node via bash, sourcing the baked /etc/rack-node.env.
        # Rendered behavior is covered in test_sd_cloud_init_render; this pins the shape.
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_sd_cloud_init.sh"
        script_text = script_path.read_text(encoding="utf-8")
        self.assertIn('. /etc/rack-node.env; SOURCE_IP="$(/usr/local/bin/rack-local-ip', script_text)
        self.assertIn('NODE_REPORT_B64="$(/usr/local/bin/rack-node-report', script_text)
        self.assertIn('/usr/local/bin/rack-callback-payload', script_text)
        self.assertIn('INSTANCE_ID="$INSTANCE_ID"', script_text)
        self.assertIn('HOSTNAME="$HOSTNAME"', script_text)
        self.assertIn('PROFILE="$PROFILE"', script_text)
        self.assertIn('SOURCE_IP="$SOURCE_IP"', script_text)
        self.assertIn('NODE_REPORT_B64="$NODE_REPORT_B64"', script_text)

    def test_prepare_sd_script_supports_pcie_boot_toggles(self) -> None:
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_sd_cloud_init.sh"
        script_text = script_path.read_text(encoding="utf-8")
        self.assertIn('CLOUD_INIT_PCIEX1_ENABLE', script_text)
        self.assertIn('CLOUD_INIT_PCIEX1_GEN3', script_text)
        self.assertIn('dtparam=pciex1', script_text)
        self.assertIn('dtparam=pciex1_gen=3', script_text)
        self.assertIn('[[ "$PROFILE" == "pi5" && ( "${CLOUD_INIT_PCIEX1_ENABLE:-false}" == "true"', script_text)
        self.assertIn('[[ "$PROFILE" == "pi5" && "${CLOUD_INIT_USB_MAX_CURRENT_ENABLE:-false}" == "true" ]]', script_text)

    def test_prepare_sd_script_accepts_pi3bplus_profile(self) -> None:
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_sd_cloud_init.sh"
        script_text = script_path.read_text(encoding="utf-8")
        self.assertIn('"$PROFILE" != "pi3bplus"', script_text)
        self.assertIn('PROFILE_ROLE="pi-3bplus"', script_text)
        self.assertIn('--profile pi5|pi3bplus', script_text)

    def test_prepare_sd_script_installs_fancy_motd_packages(self) -> None:
        script_path = Path(__file__).resolve().parents[2] / "scripts" / "prepare_sd_cloud_init.sh"
        script_text = script_path.read_text(encoding="utf-8")
        self.assertIn('CLOUD_INIT_FANCY_MOTD="${CLOUD_INIT_FANCY_MOTD:-false}"', script_text)
        self.assertIn('- fortune-mod', script_text)
        self.assertIn('- cowsay', script_text)
        self.assertIn('- lolcat', script_text)

    def test_sd_options_profile_save_persists_distribution(self) -> None:
        save = self.client.post(
            "/api/sd/options-profiles",
            json={
                "name": "k3s-template-test",
                "description": "k3s profile",
                "distribution": "k3s",
                "pi_only": True,
                "options": {
                    "k3s_config": "# pragma: k3s-config-v1\nserver: https://10.255.45.164:6443",
                    "usb_max_current_enable": True,
                },
            },
        )
        self.assertEqual(save.status_code, 200)
        save_payload = save.get_json()
        self.assertTrue(save_payload["ok"])
        self.assertEqual(save_payload["profile"].get("distribution"), "k3s")
        self.assertTrue(save_payload["profile"].get("pi_only"))

        listing = self.client.get("/api/sd/options-profiles")
        self.assertEqual(listing.status_code, 200)
        profiles = listing.get_json().get("profiles", [])
        match = [p for p in profiles if p.get("name") == "k3s-template-test"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("distribution"), "k3s")
        self.assertTrue(match[0].get("pi_only"))
