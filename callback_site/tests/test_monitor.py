"""Hardware monitor and the source-of-truth state."""

from __future__ import annotations

try:
    from .test_support import BaseCallbackSiteTest, TEST_DATA_DIR, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, TEST_DATA_DIR, patch_in

import services.images
import services.node_state
from subprocess import CompletedProcess
from unittest.mock import patch

from services.node_inventory import _ping_status


class ReachabilityTests(BaseCallbackSiteTest):
    def test_icmp_success_does_not_open_tcp_connection(self) -> None:
        with patch("services.node_inventory.subprocess.run", return_value=CompletedProcess(
            [], 0, stdout="time=0.714 ms", stderr=""
        )), patch("services.node_inventory.socket.create_connection") as connect:
            result = _ping_status("192.0.2.1")
        self.assertTrue(result["reachable"])
        self.assertEqual(result["latency_ms"], 0.714)
        self.assertEqual(result["probe"], "icmp")
        connect.assert_not_called()

    def test_tcp_success_when_icmp_is_blocked(self) -> None:
        with patch("services.node_inventory.subprocess.run", return_value=CompletedProcess(
            [], 1, stdout="100% packet loss", stderr=""
        )), patch("services.node_inventory.socket.create_connection") as connect:
            result = _ping_status("192.0.2.1", port=2222)
        connect.assert_called_once_with(("192.0.2.1", 2222), timeout=1)
        self.assertTrue(result["reachable"])
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(result["probe"], "tcp")
        self.assertEqual(result["error"], "")

    def test_missing_ping_still_checks_tcp(self) -> None:
        with patch("services.node_inventory.subprocess.run", side_effect=FileNotFoundError), patch(
            "services.node_inventory.socket.create_connection"
        ):
            result = _ping_status("192.0.2.1")
        self.assertTrue(result["reachable"])
        self.assertEqual(result["probe"], "tcp")

    def test_both_failures_are_reported(self) -> None:
        with patch("services.node_inventory.subprocess.run", return_value=CompletedProcess(
            [], 1, stdout="", stderr="ICMP blocked"
        )), patch("services.node_inventory.socket.create_connection", side_effect=TimeoutError("timed out")):
            result = _ping_status("192.0.2.1")
        self.assertFalse(result["reachable"])
        self.assertIsNone(result["latency_ms"])
        self.assertIn("ICMP blocked", result["error"])
        self.assertIn("TCP port 22: timed out", result["error"])


