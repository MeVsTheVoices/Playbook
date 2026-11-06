"""Connection profiles: per-node candidates, replaceable selection strategy and stored history."""

from __future__ import annotations

import json
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.connection_profiles as cp
import services.storage


class ConnectionProfileTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        services.storage.CONNECTION_PROFILES_FILE.unlink(missing_ok=True)
        self.addCleanup(services.storage.CONNECTION_PROFILES_FILE.unlink, missing_ok=True)

    def test_nat_node_gets_one_profile_per_way_to_reach_it(self) -> None:
        with patch_in("_tailscale_peer_ip", ["services.connection_profiles"], return_value="100.100.2.3"):
            profiles = cp.gather_profiles("rack-beta", "192.168.174.222", "192.168.174.222", user="ops", key="rack-bootstrap-ed25519")
        self.assertEqual([p.id for p in profiles], ["callback", "inventory", "tailnet", "hostname"])
        self.assertTrue(all(p.user == "ops" and p.key == "rack-bootstrap-ed25519" for p in profiles))
        self.assertEqual(cp.select_profile(profiles).id, "tailnet")

    def test_default_strategy_keeps_the_established_precedence(self) -> None:
        cases = [
            (("n", "100.70.1.5", "192.168.174.88"), ("100.70.1.5", "inventory")),
            (("n", "192.168.2.99", "100.70.1.6"), ("100.70.1.6", "callback")),
            (("n", "192.168.2.99", "192.168.2.20"), ("192.168.2.20", "callback")),
            (("n", "192.168.2.99", "10.255.45.243"), ("192.168.2.99", "inventory")),
            (("n", "10.255.45.5", "10.255.45.243"), ("10.255.45.243", "callback_recent")),
            (("n", "10.255.45.5", ""), ("10.255.45.5", "inventory_transit")),
            (("n", "", ""), ("n", "hostname")),
        ]
        with patch_in("_resolve_lan_ip_from_names", ["services.connection_profiles"], return_value=("", "")):
            for args, expected in cases:
                self.assertEqual(cp.select_connection_target(*args), expected, args)
        self.assertEqual(cp.select_connection_target("", "", ""), ("", "none"))

    def test_strategy_can_be_switched_and_replaced(self) -> None:
        profiles = [
            cp.ConnectionProfile("callback", "callback", "192.168.2.20", "callback", False, True),
            cp.ConnectionProfile("tailnet", "tailnet", "100.100.2.3", "tailscale", True, True),
        ]
        self.assertEqual(cp.select_profile(profiles).id, "tailnet")
        self.assertEqual(cp.select_profile(profiles, "lan_first").id, "callback")
        with patch.dict("os.environ", {"CALLBACK_CONNECTION_STRATEGY": "lan_first"}):
            self.assertEqual(cp.select_profile(profiles).id, "callback")

        cp.register_strategy("test_last", lambda items: items[-1])
        self.addCleanup(cp._STRATEGIES.pop, "test_last", None)
        with patch.dict("os.environ", {"CALLBACK_CONNECTION_STRATEGY": "test_last"}):
            self.assertEqual(cp.select_profile(profiles).id, "tailnet")
        with patch.dict("os.environ", {"CALLBACK_CONNECTION_STRATEGY": "no-such-strategy"}):
            self.assertEqual(cp.active_strategy_name(), cp.DEFAULT_STRATEGY)

    def test_network_helper_delegates_to_the_active_strategy(self) -> None:
        import services.network

        with patch.dict("os.environ", {"CALLBACK_CONNECTION_STRATEGY": "lan_first"}), patch_in(
            "_tailscale_peer_ip", ["services.connection_profiles"], return_value="100.100.2.3"
        ):
            self.assertEqual(services.network._preferred_node_connect_target("n", "192.168.2.99", "192.168.2.20"), ("192.168.2.20", "callback"))

    def test_profiles_are_remembered_and_only_rewritten_on_change(self) -> None:
        profiles = [cp.ConnectionProfile("callback", "callback", "192.168.2.20", "callback", False, True)]
        cp.remember_profiles("node-a", profiles, "callback", reachable=True)
        stored = json.loads(services.storage.CONNECTION_PROFILES_FILE.read_text(encoding="utf-8"))["nodes"]["node-a"]
        self.assertEqual(stored["selected"], "callback")
        record = stored["profiles"]["callback"]
        self.assertEqual((record["address"], record["reachable"]), ("192.168.2.20", True))
        self.assertIn("last_ok_at", record)

        before = services.storage.CONNECTION_PROFILES_FILE.read_text(encoding="utf-8")
        cp.remember_profiles("node-a", profiles, "callback", reachable=True)
        self.assertEqual(services.storage.CONNECTION_PROFILES_FILE.read_text(encoding="utf-8"), before)

        cp.remember_profiles("node-a", [cp.ConnectionProfile("callback", "callback", "192.168.2.21", "callback", False, True)], "callback", reachable=False)
        record = cp._read_node_profiles("node-a")["profiles"]["callback"]
        self.assertEqual((record["address"], record["reachable"]), ("192.168.2.21", False))
        self.assertIn("last_failed_at", record)
        self.assertIn("first_seen", record)

    def test_monitor_poll_records_the_profiles_it_chose(self) -> None:
        self.client.post("/api/cloud-init/callback", json={"hostname": "rack-gamma", "profile": "pi5", "stage": "phone_home", "source_ip": "192.168.174.77"})
        with patch_in("_ping_status", return_value={"reachable": False, "latency_ms": None, "error": "stub"}):
            self.assertEqual(self.client.post("/api/nodes/monitor", json={}).status_code, 200)
        stored = cp._read_node_profiles("rack-gamma")
        self.assertTrue(stored["selected"])
        self.assertIn(stored["selected"], stored["profiles"])

    def test_node_ops_reset_clears_connection_history_and_boot_logs(self) -> None:
        cp.remember_profiles("node-r", [cp.ConnectionProfile("callback", "callback", "192.168.2.20", "callback")], "callback")
        pushed = self.client.post("/api/cloud-init/boot-log", json={"hostname": "node-r", "log": "=== node ===\nnode-r\n"})
        self.assertEqual(pushed.status_code, 200)
        self.assertTrue(self.client.get("/api/nodes/node-r/boot-logs").get_json()["logs"])

        reset = self.client.post("/api/settings/reset", json={"scope": "node_ops"}).get_json()["reset"]
        self.assertGreaterEqual(reset["boot_logs_removed"], 1)
        self.assertEqual(cp._read_node_profiles("node-r").get("profiles"), {})
        self.assertEqual(self.client.get("/api/nodes/node-r/boot-logs").get_json()["logs"], [])

    def test_connection_profiles_endpoint_lists_candidates_and_history(self) -> None:
        self.client.post("/api/cloud-init/callback", json={"hostname": "rack-delta", "profile": "pi5", "stage": "phone_home", "source_ip": "192.168.174.78"})
        payload = self.client.get("/api/nodes/rack-delta/connection-profiles").get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("prefer_tailnet", payload["strategies"])
        self.assertTrue(any(c["address"] == "192.168.174.78" for c in payload["candidates"]))
        self.assertEqual(self.client.get("/api/nodes/missing-node/connection-profiles").status_code, 404)
        self.assertEqual(self.client.get("/api/nodes/.bad/connection-profiles").status_code, 400)
