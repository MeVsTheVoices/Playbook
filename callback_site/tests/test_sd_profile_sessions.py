"""Session-scoped cloud-init restore without exposing stored credentials."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from test_support import BaseCallbackSiteTest, app, patch_in
from services import sd_profile_sessions, sd_profiles
from services.sd_prepare import JOBS, JOBS_LOCK


class SdProfileSessionTests(BaseCallbackSiteTest):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for module, name, value in (
            (sd_profile_sessions, "SESSION_DIR", root / "sessions"),
            (sd_profiles, "SD_OPTION_PROFILES_FILE", root / "profiles.json"),
            (sd_profiles, "SD_OPTION_PROFILE_STATE_FILE", root / "preferred.json"),
        ):
            guard = patch.object(module, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        self.credentials = {
            "tailscale_auth_key": "tskey-auth-session-proof",
            "toolkit_wifi_password": "session-wifi-proof",
            "toolkit_terminal_password_hash": "$6$salt$session-hash-proof",
            "password": "session-login-proof",
            "telegraf_influx_token": "session-influx-proof",
        }

    def save(self, name="session-proof", options=None, **extra):
        return self.client.post("/api/sd/options-profiles", json={
            "name": name, "profile": "network-toolkit",
            "options": options if options is not None else {**self.credentials, "timezone": "UTC"},
            **extra,
        })

    def test_same_browser_tabs_restore_masks_other_browser_does_not(self):
        response = self.save()
        self.assertEqual(response.status_code, 200)
        cookie = self.client.get_cookie(sd_profile_sessions.SESSION_COOKIE)
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "Strict")
        self.assertIsNone(cookie.expires)
        other_tab = app.test_client()
        other_tab.set_cookie(sd_profile_sessions.SESSION_COOKIE, cookie.value)
        state = other_tab.get("/api/sd/options-profiles").get_json()
        self.assertEqual(state["selected_profile"], "session-proof")
        profile = state["profiles"][0]
        for key in self.credentials:
            self.assertEqual(profile["options"][key], "__CONFIGURED__")
        other_browser = app.test_client()
        isolated = other_browser.get("/api/sd/options-profiles").get_json()
        self.assertEqual(isolated["selected_profile"], "")
        for key in self.credentials:
            self.assertNotIn(key, isolated["profiles"][0]["options"])
        for secret in self.credentials.values():
            self.assertNotIn(secret, json.dumps(state))
            self.assertNotIn(secret, json.dumps(response.get_json()))
            self.assertNotIn(secret, self.client.get("/").get_data(as_text=True))
            self.assertNotIn(secret, sd_profiles.SD_OPTION_PROFILES_FILE.read_text())
        path = next(sd_profile_sessions.SESSION_DIR.glob("*.json"))
        self.assertEqual(json.loads(path.read_text())["credentials"]["session-proof"], self.credentials)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_selection_is_per_browser_and_reset_does_not_delete_credentials(self):
        self.save("first")
        self.save("second")
        self.assertEqual(self.client.post("/api/sd/options-profiles/session", json={"name": "first"}).status_code, 200)
        self.assertEqual(self.client.get("/api/sd/options-profiles").get_json()["selected_profile"], "first")
        self.client.post("/api/sd/options-profiles/session", json={"name": ""})
        state = self.client.get("/api/sd/options-profiles").get_json()
        self.assertEqual(state["selected_profile"], "")
        first = next(item for item in state["profiles"] if item["name"] == "first")
        self.assertEqual(first["options"]["tailscale_auth_key"], "__CONFIGURED__")

    def test_replace_clear_and_save_as_preserve_private_credentials(self):
        self.save()
        self.assertEqual(self.save(options={"timezone": "Europe/London"}).status_code, 200)
        masked = {key: "__CONFIGURED__" for key in self.credentials}
        response = self.save("copy-proof", options=masked, secret_source_profile="session-proof")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["profile"]["options"]["tailscale_auth_key"], "__CONFIGURED__")
        response = self.save("copy-proof", options={"tailscale_auth_key": "tskey-auth-replaced-proof"}, clear_secret_options=["toolkit_wifi_password"])
        self.assertEqual(response.status_code, 200)
        public = response.get_json()["profile"]["options"]
        self.assertNotIn("toolkit_wifi_password", public)
        private = json.loads(next(sd_profile_sessions.SESSION_DIR.glob("*.json")).read_text())["credentials"]["copy-proof"]
        self.assertEqual(private["tailscale_auth_key"], "tskey-auth-replaced-proof")
        self.assertNotIn("toolkit_wifi_password", private)

    def test_prepare_resolves_credentials_before_validation_and_redacts_job(self):
        self.save()
        payload = {
            "profile": "pi5", "hostname": "session-prepare-proof", "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAA proof", "callback_url": "http://192.0.2.1:8088/api/cloud-init/callback",
            "sd_options_profile": "session-proof",
            "cloud_init_options": {"tailscale_auth_key": "__CONFIGURED__"},
        }
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as worker:
            response = self.client.post("/api/sd/prepare", json=payload)
            self.assertEqual(response.status_code, 200)
            resolved = worker.call_args.kwargs["args"][1]
            self.assertEqual(resolved["cloud_init_options"]["tailscale_auth_key"], self.credentials["tailscale_auth_key"])
            job_id = response.get_json()["job_id"]
            with JOBS_LOCK:
                self.assertNotIn(self.credentials["tailscale_auth_key"], json.dumps(JOBS[job_id]))
                JOBS.pop(job_id)
        other = app.test_client()
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as worker:
            response = other.post("/api/sd/prepare", json=payload)
            self.assertEqual(response.status_code, 400)
            worker.assert_not_called()

    def test_toolkit_masked_keys_are_resolved_before_required_checks(self):
        from test_network_toolkit import OPTIONS
        self.assertEqual(self.save(options=OPTIONS).status_code, 200)
        restored = self.client.get("/api/sd/options-profiles").get_json()["profiles"][0]["options"]
        payload = {
            "profile": "network-toolkit", "hostname": "toolkit-session-proof", "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAA proof", "callback_url": "http://192.0.2.1:8088/api/cloud-init/callback",
            "sd_options_profile": "session-proof", "cloud_init_options": restored,
        }
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as worker:
            response = self.client.post("/api/sd/prepare", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        resolved = worker.call_args.kwargs["args"][1]["cloud_init_options"]
        for key in ("tailscale_auth_key", "toolkit_terminal_password_hash", "toolkit_wifi_password"):
            self.assertEqual(resolved[key], OPTIONS[key])
        with JOBS_LOCK:
            JOBS.pop(response.get_json()["job_id"])

    def test_auto_generated_profile_retains_credentials_for_session(self):
        payload = {
            "profile": "pi5", "hostname": "session-generated-proof", "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAA proof", "callback_url": "http://192.0.2.1:8088/api/cloud-init/callback",
            "cloud_init_options": {"tailscale_auth_key": self.credentials["tailscale_auth_key"]},
        }
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread"):
            response = self.client.post("/api/sd/prepare", json=payload)
        self.assertEqual(response.status_code, 200)
        state = self.client.get("/api/sd/options-profiles").get_json()
        self.assertTrue(state["selected_profile"].startswith("generated-session-generated-proof-"))
        self.assertEqual(state["profiles"][0]["options"]["tailscale_auth_key"], "__CONFIGURED__")
        with JOBS_LOCK:
            JOBS.pop(response.get_json()["job_id"])

    def test_missing_mask_source_and_invalid_session_requests_fail_explicitly(self):
        self.assertEqual(self.save(options={"tailscale_auth_key": "__CONFIGURED__"}).status_code, 400)
        for payload in ([], {}, {"name": 1}, {"unknown": ""}):
            self.assertEqual(self.client.post("/api/sd/options-profiles/session", json=payload).status_code, 400)
        self.assertEqual(self.client.post("/api/sd/options-profiles/session", json={"name": "unknown"}).status_code, 404)
        self.assertEqual(self.save(clear_secret_options=["timezone"]).status_code, 400)

    def test_ssh_key_selection_shared_with_tabs_but_not_other_sessions(self):
        key_dir = sd_profile_sessions.SESSION_DIR.parent / "keys"
        key_dir.mkdir()
        (key_dir / "session-key.pub").write_text("ssh-ed25519 AAAA proof")
        with patch("routes.sd.KEYS_DIR", key_dir):
            response = self.client.post("/api/sd/options-profiles/session", json={"ssh_key_name": "session-key"})
            self.assertEqual(response.status_code, 200)
        cookie = self.client.get_cookie(sd_profile_sessions.SESSION_COOKIE)
        tab = app.test_client()
        tab.set_cookie(sd_profile_sessions.SESSION_COOKIE, cookie.value)
        self.assertEqual(tab.get("/api/sd/options-profiles").get_json()["ssh_key_name"], "session-key")
        self.assertEqual(app.test_client().get("/api/sd/options-profiles").get_json()["ssh_key_name"], "")

    def test_delete_forgets_current_session_selection_and_credentials(self):
        self.save()
        self.assertEqual(self.client.delete("/api/sd/options-profiles/session-proof").status_code, 200)
        state = json.loads(next(sd_profile_sessions.SESSION_DIR.glob("*.json")).read_text())
        self.assertEqual(state["selected_profile"], "")
        self.assertNotIn("session-proof", state["credentials"])

    def test_corrupt_or_unwritable_session_never_returns_success(self):
        self.save()
        path = next(sd_profile_sessions.SESSION_DIR.glob("*.json"))
        path.write_text("{")
        with self.assertLogs(level="ERROR"):
            response = self.client.get("/api/sd/options-profiles")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(path.read_text(), "{")
        path.unlink()
        with patch("services.sd_profile_sessions._atomic_write_text", side_effect=OSError("Disk full")), self.assertLogs(level="ERROR"):
            response = self.client.post("/api/sd/options-profiles/session", json={"name": ""})
        self.assertEqual(response.status_code, 500)
