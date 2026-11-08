"""SSH key generation and listing endpoints."""

from __future__ import annotations
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.storage


class SshKeyTests(BaseCallbackSiteTest):
    def test_public_endpoint_serves_only_public_key_as_uncached_text(self) -> None:
        self.client.post("/api/ssh/generate", json={"key_name": "endpoint-key", "mode": "generate"})
        response = self.client.get("/api/ssh/public/endpoint-key")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("attachment", response.headers.get("Content-Disposition", ""))
        self.assertEqual(response.get_data(as_text=True).strip(),
                         (services.storage.KEYS_DIR / "endpoint-key.pub").read_text().strip())
        self.assertNotIn("PRIVATE KEY", response.get_data(as_text=True))
        response.close()
        self.assertEqual(self.client.get("/api/ssh/public/missing-key").status_code, 404)
        self.assertEqual(self.client.get("/api/ssh/public/bad$key").status_code, 400)

    def test_prepare_derives_fetch_url_and_checks_selected_key(self) -> None:
        created = self.client.post("/api/ssh/generate", json={"key_name": "boot-key", "mode": "generate"}).get_json()
        payload = {
            "profile": "pi3bplus", "hostname": "boot-test", "device": "/dev/fake",
            "callback_url": "https://callback.test:8088/api/cloud-init/callback",
            "ssh_key_name": "boot-key", "ssh_public_key": created["public_key"],
            "ssh_public_key_url": "https://untrusted.test/key",
        }
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as thread:
            response = self.client.post("/api/sd/prepare", json=payload)
            self.assertEqual(response.status_code, 200)
            form = thread.call_args.kwargs["args"][1]
            self.assertEqual(form["ssh_public_key_url"], "https://callback.test:8088/api/ssh/public/boot-key")
            for extra in (
                {"ssh_public_key": "ssh-ed25519 different"},
                {"ssh_key_name": "missing"},
                {"ssh_key_name": "../private"},
                {"callback_url": "http://localhost:8088/callback"},
                {"callback_url": "http://127.0.0.1/callback"},
                {"callback_url": "http://[::1]/callback"},
                {"callback_url": "http://callback.test:bad/callback"},
            ):
                with self.subTest(extra=extra):
                    thread.reset_mock()
                    self.assertEqual(self.client.post("/api/sd/prepare", json={**payload, **extra}).status_code, 400)
                    thread.assert_not_called()
            response = self.client.post("/api/sd/prepare", json={**payload, "ssh_key_name": ""})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("ssh_public_key_url", thread.call_args.kwargs["args"][1])

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
