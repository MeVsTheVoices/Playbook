"""Node summaries, inventory view, manual nodes and stale pruning."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.events
import services.network
import services.node_inventory
import services.node_state


class NodeInventoryTests(BaseCallbackSiteTest):
    def test_nodes_summary_uses_canonical_source_of_truth(self) -> None:
        fake_state = {
            "nodes": [{"name": "node-a", "selected_profiles": ["baseline"], "latest_check": {"ok": True}, "in_node_ops_view": True}],
            "provisioning": {
                "config": {"username": "ops", "password_set": True, "webhook_url": "https://example.invalid", "verify_tls": True},
                "node_inputs": {"node-a": {"baseline": {"timezone": "UTC"}}},
                "manual_nodes": {"node-a": {"address": "192.0.2.20"}},
            },
            "images": {"summary": {"total_records": 2}},
        }

        with patch_in("_single_source_of_truth_summary", return_value=fake_state):
            response = self.client.get("/api/nodes/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload.get("nodes", [])[0].get("name"), "node-a")
        self.assertEqual(payload.get("config", {}).get("username"), "ops")
        self.assertEqual(payload.get("image_summary", {}).get("total_records"), 2)

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

    def test_manual_node_crud_surfaces_in_nodes_summary(self) -> None:
        create = self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "manual-node-01",
                "group": "manual",
                "address": "192.168.174.238",
                "username": "jdizzle",
                "password": "not-empty",
                "default_shell": "/bin/bash",
                "certificate": "rack-bootstrap-ed25519",
            },
        )
        self.assertEqual(create.status_code, 200)
        create_payload = create.get_json()
        self.assertTrue(create_payload["ok"])
        self.assertTrue(create_payload["node"].get("password_set"))

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "manual-node-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("group"), "manual")
        self.assertEqual(match[0].get("address"), "192.168.174.238")
        self.assertTrue(match[0].get("manual_node"))

        delete = self.client.delete("/api/nodes/manual-nodes/manual-node-01")
        self.assertEqual(delete.status_code, 200)
        self.assertTrue(delete.get_json()["ok"])

    def test_manual_node_password_persists_to_provisioning_state(self) -> None:
        response = self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "manual-node-password-01",
                "group": "manual",
                "address": "192.168.174.239",
                "username": "jdizzle",
                "password": "expected-password-value",
                "certificate": "rack-bootstrap-ed25519",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["node"].get("password_set"))

        state = services.node_state._load_node_provisioning()
        manual_nodes = state.get("manual_nodes", {}) if isinstance(state.get("manual_nodes"), dict) else {}
        self.assertIn("manual-node-password-01", manual_nodes)
        saved_node = manual_nodes["manual-node-password-01"]
        self.assertEqual(str(saved_node.get("password", "")), "expected-password-value")

    def test_manual_node_delete_missing_returns_404(self) -> None:
        response = self.client.delete("/api/nodes/manual-nodes/does-not-exist")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload["ok"])

    def test_node_ops_inventory_view_hides_default_untouched_inventory_nodes(self) -> None:
        with patch_in("_inventory_nodes",
            return_value=[{"name": "rpi5-default-01", "group": "rpi5_active", "address": "192.168.1.44"}],
        ), patch_in("_latest_callback_details_by_host",
            return_value={},
        ), patch_in("_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}},
        ):
            view = services.node_inventory._node_ops_inventory_view()

        self.assertEqual(view, [])

    def test_node_ops_inventory_view_includes_state_only_nodes(self) -> None:
        with patch_in("_inventory_nodes",
            return_value=[],
        ), patch_in("_latest_callback_details_by_host",
            return_value={},
        ), patch_in("_load_node_provisioning",
            return_value={
                "node_configs": {"node-from-state": ["identity"]},
                "node_inputs": {},
                "results": {},
            },
        ):
            view = services.node_inventory._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("name"), "node-from-state")
        self.assertEqual(view[0].get("group"), "provisioned")

    def test_node_ops_inventory_view_includes_callback_only_nodes(self) -> None:
        with patch_in("_inventory_nodes",
            return_value=[],
        ), patch_in("_latest_callback_details_by_host",
            return_value={
                "fresh-callback-01": {
                    "last_callback": "2026-07-10T00:00:00+00:00",
                    "source_ip": "192.168.174.88",
                    "profile": "pi5",
                }
            },
        ), patch_in("_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}, "manual_nodes": {}},
        ):
            view = services.node_inventory._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("name"), "fresh-callback-01")
        self.assertEqual(view[0].get("group"), "rpi5_active")
        self.assertEqual(view[0].get("address"), "192.168.174.88")
        self.assertEqual(view[0].get("address_source"), "callback")

    def test_node_ops_inventory_view_preserves_callback_address_source(self) -> None:
        with patch_in("_inventory_nodes",
            return_value=[],
        ), patch_in("_latest_callback_details_by_host",
            return_value={
                "seeded-node": {
                    "last_callback": "2026-07-10T00:00:00+00:00",
                    "source_ip": "10.255.45.207",
                    "profile": "pi5",
                }
            },
        ), patch_in("_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}, "manual_nodes": {}},
        ):
            view = services.node_inventory._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("address_source"), "callback")

    def test_node_ops_inventory_view_uses_connectable_callback_ip_from_history(self) -> None:
        with patch_in("_inventory_nodes",
            return_value=[],
        ), patch_in("_latest_callback_details_by_host",
            return_value={
                "seeded-node": {
                    "last_callback": "2026-07-10T00:00:00+00:00",
                    "source_ip": "10.255.45.207",
                    "source_ips": ["10.255.45.207", "192.168.174.88"],
                    "source_ip_source": "remote_addr_transit",
                    "profile": "pi5",
                }
            },
        ), patch_in("_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}, "manual_nodes": {}},
        ):
            view = services.node_inventory._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("callback_source_ip"), "10.255.45.207")
        self.assertEqual(view[0].get("callback_connect_ip"), "192.168.174.88")
        self.assertEqual(view[0].get("address"), "192.168.174.88")
        self.assertEqual(view[0].get("address_source"), "callback")

    def test_preferred_connect_ip_from_history_falls_back_to_usable_private_ip(self) -> None:
        selected = services.network._preferred_connect_ip_from_history(
            "10.255.45.207",
            ["10.255.45.207", "192.168.174.88"],
        )
        self.assertEqual(selected, "192.168.174.88")

    def test_nodes_monitor_uses_enriched_address_source(self) -> None:
        with patch_in("_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-live-01",
                    "group": "rpi5_active",
                    "address": "203.0.113.21",
                    "address_source": "callback",
                    "last_callback": "2026-06-29T00:00:00+00:00",
                }
            ],
        ), patch_in("_ping_status",
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

    def test_nodes_summary_includes_latest_callback_metadata(self) -> None:
        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-01",
                "instance_id": "iid-meta-01",
                "profile": "pi5",
                "stage": "runcmd",
                "node_report": {"source": "unit-test"},
            },
            headers={
                "X-Forwarded-For": "198.51.100.45",
                "X-SSL-Client-Subject": "CN=rpi5-01",
                "X-SSL-Client-Fingerprint": "AA:BB:CC:DD",
            },
        )
        self.assertEqual(callback.status_code, 200)

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "rpi5-01"]
        self.assertTrue(match)
        row = match[0]
        self.assertEqual(row.get("callback_profile"), "pi5")
        self.assertEqual(row.get("callback_stage"), "runcmd")
        self.assertEqual(row.get("callback_instance_id"), "iid-meta-01")
        self.assertEqual(row.get("callback_fqdn"), "")
        self.assertEqual(row.get("callback_source_ip"), "198.51.100.45")
        self.assertEqual(row.get("callback_source_ips"), ["198.51.100.45"])
        self.assertEqual(row.get("callback_source_ip_source"), "x-forwarded-for")
        self.assertTrue(row.get("callback_cert_present"))
        self.assertEqual(row.get("callback_cert_subject"), "CN=rpi5-01")
        self.assertEqual(row.get("callback_cert_fingerprint"), "AA:BB:CC:DD")
        self.assertTrue(row.get("callback_node_report_present"))
        self.assertTrue(str(row.get("callback_node_report_id", "")).strip())

    def test_latest_callback_details_uses_most_recent_source_ip_and_tracks_history(self) -> None:
        events = [
            {
                "received_at": "2026-01-01T00:00:02+00:00",
                "hostname": "node-01",
                "fqdn": "node-01.lab.local",
                "source_ip": "10.255.45.243",
                "source_ip_source": "remote_addr_transit",
                "profile": "pi5",
                "raw_profile": "pi5",
                "profile_source": "payload",
                "stage": "power_restore",
                "instance_id": "iid-2",
                "cert_present": False,
                "node_report_present": False,
            },
            {
                "received_at": "2026-01-01T00:00:01+00:00",
                "hostname": "node-01",
                "fqdn": "node-01.lab.local",
                "source_ip": "192.168.174.100",
                "source_ip_source": "payload:source_ip",
                "profile": "pi5",
                "raw_profile": "pi5",
                "profile_source": "payload",
                "stage": "hardware-check",
                "instance_id": "iid-1",
                "cert_present": False,
                "node_report_present": True,
            },
        ]
        with patch_in("_read_events", return_value=events):
            details = services.node_inventory._latest_callback_details_by_host(limit=10)

        self.assertEqual(details["node-01"]["source_ip"], "10.255.45.243")
        self.assertEqual(details["node-01"]["source_ip_source"], "remote_addr_transit")
        self.assertEqual(details["node-01"]["fqdn"], "node-01.lab.local")
        self.assertEqual(details["node-01"]["source_ips"], ["10.255.45.243", "192.168.174.100"])

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

    def test_prune_stale_nodes_rejects_invalid_hours(self) -> None:
        response = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": "abc", "dry_run": True},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])

    def test_prune_stale_nodes_parses_string_dry_run_false(self) -> None:
        stale_ts = "2020-01-01T00:00:00+00:00"
        services.events._append_event(
            {
                "received_at": stale_ts,
                "hostname": "string-dryrun-node",
                "fqdn": "string-dryrun-node.lab.local",
                "instance_id": "iid-string-dryrun",
                "profile": "pi5",
                "stage": "phone_home",
                "source_ip": "198.51.100.77",
                "source_ip_source": "x-forwarded-for",
            }
        )

        execute = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": 24, "dry_run": "false"},
        )
        self.assertEqual(execute.status_code, 200)
        payload = execute.get_json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dry_run"])
        names = {row.get("name") for row in payload.get("stale_nodes", [])}
        self.assertIn("string-dryrun-node", names)

    def test_prune_stale_nodes_propagates_across_all_state(self) -> None:
        stale_ts = "2020-01-01T00:00:00+00:00"
        fresh_ts = datetime.now(timezone.utc).isoformat()

        services.events._append_event(
            {
                "received_at": stale_ts,
                "hostname": "stale-node-01",
                "fqdn": "stale-node-01.lab.local",
                "instance_id": "iid-stale-01",
                "profile": "pi5",
                "stage": "phone_home",
                "source_ip": "198.51.100.11",
                "source_ip_source": "x-forwarded-for",
            }
        )
        services.events._append_event(
            {
                "received_at": fresh_ts,
                "hostname": "fresh-node-01",
                "fqdn": "fresh-node-01.lab.local",
                "instance_id": "iid-fresh-01",
                "profile": "pi5",
                "stage": "phone_home",
                "source_ip": "198.51.100.12",
                "source_ip_source": "x-forwarded-for",
            }
        )

        monitor_state = services.node_state._load_monitor_state()
        monitor_state["managed_nodes"] = {
            "stale-node-01": {
                "name": "stale-node-01",
                "group": "rpi5_active",
                "address": "198.51.100.11",
                "notes": "stale",
                "enabled": True,
            },
            "fresh-node-01": {
                "name": "fresh-node-01",
                "group": "rpi5_active",
                "address": "198.51.100.12",
                "notes": "fresh",
                "enabled": True,
            },
        }
        monitor_state["node_cloud_init"] = {
            "stale-node-01.lab.local": ["base"],
            "fresh-node-01": ["base"],
        }
        services.node_state._save_monitor_state(monitor_state)

        provision_state = services.node_state._load_node_provisioning()
        provision_state["node_configs"] = {
            "stale-node-01.lab.local": ["baseline"],
            "fresh-node-01": ["baseline"],
        }
        provision_state["node_inputs"] = {
            "stale-node-01.lab.local": {"baseline": {"timezone": "UTC"}},
            "fresh-node-01": {"baseline": {"timezone": "UTC"}},
        }
        provision_state["results"] = {
            "stale-node-01.lab.local": {"checked_at": stale_ts},
            "fresh-node-01": {"checked_at": fresh_ts},
        }
        provision_state["manual_nodes"] = {
            "stale-node-01.lab.local": {
                "name": "stale-node-01",
                "group": "manual",
                "address": "198.51.100.11",
            },
            "fresh-node-01": {"name": "fresh-node-01", "group": "manual", "address": "198.51.100.12"},
        }
        services.node_state._save_node_provisioning(provision_state)

        preview = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": 24, "dry_run": True},
        )
        self.assertEqual(preview.status_code, 200)
        preview_payload = preview.get_json()
        self.assertTrue(preview_payload["ok"])
        self.assertTrue(preview_payload["dry_run"])
        preview_names = {row.get("name") for row in preview_payload.get("stale_nodes", [])}
        self.assertIn("stale-node-01", preview_names)

        dry_run_summary = self.client.get("/api/nodes/summary").get_json()
        dry_run_names = {row.get("name") for row in dry_run_summary.get("nodes", [])}
        self.assertIn("stale-node-01", dry_run_names)

        execute = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": 24, "dry_run": False},
        )
        self.assertEqual(execute.status_code, 200)
        execute_payload = execute.get_json()
        self.assertTrue(execute_payload["ok"])
        self.assertFalse(execute_payload["dry_run"])
        self.assertGreaterEqual(int(execute_payload.get("removed", {}).get("callbacks", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_cloud_init", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_configs", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_inputs", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_results", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("manual_nodes", 0) or 0), 1)

        nodes_summary = self.client.get("/api/nodes/summary")
        self.assertEqual(nodes_summary.status_code, 200)
        nodes_payload = nodes_summary.get_json()
        nodes_names = {row.get("name") for row in nodes_payload.get("nodes", [])}
        self.assertNotIn("stale-node-01", nodes_names)
        self.assertIn("fresh-node-01", nodes_names)

        monitor_summary = self.client.get("/api/monitor/summary")
        self.assertEqual(monitor_summary.status_code, 200)
        monitor_payload = monitor_summary.get_json()
        monitor_names = {row.get("name") for row in monitor_payload.get("nodes", [])}
        self.assertNotIn("stale-node-01", monitor_names)
        self.assertIn("fresh-node-01", monitor_names)

        monitor_state_after = services.node_state._load_monitor_state()
        self.assertNotIn("stale-node-01.lab.local", monitor_state_after.get("node_cloud_init", {}))

        provision_after = services.node_state._load_node_provisioning()
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("node_configs", {}))
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("node_inputs", {}))
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("results", {}))
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("manual_nodes", {}))

    def test_preferred_node_connect_target_avoids_transit_inventory_when_callback_is_usable(self) -> None:
        target, source = services.network._preferred_node_connect_target(
            "node-01",
            "10.255.45.243",
            "192.168.2.20",
        )
        self.assertEqual(target, "192.168.2.20")
        self.assertEqual(source, "callback")

    def test_preferred_node_connect_target_uses_recent_callback_over_inventory(self) -> None:
        target, source = services.network._preferred_node_connect_target(
            "node-01",
            "192.168.2.99",
            "192.168.2.20",
        )
        self.assertEqual(target, "192.168.2.20")
        self.assertEqual(source, "callback")

    def test_preferred_node_connect_target_prefers_inventory_when_callback_not_usable(self) -> None:
        target, source = services.network._preferred_node_connect_target(
            "node-01",
            "192.168.2.99",
            "8.8.8.8",
        )
        self.assertEqual(target, "192.168.2.99")
        self.assertEqual(source, "inventory")

    def test_latest_hardware_by_host_keeps_most_recent_event(self) -> None:
        older = {
            "stage": "hardware-check",
            "received_at": "2025-01-01T00:00:00+00:00",
            "hostname": "node-01",
            "fqdn": "node-01.example",
            "raw": {"hardware": {"model": "old", "ram_mb": 512, "nvme_capacity_gb": 0}},
        }
        newer = {
            "stage": "hardware-check",
            "received_at": "2025-01-01T00:05:00+00:00",
            "hostname": "node-01",
            "fqdn": "node-01.example",
            "raw": {"hardware": {"model": "new", "ram_mb": 2048, "nvme_capacity_gb": 953}},
        }
        with patch_in("_read_events", return_value=[older, newer]), patch_in("_first_callback_by_host",
            return_value={"node-01": "2024-12-31T23:59:58+00:00"},
        ):
            result = services.node_inventory._latest_hardware_by_host(limit=10)
        self.assertEqual(result["node-01"]["model"], "new")
        self.assertEqual(result["node-01"]["ram_mb"], 2048)
        self.assertEqual(result["node-01"]["nvme_capacity_gb"], 953)
        self.assertEqual(result["node-01"]["first_callback"], "2024-12-31T23:59:58+00:00")

    def test_manual_node_requires_certificate(self) -> None:
        create = self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "manual-node-no-cert",
                "group": "manual",
                "address": "192.168.174.239",
                "username": "jdizzle",
                "password": "not-empty",
                "default_shell": "/bin/bash",
                "certificate": "",
            },
        )
        self.assertEqual(create.status_code, 400)
        payload = create.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "certificate is required")
