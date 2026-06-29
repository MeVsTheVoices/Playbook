from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="callback-site-tests-"))
os.environ["CALLBACK_APPDATA_DIR"] = str(TEST_DATA_DIR)

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from app import app  # noqa: E402


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
        for path in ("/", "/nodes", "/monitor", "/images", "/tailscale-acl", "/settings"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, msg=f"expected 200 for {path}")

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

    def test_ssh_key_mode_validation(self) -> None:
        response = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "bad-mode-key", "mode": "load"},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])

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


if __name__ == "__main__":
    unittest.main()
