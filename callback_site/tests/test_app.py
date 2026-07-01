from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="callback-site-tests-"))
os.environ["CALLBACK_APPDATA_DIR"] = str(TEST_DATA_DIR)

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app as callback_app  # noqa: E402
from app import app  # noqa: E402
import core as callback_core  # noqa: E402


class CallbackSiteTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    def setUp(self) -> None:
        self.client = app.test_client()

    def test_health_endpoint(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_dashboard_and_pages_render(self) -> None:
        for path in ("/", "/nodes", "/monitor", "/images", "/tailscale-acl", "/settings", "/test-terminal"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, msg=f"expected 200 for {path}")

    def test_home_page_contains_cloud_init_template_controls(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Cloud-Init Optional Settings", text)
        self.assertIn("Profile Selection", text)
        self.assertIn("Save Current As Template", text)
        self.assertIn("Delete Selected Template", text)
        self.assertIn("Reset Optional Fields", text)
        self.assertIn("fields set", text)
        self.assertNotIn("This operation erases the target device", text)

    def test_home_page_embeds_cached_catalog_metadata(self) -> None:
        entry = next(item for item in callback_app.IMAGE_CATALOG if str(item.get("image_url", "")).strip())
        cache_path = str(callback_app.IMAGE_CACHE_DIR / callback_app._safe_catalog_filename(str(entry.get("image_url", "")), str(entry.get("id", ""))))
        callback_core._append_image_history(
            {
                "recorded_at": "2026-07-01T00:00:00+00:00",
                "status": "ok",
                "source": "image_url",
                "source_value": entry.get("image_url", ""),
                "image_url": entry.get("image_url", ""),
                "download_path": cache_path,
                "expected_sha256": entry.get("image_sha256", ""),
                "observed_sha256": entry.get("image_sha256", ""),
            }
        )

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('"cache_path":', text)
        self.assertIn(str(entry.get("id", "")), text)
        self.assertIn(cache_path, text)

    def test_home_page_contains_flash_mascot_hooks(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("id=\"flash-mascot\"", text)
        self.assertIn("Happy flasher online", text)
        self.assertIn("flashMascotEl.classList.add(\"active\")", text)
        self.assertIn('SD_ACTIVE_JOB_KEY = "sd-active-job-id"', text)
        self.assertIn("resumeActiveJobIfAny", text)
        self.assertIn('href="/test-terminal"', text)

    def test_verifier_terminal_page_uses_sidebar_navigation(self) -> None:
        response = self.client.get("/test-terminal")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('class="sidebar"', text)
        self.assertIn('href="/test-terminal"', text)
        self.assertIn('id="sidebar-toggle"', text)
        self.assertIn("resolveVerificationHost", text)
        self.assertIn("hostSelectEl.value.trim() || hostEl.value.trim()", text)

    def test_monitor_page_excludes_cloud_init_template_builder(self) -> None:
        response = self.client.get("/monitor")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn("Cloud-init Option Templates", text)
        self.assertNotIn("Cloud-init Option Selection", text)
        self.assertNotIn("Option ID", text)
        self.assertNotIn("No fields staged.", text)
        self.assertIn("Cloud-init optional templates were moved to the Home page SD Card Imager section.", text)

    def test_monitor_node_and_cloud_init_options(self) -> None:
        summary = self.client.get("/api/monitor/summary")
        self.assertEqual(summary.status_code, 200)
        summary_payload = summary.get_json()
        self.assertTrue(summary_payload["ok"])
        self.assertIn("nodes", summary_payload)
        self.assertIn("profiles", summary_payload)
        self.assertIn("cloud_init_options", summary_payload)

        save_node = self.client.post(
            "/api/monitor/nodes",
            json={
                "name": "lab-node-01",
                "group": "custom",
                "address": "192.168.50.10",
                "enabled": True,
                "notes": "manual node",
            },
        )
        self.assertEqual(save_node.status_code, 200)
        self.assertTrue(save_node.get_json()["ok"])

        save_option = self.client.post(
            "/api/monitor/cloud-init/options",
            json={
                "id": "bootstrap",
                "label": "Bootstrap",
                "description": "base cloud-init option",
                "fields": [
                    {
                        "id": "package_set",
                        "label": "Package Set",
                        "type": "text",
                        "required": False,
                    }
                ],
            },
        )
        self.assertEqual(save_option.status_code, 200)
        self.assertTrue(save_option.get_json()["ok"])

        assign_option = self.client.post(
            "/api/monitor/nodes/lab-node-01/cloud-init",
            json={"option_ids": ["bootstrap"]},
        )
        self.assertEqual(assign_option.status_code, 200)
        assign_payload = assign_option.get_json()
        self.assertTrue(assign_payload["ok"])
        self.assertEqual(assign_payload["option_ids"], ["bootstrap"])

        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "lab-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-SSL-Client-Subject": "CN=lab-node-01",
                "X-SSL-Client-Issuer": "CN=Rack-CA",
                "X-SSL-Client-Fingerprint": "11:22:33",
            },
        )
        self.assertEqual(callback.status_code, 200)

        cert = self.client.get("/api/monitor/nodes/lab-node-01/certificate")
        self.assertEqual(cert.status_code, 200)
        cert_payload = cert.get_json()
        self.assertTrue(cert_payload["ok"])
        self.assertTrue(cert_payload["certificate"]["cert_present"])
        self.assertEqual(cert_payload["certificate"]["cert_subject"], "CN=lab-node-01")

        delete_option = self.client.delete("/api/monitor/cloud-init/options/bootstrap")
        self.assertEqual(delete_option.status_code, 200)
        self.assertTrue(delete_option.get_json()["ok"])

        delete_node = self.client.delete("/api/monitor/nodes/lab-node-01")
        self.assertEqual(delete_node.status_code, 200)
        self.assertTrue(delete_node.get_json()["ok"])

    def test_monitor_empty_by_default_then_callback_discovery(self) -> None:
        clear_resp = self.client.post("/api/cloud-init/events/clear", json={})
        self.assertEqual(clear_resp.status_code, 200)

        initial = self.client.get("/api/monitor/summary")
        self.assertEqual(initial.status_code, 200)
        initial_payload = initial.get_json()
        self.assertTrue(initial_payload["ok"])
        self.assertEqual(initial_payload.get("nodes"), [])

        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "fresh-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "203.0.113.55",
                "X-SSL-Client-Subject": "CN=fresh-node-01",
                "X-SSL-Client-Issuer": "CN=Rack-CA",
                "X-SSL-Client-Fingerprint": "AA:11:BB:22",
            },
        )
        self.assertEqual(callback.status_code, 200)

        after = self.client.get("/api/monitor/summary")
        self.assertEqual(after.status_code, 200)
        after_payload = after.get_json()
        nodes = after_payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "fresh-node-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("source"), "callback")
        self.assertTrue(match[0].get("last_callback"))
        self.assertTrue(match[0].get("cert", {}).get("cert_present"))

        cert = self.client.get("/api/monitor/nodes/fresh-node-01/certificate")
        self.assertEqual(cert.status_code, 200)
        cert_payload = cert.get_json()
        self.assertTrue(cert_payload["ok"])
        self.assertEqual(cert_payload["certificate"]["cert_subject"], "CN=fresh-node-01")

    def test_catalog_availability_endpoint(self) -> None:
        response = self.client.get("/api/sd/catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        entries = payload.get("entries") or []
        self.assertTrue(entries)
        first = entries[0]
        self.assertIn("local_available", first)
        self.assertIn("cache_path", first)

    def test_ssh_key_generate_and_kload_modes(self) -> None:
        key_name = "test-mode-key"

        generated = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "generate"},
        )
        self.assertEqual(generated.status_code, 200)
        generated_payload = generated.get_json()
        self.assertTrue(generated_payload["ok"])
        self.assertTrue(generated_payload["created"])
        self.assertEqual(generated_payload["mode"], "generate")

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

    def test_test_terminal_checks_catalog(self) -> None:
        response = self.client.get("/api/test-terminal/checks")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        checks = payload.get("checks") or []
        check_ids = [c.get("id") for c in checks]
        self.assertIn("identity", check_ids)
        self.assertIn("cloud_init", check_ids)

    def test_test_terminal_targets_include_callback_hosts(self) -> None:
        self.client.post(
            "/api/cloud-init/events/clear",
            json={},
        )
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-target-01",
                "fqdn": "rpi5-target-01.lab.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.78",
            },
        )

        response = self.client.get("/api/test-terminal/targets")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        targets = payload.get("targets") or []
        hosts = [t.get("host") for t in targets]
        self.assertIn("rpi5-target-01", hosts)
        self.assertIn("rpi5-target-01.lab.local", hosts)
        self.assertIn("198.51.100.78", hosts)

    def test_test_terminal_start_rejects_invalid_check_id(self) -> None:
        response = self.client.post(
            "/api/test-terminal/start",
            json={
                "host": "10.0.0.10",
                "username": "ops",
                "key_name": "rack-bootstrap-ed25519",
                "check_id": "run-anything",
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("invalid check_id", payload.get("error", ""))

    def test_test_terminal_start_uses_named_check_command(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertEqual(create_key.status_code, 200)

        with patch("app.threading.Thread") as thread_cls:
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "10.0.0.10",
                    "username": "ops",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with callback_app.TEST_TERMINAL_LOCK:
            job = callback_app.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertTrue(command)
            self.assertEqual(job.get("check_id"), "identity")
            self.assertIn("hostname && whoami && uname -a", command)

        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))

        with callback_app.TEST_TERMINAL_LOCK:
            callback_app.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_sd_option_profiles_crud(self) -> None:
        initial = self.client.get("/api/sd/options-profiles")
        self.assertEqual(initial.status_code, 200)
        self.assertTrue(initial.get_json()["ok"])

        bad = self.client.post(
            "/api/sd/options-profiles",
            json={"name": "", "options": {}},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.get_json()["ok"])

        create = self.client.post(
            "/api/sd/options-profiles",
            json={
                "name": "pi5-baseline",
                "description": "base bootstrap",
                "options": {
                    "timezone": "UTC",
                    "packages": ["git", "curl"],
                    "disable_ssh_password_auth": True,
                },
            },
        )
        self.assertEqual(create.status_code, 200)
        create_payload = create.get_json()
        self.assertTrue(create_payload["ok"])
        self.assertEqual(create_payload["profile"]["name"], "pi5-baseline")

        listed = self.client.get("/api/sd/options-profiles")
        self.assertEqual(listed.status_code, 200)
        listed_payload = listed.get_json()
        names = [p.get("name") for p in listed_payload.get("profiles", [])]
        self.assertIn("pi5-baseline", names)

        deleted = self.client.delete("/api/sd/options-profiles/pi5-baseline")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["ok"])

        missing_delete = self.client.delete("/api/sd/options-profiles/pi5-baseline")
        self.assertEqual(missing_delete.status_code, 404)
        self.assertFalse(missing_delete.get_json()["ok"])

    def test_sanitize_sd_prepare_payload_includes_cloud_init_options(self) -> None:
        cleaned = callback_app._sanitize_sd_prepare_payload(
            {
                "profile": "pi5",
                "hostname": "rack-node-01",
                "device": "/dev/sdb",
                "ssh_public_key": "ssh-ed25519 AAAA",
                "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                "cloud_init_options": {"timezone": "UTC", "packages": ["git"]},
                "sd_options_profile": "base-template",
                "ignored": "value",
            }
        )
        self.assertNotIn("ignored", cleaned)
        self.assertEqual(cleaned["sd_options_profile"], "base-template")
        self.assertEqual(cleaned["cloud_init_options"]["timezone"], "UTC")

    def test_nodes_summary_prefers_callback_ip_over_placeholder_inventory_address(self) -> None:
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.45",
            },
        )

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "rpi5-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("address"), "198.51.100.45")
        self.assertEqual(match[0].get("address_source"), "callback")
        self.assertTrue(match[0].get("last_callback"))

    def test_nodes_monitor_uses_enriched_address_source(self) -> None:
        with patch.object(
            callback_app,
            "_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-live-01",
                    "group": "rpi5_active",
                    "address": "203.0.113.21",
                    "address_source": "callback",
                    "last_callback": "2026-06-29T00:00:00+00:00",
                }
            ],
        ), patch.object(
            callback_app,
            "_ping_status",
            return_value={"reachable": True, "latency_ms": 1.25, "error": ""},
        ):
            monitor = self.client.post("/api/nodes/monitor", json={"send_webhook": False})

        self.assertEqual(monitor.status_code, 200)
        payload = monitor.get_json()
        self.assertTrue(payload["ok"])
        statuses = payload.get("statuses", [])
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].get("address"), "203.0.113.21")
        self.assertEqual(statuses[0].get("address_source"), "callback")
        self.assertEqual(statuses[0].get("last_callback"), "2026-06-29T00:00:00+00:00")

    def test_nodes_summary_matches_callback_aliases(self) -> None:
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-02.lab.local",
                "fqdn": "rpi5-02.lab.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.52",
            },
        )

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "rpi5-02"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("address"), "198.51.100.52")
        self.assertEqual(match[0].get("address_source"), "callback")

    def test_nodes_monitor_surfaces_callback_last_seen(self) -> None:
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-02.lab.local",
                "fqdn": "rpi5-02.lab.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.52",
            },
        )

        monitor = self.client.post("/api/nodes/monitor", json={"send_webhook": False})
        self.assertEqual(monitor.status_code, 200)
        payload = monitor.get_json()
        self.assertTrue(payload["ok"])
        statuses = payload.get("statuses", [])
        match = [n for n in statuses if n.get("name") == "rpi5-02"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("address_source"), "callback")
        self.assertTrue(match[0].get("last_callback"))

    def test_sd_prepare_queues_cloud_init_options_metadata(self) -> None:
        with patch.object(callback_app, "_ensure_prereqs", return_value=[]), patch("app.threading.Thread") as thread_cls:
            response = self.client.post(
                "/api/sd/prepare",
                json={
                    "profile": "pi5",
                    "hostname": "rack-node-02",
                    "device": "/dev/sdb",
                    "ssh_public_key": "ssh-ed25519 AAAA test",
                    "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                    "sd_options_profile": "base-profile",
                    "cloud_init_options": {
                        "timezone": "UTC",
                        "packages": ["git", "curl"],
                    },
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

        self.assertEqual(request_payload.get("sd_options_profile"), "base-profile")
        self.assertEqual(request_payload.get("cloud_init_options", {}).get("timezone"), "UTC")
        self.assertEqual(request_payload.get("cloud_init_options", {}).get("packages"), ["git", "curl"])

        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))
        args_tuple = kwargs.get("args") or ()
        self.assertTrue(len(args_tuple) >= 2)
        self.assertEqual(args_tuple[0], job_id)

        with callback_app.JOBS_LOCK:
            callback_app.JOBS.pop(job_id, None)

    def test_settings_reset_node_ops(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        nodes = summary.get_json().get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for settings reset test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["telegraf"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        set_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "telegraf": {
                        "influx_endpoint": "https://influx.example:8086",
                        "influx_bucket": "telegraf",
                        "influx_org": "homelab",
                        "influx_access_key": "access",
                        "influx_secret_key": "secret",
                    }
                },
            },
        )
        self.assertEqual(set_inputs.status_code, 200)

        reset = self.client.post("/api/settings/reset", json={"scope": "node_ops"})
        self.assertEqual(reset.status_code, 200)
        reset_payload = reset.get_json()
        self.assertTrue(reset_payload["ok"])
        self.assertEqual(reset_payload["scope"], "node_ops")

        after_profiles = self.client.get("/api/nodes/profiles")
        self.assertEqual(after_profiles.status_code, 200)
        after_payload = after_profiles.get_json()
        self.assertEqual(after_payload.get("node_configs"), {})
        self.assertEqual(after_payload.get("node_inputs"), {})

    def test_settings_rack_name_roundtrip(self) -> None:
        current = self.client.get("/api/settings")
        self.assertEqual(current.status_code, 200)
        current_payload = current.get_json()
        self.assertTrue(current_payload["ok"])
        self.assertIn("rack_name", current_payload.get("settings", {}))

        updated = self.client.post("/api/settings", json={"rack_name": "Rack Alpha 01"})
        self.assertEqual(updated.status_code, 200)
        updated_payload = updated.get_json()
        self.assertTrue(updated_payload["ok"])
        self.assertEqual(updated_payload.get("settings", {}).get("rack_name"), "rack-alpha-01")

        reloaded = self.client.get("/api/settings")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.get_json().get("settings", {}).get("rack_name"), "rack-alpha-01")

    def test_fragment_endpoints_render(self) -> None:
        for path in (
            "/fragments/cloud-init/event-rows",
            "/fragments/sd/image-catalog-rows",
            "/fragments/sd/images-rows",
            "/fragments/tailscale-acl/overview",
            "/fragments/tailscale-acl/inputs",
            "/fragments/tailscale-acl/output",
            "/fragments/tailscale-acl/apply-guide",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, msg=f"expected 200 for {path}")

    def test_tailscale_acl_page_lazy_sections(self) -> None:
        response = self.client.get("/tailscale-acl")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('hx-get="/fragments/tailscale-acl/overview"', text)
        self.assertIn('hx-get="/fragments/tailscale-acl/inputs"', text)
        self.assertIn('hx-get="/fragments/tailscale-acl/output"', text)
        self.assertIn('hx-get="/fragments/tailscale-acl/apply-guide"', text)

    def test_image_catalog_is_yaml_backed_and_has_intended_urls(self) -> None:
        entries = {entry.get("id"): entry for entry in callback_core.IMAGE_CATALOG}
        expected_files = {
            "raspios-trixie-arm64": "2026-06-18-raspios-trixie-arm64.img.xz",
            "raspios-trixie-armhf": "2026-06-18-raspios-trixie-armhf.img.xz",
            "raspios-bookworm-arm64-lite": "2026-06-18-raspios-bookworm-arm64-lite.img.xz",
            "raspios-bookworm-armhf-lite": "2026-06-18-raspios-bookworm-armhf-lite.img.xz",
            "ubuntu-24.04-pi-arm64": "ubuntu-24.04.2-preinstalled-server-arm64+raspi.img.xz",
            "ubuntu-22.04-pi-arm64": "ubuntu-22.04.4-preinstalled-server-arm64+raspi.img.xz",
        }

        for entry_id, expected_suffix in expected_files.items():
            self.assertIn(entry_id, entries)
            entry = entries[entry_id]
            self.assertTrue(entry.get("image_url"), msg=f"missing image_url for {entry_id}")
            self.assertTrue(entry["image_url"].endswith(expected_suffix))
            self.assertEqual(
                callback_core._safe_catalog_filename(entry["image_url"], entry_id),
                f"{entry_id}-{expected_suffix}",
            )

    def test_images_page_has_cache_path_color_hooks(self) -> None:
        response = self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("cache-path-ok", text)
        self.assertIn("cache-path-missing", text)
        self.assertIn("source-panel", text)
        self.assertIn("Distro image presets", text)
        self.assertIn("download-state available", text)
        self.assertIn("download-state missing", text)

    def test_image_crud_and_fragment_rows(self) -> None:
        created = self.client.post(
            "/api/sd/images",
            json={
                "label": "Ubuntu 24.04 LTS",
                "profile": "pi5",
                "source": "image_url",
                "source_value": "https://example.invalid/image.img.xz",
                "download_path": "/tmp/image.img.xz",
                "hash_status": "verified",
                "expected_sha256": "abc123",
                "observed_sha256": "abc123",
                "job_status": "completed",
                "exit_code": 0,
                "notes": "seed record",
            },
        )
        self.assertEqual(created.status_code, 201)
        created_payload = created.get_json()
        self.assertTrue(created_payload["ok"])
        record_id = created_payload["record"]["id"]

        listing = self.client.get("/api/sd/images")
        self.assertEqual(listing.status_code, 200)
        listed_entries = listing.get_json()["entries"]
        self.assertTrue(any(entry["id"] == record_id for entry in listed_entries))

        updated = self.client.put(
            f"/api/sd/images/{record_id}",
            json={"label": "Ubuntu 24.04 LTS Updated", "notes": "edited record"},
        )
        self.assertEqual(updated.status_code, 200)
        updated_payload = updated.get_json()
        self.assertEqual(updated_payload["record"]["label"], "Ubuntu 24.04 LTS Updated")
        self.assertEqual(updated_payload["record"]["notes"], "edited record")

        rows = self.client.get("/fragments/sd/images-rows")
        self.assertEqual(rows.status_code, 200)
        self.assertIn("Ubuntu 24.04 LTS Updated", rows.get_data(as_text=True))

        deleted = self.client.delete(f"/api/sd/images/{record_id}")
        self.assertEqual(deleted.status_code, 200)
        after_delete = self.client.get("/api/sd/images")
        self.assertFalse(any(entry["id"] == record_id for entry in after_delete.get_json()["entries"]))

    def test_image_relative_download_path_availability(self) -> None:
        rel_dir = TEST_DATA_DIR / "cache" / "images"
        rel_dir.mkdir(parents=True, exist_ok=True)
        rel_file = rel_dir / "example.img.xz"
        rel_file.write_text("dummy", encoding="utf-8")

        created = self.client.post(
            "/api/sd/images",
            json={
                "label": "Relative Path Image",
                "profile": "pi5",
                "source": "catalog_pull",
                "source_value": "https://example.invalid/example.img.xz",
                "download_path": "cache/images/example.img.xz",
                "hash_status": "verified",
                "job_status": "completed",
                "exit_code": 0,
            },
        )
        self.assertEqual(created.status_code, 201)
        record_id = created.get_json()["record"]["id"]

        listing = self.client.get("/api/sd/images")
        self.assertEqual(listing.status_code, 200)
        rows = listing.get_json().get("entries", [])
        match = [row for row in rows if row.get("id") == record_id]
        self.assertTrue(match)
        self.assertTrue(match[0].get("local_available"))

    def test_catalog_pull_api_validation(self) -> None:
        empty = self.client.post("/api/sd/catalog-pull", json={"catalog_ids": []})
        self.assertEqual(empty.status_code, 400)
        self.assertFalse(empty.get_json()["ok"])

        no_url = self.client.post("/api/sd/catalog-pull", json={"catalog_ids": ["custom-arm64"]})
        self.assertEqual(no_url.status_code, 400)
        no_url_payload = no_url.get_json()
        self.assertFalse(no_url_payload["ok"])
        self.assertIn("custom-arm64", no_url_payload.get("missing_url_ids", []))

        not_found = self.client.get("/api/sd/catalog-pull/jobs/does-not-exist")
        self.assertEqual(not_found.status_code, 404)
        self.assertFalse(not_found.get_json()["ok"])

    def test_catalog_pull_worker_completes_and_records_progress(self) -> None:
        job_id = "job-test-catalog"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }

        def fake_download(url: str, target: Path) -> str:
            target.write_text("payload", encoding="utf-8")
            return "abc123"

        with callback_core.CATALOG_PULL_JOBS_LOCK:
            callback_core.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        with patch.object(callback_core, "_download_catalog_image", side_effect=fake_download), patch.object(
            callback_core,
            "_append_image_history",
            return_value=None,
        ):
            callback_core._run_catalog_pull_job(job_id, [entry], skip_existing=False)

        with callback_core.CATALOG_PULL_JOBS_LOCK:
            job = callback_core.CATALOG_PULL_JOBS[job_id]

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["summary"]["ok"], 1)
        self.assertEqual(job["summary"]["running"], 0)
        self.assertEqual(job["summary"]["total"], 1)
        self.assertEqual(job["results"][0]["status"], "ok")

    def test_callback_event_fragment(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-99",
                "instance_id": "iid-99",
                "fqdn": "rpi5-99.example.invalid",
                "profile": "pi5",
                "stage": "phone_home",
            },
        )
        self.assertEqual(response.status_code, 200)

        fragment = self.client.get("/fragments/cloud-init/event-rows")
        self.assertEqual(fragment.status_code, 200)
        text = fragment.get_data(as_text=True)
        self.assertIn("rpi5-99", text)
        self.assertIn("phone_home", text)

    def test_callback_source_ip_and_cert_capture(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-100",
                "instance_id": "iid-100",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "203.0.113.10, 10.0.0.1",
                "X-SSL-Client-Subject": "CN=rpi5-100",
                "X-SSL-Client-Fingerprint": "AA:BB:CC",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("hostname") == "rpi5-100"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertEqual(latest["source_ip"], "203.0.113.10")
        self.assertEqual(latest["source_ip_source"], "x-forwarded-for")
        self.assertEqual(latest["cert_subject"], "CN=rpi5-100")
        self.assertEqual(latest["cert_fingerprint"], "AA:BB:CC")

    def test_callback_dashboard_payload_shows_network_envelope(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-audit-01",
                "instance_id": "iid-audit-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "192.168.147.23, 10.255.45.243",
                "User-Agent": "Cloud-Init/24.4-0ubuntu1~24.04.2",
            },
        )
        self.assertEqual(response.status_code, 200)

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("source_ip_source", text)
        self.assertIn("forwarded_for", text)
        self.assertIn("remote_addr", text)
        self.assertIn("source: ", text)

    def test_callback_source_ip_fallback_to_x_real_ip(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-101",
                "instance_id": "iid-101",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Real-IP": "198.51.100.22",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("hostname") == "rpi5-101"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertEqual(latest["source_ip"], "198.51.100.22")
        self.assertEqual(latest["source_ip_source"], "x-real-ip")

    def test_callback_dataset_field_is_stripped(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-102",
                "instance_id": "iid-102",
                "profile": "pi5",
                "stage": "phone_home",
                "dataset": {"very": "large"},
                "meta": {
                    "dataset": "inner",
                    "tags": [{"dataset": "nested-list"}, {"ok": True}],
                },
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("hostname") == "rpi5-102"]
        self.assertTrue(matching)
        latest = matching[0]
        raw_text = json.dumps(latest.get("raw", {}), sort_keys=True)
        self.assertNotIn("\"dataset\"", raw_text)

    def test_callback_profile_inferred_from_hostname_when_missing(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-42",
                "instance_id": "iid-42",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-42"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertEqual(latest["profile"], "pi5")
        self.assertEqual(latest["stage"], "phone_home")

    def test_callback_profile_falls_back_to_recent_event(self) -> None:
        first = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "edge-node-01",
                "instance_id": "iid-edge-01",
                "profile": "pibplus",
                "stage": "hardware-check",
            },
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "edge-node-01",
                "instance_id": "iid-edge-01",
            },
        )
        self.assertEqual(second.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-edge-01"]
        self.assertGreaterEqual(len(matching), 2)
        latest = matching[0]
        self.assertEqual(latest["profile"], "pibplus")
        self.assertEqual(latest["stage"], "hardware-check")

    def test_callback_cert_presence_from_alt_header(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-cert-01",
                "instance_id": "iid-cert-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "SSL-Client-Cert": "-----BEGIN CERTIFICATE-----...",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-cert-01"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertTrue(latest["cert_present"])
        self.assertTrue(latest["cert_header_present"])

    def test_callback_ignores_empty_payload_noise(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            data="",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("ignored"))

    def test_callback_backlog_clear(self) -> None:
        create_resp = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-clear-01",
                "instance_id": "iid-clear-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
        )
        self.assertEqual(create_resp.status_code, 200)

        before = self.client.get("/api/cloud-init/events")
        self.assertEqual(before.status_code, 200)
        self.assertTrue(before.get_json().get("events"))

        clear_resp = self.client.post("/api/cloud-init/events/clear", json={})
        self.assertEqual(clear_resp.status_code, 200)
        clear_payload = clear_resp.get_json()
        self.assertTrue(clear_payload.get("ok"))
        self.assertGreaterEqual(int(clear_payload.get("cleared", 0)), 1)

        after = self.client.get("/api/cloud-init/events")
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.get_json().get("events"), [])

    def test_node_profile_assignment_and_check(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        summary_payload = summary.get_json()
        self.assertTrue(summary_payload["ok"])

        nodes = summary_payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for node provisioning test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["baseline", "tailscale"]},
        )
        self.assertEqual(set_profiles.status_code, 200)
        set_payload = set_profiles.get_json()
        self.assertTrue(set_payload["ok"])
        self.assertEqual(set_payload["profile_ids"], ["baseline", "tailscale"])

        save_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "tailscale": {
                        "auth_key": "tskey-auth-test",
                        "tailnet": "tail00c2ce.ts.net",
                    },
                    "baseline": {
                        "timezone": "UTC",
                    },
                },
            },
        )
        self.assertEqual(save_inputs.status_code, 200)
        self.assertTrue(save_inputs.get_json()["ok"])

        run_check = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": False},
        )
        self.assertEqual(run_check.status_code, 200)
        run_payload = run_check.get_json()
        self.assertTrue(run_payload["ok"])
        self.assertEqual(run_payload["mode"], "check")
        self.assertEqual(len(run_payload["results"]), 1)

        node_result = run_payload["results"][0]
        self.assertEqual(node_result["node"], node_name)
        self.assertGreaterEqual(len(node_result["results"]), 1)
        self.assertTrue(all(item["status"] == "planned" for item in node_result["results"]))

        summary_after = self.client.get("/api/nodes/summary")
        self.assertEqual(summary_after.status_code, 200)
        payload_after = summary_after.get_json()
        match = [n for n in payload_after.get("nodes", []) if n.get("name") == node_name]
        self.assertTrue(match)
        self.assertEqual(match[0].get("selected_profiles"), ["baseline", "tailscale"])
        latest_check = match[0].get("latest_check", {})
        self.assertEqual(latest_check.get("status"), "planned")
        self.assertEqual(latest_check.get("mode"), "check")

    def test_node_inputs_ignore_unselected_profiles(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        nodes = payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for profile input scope test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["tailscale"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        save_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "tailscale": {
                        "auth_key": "tskey-auth-test",
                        "tailnet": "tail00c2ce.ts.net",
                    },
                    "telegraf": {
                        "influx_endpoint": "https://influx.example:8086",
                        "influx_bucket": "telegraf",
                        "influx_org": "homelab",
                        "influx_access_key": "access",
                        "influx_secret_key": "secret",
                    },
                },
            },
        )
        self.assertEqual(save_inputs.status_code, 200)

        profiles_state = self.client.get("/api/nodes/profiles")
        self.assertEqual(profiles_state.status_code, 200)
        node_inputs = profiles_state.get_json().get("node_inputs", {}).get(node_name, {})
        self.assertIn("tailscale", node_inputs)
        self.assertNotIn("telegraf", node_inputs)

    def test_node_provision_requires_profile_inputs(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        nodes = payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for input requirement test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["telegraf"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        check_missing = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": False},
        )
        self.assertEqual(check_missing.status_code, 400)
        missing_payload = check_missing.get_json()
        self.assertFalse(missing_payload["ok"])
        self.assertEqual(missing_payload["error"], "missing required provisioning inputs")
        self.assertTrue(missing_payload.get("missing_inputs"))

        save_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "telegraf": {
                        "influx_endpoint": "https://influx.example:8086",
                        "influx_bucket": "telegraf",
                        "influx_org": "homelab",
                        "influx_access_key": "access",
                        "influx_secret_key": "secret",
                    }
                },
            },
        )
        self.assertEqual(save_inputs.status_code, 200)
        self.assertTrue(save_inputs.get_json()["ok"])

        check_ok = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": False},
        )
        self.assertEqual(check_ok.status_code, 200)
        check_payload = check_ok.get_json()
        self.assertTrue(check_payload["ok"])

    def test_prepare_script_escapes_embedded_runtime_vars(self) -> None:
        script_text = (APP_DIR.parent / "scripts" / "prepare_sd_cloud_init.sh").read_text(encoding="utf-8")
        self.assertIn('\\$PI_MODEL', script_text)
        self.assertIn('\\$RAM_MB', script_text)
        self.assertIn('INFLUX_URL="\\${CLOUD_INIT_TELEGRAF_INFLUX_URL:-}"', script_text)


if __name__ == "__main__":
    unittest.main()