class MonitorTests(BaseCallbackSiteTest):
    def test_monitor_page_excludes_cloud_init_template_builder(self) -> None:
        response = self.client.get("/monitor")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn("Cloud-init Option Templates", text)
        self.assertNotIn("Cloud-init Option Selection", text)
        self.assertNotIn("No fields staged.", text)
        self.assertIn("Hardware Monitor", text)
        self.assertIn("Hardware Status", text)
        self.assertIn("Cloud-Init Coverage And Security Profile", text)
        self.assertIn("Latest Provision Check Outcomes", text)
        self.assertIn('id="fleet-kpis"', text)

    def test_monitor_page_exposes_discovered_node_closer_controls(self) -> None:
        response = self.client.get("/monitor")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn('id="monitor-node-select"', text)
        self.assertNotIn('id="prune-stale-monitor"', text)
        self.assertIn('id="monitor-run-check"', text)
        self.assertIn('id="summary-run-ping"', text)
        self.assertIn('id="monitor-run-ping"', text)
        self.assertIn('id="node-form-ping"', text)
        self.assertIn("Single Source of Truth", text)
        self.assertIn('id="sot-kpis"', text)
        self.assertIn('id="sot-body"', text)
        self.assertIn("Discovered node", text)
        self.assertIn("Storage (GB)", text)
        self.assertIn("<th>Architecture</th>", text)
        self.assertIn('escapeHtml(hw.architecture || "-")', text)
        self.assertIn('colspan="7"', text)
        self.assertIn("colspan='7'", text)
        self.assertIn("GB total", text)
        self.assertIn("GB SD", text)
        self.assertIn("GB NVMe", text)
        self.assertIn("nvme_capacity_gb", text)

    def test_monitor_page_shows_hardware_first_callback_copy(self) -> None:
        response = self.client.get("/monitor")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn("First callback:", text)
        self.assertIn("font-size:1rem; font-weight:700;", text)

    def test_source_of_truth_endpoint_unifies_image_monitor_and_provision_state(self) -> None:
        cache_path = TEST_DATA_DIR / "image_cache" / "pi5-latest.img.xz"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("image-bytes-placeholder", encoding="utf-8")

        services.images._append_image_history(
            {
                "recorded_at": "2026-07-12T00:00:00+00:00",
                "profile": "pi5",
                "label": "Pi5 Base",
                "source": "image_url",
                "source_value": "https://example.invalid/pi5.img.xz",
                "download_path": str(cache_path),
                "job_status": "completed",
                "hash_status": "verified",
            }
        )

        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "truth-node-01",
                "profile": "pi5",
                "stage": "hardware-check",
                "hardware": {
                    "model": "Raspberry Pi 5",
                    "architecture": "aarch64",
                    "ram_mb": 8192,
                },
            },
            headers={"X-Forwarded-For": "198.51.100.20"},
        )

        state = services.node_state._load_node_provisioning()
        state.setdefault("node_configs", {})["truth-node-01"] = ["baseline", "telegraf"]
        services.node_state._save_node_provisioning(state)

        response = self.client.get("/api/state/source-of-truth")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        unified = payload.get("state", {})
        self.assertIn("counts", unified)
        self.assertIn("images", unified)
        self.assertIn("nodes", unified)

        nodes = unified.get("nodes", [])
        match = next((node for node in nodes if node.get("name") == "truth-node-01"), None)
        self.assertIsNotNone(match)
        self.assertIn("baseline", match.get("selected_profiles", []))
        self.assertEqual(match.get("callback_source_ip"), "198.51.100.20")
        self.assertIn("hardware", match)
        self.assertEqual(match["hardware"]["architecture"], "aarch64")

        summary = self.client.get("/api/monitor/summary")
        self.assertEqual(summary.status_code, 200)
        monitor_node = next(node for node in summary.get_json()["nodes"] if node["name"] == "truth-node-01")
        self.assertEqual(monitor_node["hardware"]["architecture"], "aarch64")

        image = (match or {}).get("image", {})
        self.assertEqual(image.get("source"), "image_url")
        self.assertTrue(image.get("local_available"))

        counts = unified.get("counts", {})
        self.assertGreaterEqual(int(counts.get("nodes_in_monitor", 0) or 0), 1)
        self.assertGreaterEqual(int(counts.get("nodes_with_hardware", 0) or 0), 1)

    def test_monitor_summary_uses_canonical_source_of_truth(self) -> None:
        fake_state = {
            "nodes": [{"name": "mon-a", "in_monitor_view": True, "hardware": {"received_at": "2026-07-12T00:00:00+00:00"}}],
            "monitor": {
                "cloud_init_options": [{"id": "base", "label": "Base"}],
                "node_cloud_init": {"mon-a": ["base"]},
            }
        }

        with patch_in("_single_source_of_truth_summary", return_value=fake_state):
            response = self.client.get("/api/monitor/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload.get("nodes", [])[0].get("name"), "mon-a")
        self.assertEqual(payload.get("cloud_init_options", [])[0].get("id"), "base")

    def test_monitor_summary_resolves_node_cloud_init_by_alias(self) -> None:
        services.node_state._save_monitor_state(
            {
                "managed_nodes": {},
                "cloud_init_options": {
                    "bootstrap": {
                        "id": "bootstrap",
                        "label": "Bootstrap",
                        "description": "alias test option",
                        "fields": [],
                    }
                },
                "node_cloud_init": {
                    "lab-node-01.example.local": ["bootstrap"],
                },
            }
        )

        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "lab-node-01",
                "fqdn": "lab-node-01.example.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
        )
        self.assertEqual(callback.status_code, 200)

        summary = self.client.get("/api/monitor/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [node for node in nodes if node.get("name") == "lab-node-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("cloud_init_option_ids"), ["bootstrap"])

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

    def test_monitor_cloud_init_option_delete_clears_legacy_mixed_case_assignments(self) -> None:
        services.node_state._save_monitor_state(
            {
                "managed_nodes": {},
                "cloud_init_options": {
                    "bootstrap": {
                        "id": "bootstrap",
                        "label": "Bootstrap",
                        "description": "legacy delete cleanup",
                        "fields": [],
                    }
                },
                "node_cloud_init": {
                    "lab-node-01": ["Bootstrap", "Other"],
                },
            }
        )

        delete_option = self.client.delete("/api/monitor/cloud-init/options/bootstrap")
        self.assertEqual(delete_option.status_code, 200)
        self.assertTrue(delete_option.get_json()["ok"])

        state = services.node_state._load_monitor_state()
        self.assertEqual(state.get("node_cloud_init", {}).get("lab-node-01"), ["Other"])

    def test_monitor_empty_by_default_then_callback_discovery(self) -> None:
        clear_resp = self.client.post("/api/cloud-init/events/clear", json={})
        self.assertEqual(clear_resp.status_code, 200)
        services.node_state._save_monitor_state(services.node_state._default_monitor_state())

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

    def test_source_of_truth_preserves_callback_source_for_discovered_node(self) -> None:
        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "source-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={"X-Forwarded-For": "203.0.113.61"},
        )
        self.assertEqual(callback.status_code, 200)

        response = self.client.get("/api/state/source-of-truth")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        nodes = payload.get("state", {}).get("nodes", [])
        node = next((item for item in nodes if item.get("name") == "source-node-01"), None)
        self.assertIsNotNone(node)
        self.assertTrue(bool(node.get("in_node_ops_view")))
        self.assertTrue(bool(node.get("in_monitor_view")))
        self.assertEqual(node.get("source"), "callback")

    def test_monitor_node_simple_ping_endpoint(self) -> None:
        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "ping-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={"X-Forwarded-For": "203.0.113.88"},
        )
        self.assertEqual(callback.status_code, 200)

        with patch_in("_ping_status", return_value={"reachable": True, "latency_ms": 2.4, "error": ""}) as ping_status:
            ping = self.client.get("/api/monitor/nodes/ping-node-01/ping")

        self.assertEqual(ping.status_code, 200)
        payload = ping.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["node"], "ping-node-01")
        self.assertEqual(payload["address"], "203.0.113.88")
        self.assertTrue(payload["reachable"])
        self.assertEqual(payload["latency_ms"], 2.4)
        ping_status.assert_called_once_with("203.0.113.88")

    def test_monitor_node_simple_ping_endpoint_missing_node(self) -> None:
        response = self.client.get("/api/monitor/nodes/not-there/ping")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
