"""SSH key generation and listing endpoints."""

from __future__ import annotations
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.storage


class SshKeyTests(BaseCallbackSiteTest):
    def test_new_named_pair_preserves_original_and_rejects_reuse(self) -> None:
        original = self.client.post("/api/ssh/generate", json={
            "key_name": "original-key", "mode": "generate",
        }).get_json()
        private = (services.storage.KEYS_DIR / "original-key").read_bytes()
        fresh = self.client.post("/api/ssh/generate", json={
            "key_name": "original-key-2", "mode": "generate",
        })
        self.assertEqual(fresh.status_code, 200)
        self.assertNotEqual(original["public_key"], fresh.get_json()["public_key"])
        self.assertEqual((services.storage.KEYS_DIR / "original-key").read_bytes(), private)
        self.assertEqual((services.storage.KEYS_DIR / "original-key.pub").read_text().strip(),
                         original["public_key"].strip())
        self.assertEqual(self.client.post("/api/ssh/generate", json={
            "key_name": "original-key", "mode": "generate",
        }).status_code, 409)
        self.assertEqual((services.storage.KEYS_DIR / "original-key").read_bytes(), private)

    def test_regeneration_ui_creates_new_name_and_preserves_selection_on_failure(self) -> None:
        self.browser_probe("/", ["home/state.js", "home/ssh_keys.js"], """
const initialSdOptionProfiles = {};
let savedSelection = "";
async function saveSdBrowserSelection(value) { savedSelection = value.ssh_key_name; }
let requests = [];
let fail = false;
let names = ["original", "original-2"];
window.fetch = async (url, options) => {
  if (url === "/api/ssh/keys") return {
    ok: true, json: async () => ({ok: true, keys: names.map(key_name => ({key_name, public_key_exists: true}))})
  };
  const body = JSON.parse(options.body);
  requests.push(body);
  if (fail) return {ok: false, json: async () => ({ok: false, errors: ["Name already exists"]})};
  names.push(body.key_name);
  return {ok: true, json: async () => ({
    ok: true, created: true, mode: "generate", key_name: body.key_name,
    public_key: "ssh-ed25519 fresh", private_key_path: "/private/path"
  })};
};
""", """
await refreshSshKeyChoices("original");
check(!document.getElementById("regenerate-ssh-key").disabled, "saved selection enables regeneration");
toggleSshKeyRegeneration(true);
const input = document.getElementById("ssh_key_new_name");
check(input.value === "original-3", "suggestion skips existing names");
check(!document.getElementById("ssh-key-regeneration").hidden, "inline name editor opens");
input.value = "original";
await regenerateSelectedSshKey();
check(requests.length === 0, "existing name cannot be regenerated");
input.value = "fresh-name";
fail = true;
await regenerateSelectedSshKey();
check(sshKeySelectEl.value === "original" && !document.getElementById("ssh-key-regeneration").hidden,
  "failure preserves old selection and editor");
check(sshKeyStatusEl.textContent.includes("Name already exists"), "server error is shown");
fail = false;
await regenerateSelectedSshKey();
check(requests.at(-1).mode === "generate" && requests.at(-1).key_name === "fresh-name",
  "regeneration uses create-only endpoint with new name");
check(form.ssh_public_key.value === "ssh-ed25519 fresh" && form.ssh_public_key.dataset.keyName === "fresh-name",
  "fresh public key is loaded for provisioning");
check(sshKeySelectEl.value === "fresh-name" && savedSelection === "fresh-name",
  "new key is selected and retained");
check(document.getElementById("key-links").textContent.includes("Download private key"),
  "new private key download is available");
check(document.getElementById("ssh-key-regeneration").hidden, "editor closes after success");
toggleSshKeyRegeneration(true);
toggleSshKeyRegeneration(false);
check(sshKeySelectEl.value === "fresh-name", "cancel does not change current key");
""")

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
