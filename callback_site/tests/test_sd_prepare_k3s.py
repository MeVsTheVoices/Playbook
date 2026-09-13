from __future__ import annotations

from test_support import BaseCallbackSiteTest, callback_app


class SdPrepareK3sTests(BaseCallbackSiteTest):
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

    def test_sd_options_profile_save_persists_distribution(self) -> None:
        save = self.client.post(
            "/api/sd/options-profiles",
            json={
                "name": "k3s-template-test",
                "description": "k3s profile",
                "distribution": "k3s",
                "options": {"k3s_config": "# pragma: k3s-config-v1\nserver: https://10.255.45.164:6443"},
            },
        )
        self.assertEqual(save.status_code, 200)
        save_payload = save.get_json()
        self.assertTrue(save_payload["ok"])
        self.assertEqual(save_payload["profile"].get("distribution"), "k3s")

        listing = self.client.get("/api/sd/options-profiles")
        self.assertEqual(listing.status_code, 200)
        profiles = listing.get_json().get("profiles", [])
        match = [p for p in profiles if p.get("name") == "k3s-template-test"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("distribution"), "k3s")
