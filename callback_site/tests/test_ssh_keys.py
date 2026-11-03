"""SSH key generation and listing endpoints."""

from __future__ import annotations

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

import services.storage


class SshKeyTests(BaseCallbackSiteTest):
    def test_ssh_key_generate_and_kload_modes(self) -> None:
        key_name = "test-mode-key"
        private_key_path = services.storage.KEYS_DIR / key_name
        public_key_path = services.storage.KEYS_DIR / f"{key_name}.pub"

        generated = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "generate"},
        )
        self.assertEqual(generated.status_code, 200)
        generated_payload = generated.get_json()
        self.assertTrue(generated_payload["ok"])
        self.assertTrue(generated_payload["created"])
        self.assertEqual(generated_payload["mode"], "generate")
        self.assertEqual(oct(private_key_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(public_key_path.stat().st_mode & 0o777), "0o644")

        generate_again = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "generate"},
        )
        self.assertEqual(generate_again.status_code, 409)
        self.assertFalse(generate_again.get_json()["ok"])

        loaded = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "kload"},
        )
        self.assertEqual(loaded.status_code, 200)
        loaded_payload = loaded.get_json()
        self.assertTrue(loaded_payload["ok"])
        self.assertFalse(loaded_payload["created"])
        self.assertEqual(loaded_payload["mode"], "kload")

    def test_ssh_key_list_endpoint(self) -> None:
        key_name = "test-list-key"
        created = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "generate"},
        )
        self.assertEqual(created.status_code, 200)

        response = self.client.get("/api/ssh/keys")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        names = [item.get("key_name") for item in payload.get("keys", [])]
        self.assertIn(key_name, names)

    def test_ssh_key_mode_validation(self) -> None:
        response = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "bad-mode-key", "mode": "load"},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
