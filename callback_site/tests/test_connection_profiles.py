"""Connection profiles: per-node candidates, replaceable selection strategy and stored history."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.connection_profiles as cp
import services.network as network
import services.storage
from services.network import _tailscale_peer_fqdn


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

    def test_tailnet_fqdn_is_a_profile_and_a_strategy_can_prefer_it(self) -> None:
        with patch_in("_tailscale_peer_ip", ["services.connection_profiles"], return_value="100.100.2.3"), patch_in(
            "_tailscale_peer_fqdn", ["services.connection_profiles"], return_value="rack-beta.tail.ts.net"
        ), patch_in("_name_resolves", ["services.connection_profiles"], return_value=True):
            profiles = cp.gather_profiles("rack-beta", "192.168.174.222", "192.168.174.222")
        by_id = {p.id: p for p in profiles}
        self.assertEqual(by_id["tailnet_fqdn"].address, "rack-beta.tail.ts.net")
        self.assertEqual(by_id["tailnet_fqdn"].source, "tailscale_fqdn")
        self.assertEqual(by_id["tailnet"].fqdn, "rack-beta.tail.ts.net", "the IP profile carries the name HTTPS certificates need")
        self.assertEqual(cp.select_profile(profiles).id, "tailnet")
        self.assertEqual(cp.select_profile(profiles, "prefer_fqdn").id, "tailnet_fqdn")
        with patch.dict("os.environ", {"CALLBACK_CONNECTION_STRATEGY": "prefer_fqdn"}):
            self.assertEqual(cp.select_profile(profiles).address, "rack-beta.tail.ts.net")

    def test_fqdn_this_host_cannot_resolve_is_never_chosen(self) -> None:
        with patch_in("_tailscale_peer_ip", ["services.connection_profiles"], return_value="100.100.2.3"), patch_in(
            "_tailscale_peer_fqdn", ["services.connection_profiles"], return_value="rack-beta.tail.ts.net"
        ), patch_in("_name_resolves", ["services.connection_profiles"], return_value=False):
            profiles = cp.gather_profiles("rack-beta", "192.168.174.222", "192.168.174.222")
        fqdn = next(p for p in profiles if p.id == "tailnet_fqdn")
        self.assertFalse(fqdn.usable)
        self.assertEqual(cp.select_profile(profiles, "prefer_fqdn").id, "tailnet")

    def test_name_resolution_check_is_cached_and_tolerates_failure(self) -> None:
        cp._RESOLVE_CACHE.clear()
        self.addCleanup(cp._RESOLVE_CACHE.clear)
        with patch("services.connection_profiles.socket.getaddrinfo", side_effect=OSError("nxdomain")) as lookup:
            self.assertFalse(cp._name_resolves("nope.tail.ts.net"))
            self.assertFalse(cp._name_resolves("nope.tail.ts.net"))
        self.assertEqual(lookup.call_count, 1)

    def test_tailnet_status_supplies_fqdns_without_the_trailing_dot(self) -> None:
        import services.network as network

        status = json.dumps({"Peer": {"a": {"HostName": "Rack-Beta", "DNSName": "rack-beta.tail.ts.net.", "Online": True, "TailscaleIPs": ["100.100.2.3"]}}})
        self.addCleanup(network._TAILNET_CACHE.update, {"at": -1e9, "peers": {}, "fqdns": {}})
        network._TAILNET_CACHE.update({"at": -1e9, "peers": {}, "fqdns": {}})
        with patch("services.network.subprocess.run", return_value=type("R", (), {"stdout": status})()):
            peers, fqdns = network._load_tailnet()
        self.assertEqual(peers["rack-beta"], "100.100.2.3")
        self.assertEqual(fqdns["rack-beta"], "rack-beta.tail.ts.net")

    def test_tailnet_fqdn_uses_cached_reverse_dns_without_local_tailscale(self) -> None:
        network._TAILNET_REVERSE_CACHE.clear()
        self.addCleanup(network._TAILNET_REVERSE_CACHE.clear)
        with patch.object(network, "_load_tailnet", return_value=({}, {})), patch.object(
            network.socket, "gethostbyaddr", return_value=("Rack-Beta.Tail.ts.net.", [], ["100.100.2.3"])
        ) as reverse:
            self.assertEqual(_tailscale_peer_fqdn("rack-beta", "100.100.2.3"), "rack-beta.tail.ts.net")
            self.assertEqual(_tailscale_peer_fqdn("rack-beta", "100.100.2.3"), "rack-beta.tail.ts.net")
            self.assertEqual(_tailscale_peer_fqdn("rack-beta.tail.ts.net."), "rack-beta.tail.ts.net")
        reverse.assert_called_once_with("100.100.2.3")

    def test_tailnet_reverse_dns_rejects_non_magicdns_names_and_lan_ips(self) -> None:
        network._TAILNET_REVERSE_CACHE.clear()
        self.addCleanup(network._TAILNET_REVERSE_CACHE.clear)
        with patch.object(network, "_load_tailnet", return_value=({}, {})), patch.object(
            network.socket, "gethostbyaddr", return_value=("rack-beta.example.org", [], ["100.100.2.3"])
        ) as reverse:
            self.assertEqual(_tailscale_peer_fqdn("rack-beta", "192.168.1.20"), "")
            reverse.assert_not_called()
            self.assertEqual(_tailscale_peer_fqdn("rack-beta", "100.100.2.3"), "")
        with patch.object(network, "_load_tailnet", return_value=({}, {})), patch.object(
            network.socket, "gethostbyaddr", side_effect=OSError("no PTR record")
        ):
            self.assertEqual(_tailscale_peer_fqdn("rack-beta", "100.100.2.4"), "")

    def test_gather_passes_known_tailnet_ip_to_fqdn_discovery(self) -> None:
        with patch_in("_tailscale_peer_fqdn", ["services.connection_profiles"], return_value="rack-beta.tail.ts.net") as fqdn, \
             patch_in("_name_resolves", ["services.connection_profiles"], return_value=False):
            profiles = cp.gather_profiles("rack-beta", "100.100.2.3", "100.100.2.3")
        fqdn.assert_called_once_with("rack-beta", "100.100.2.3")
        self.assertTrue(all(p.fqdn == "rack-beta.tail.ts.net" for p in profiles if p.tailnet))
        self.assertEqual(cp.select_profile(profiles).id, "callback")
        self.assertFalse(next(p for p in profiles if p.kind == "tailnet_fqdn").usable)

    def test_tailnet_reverse_dns_wait_is_bounded_and_timeout_is_cached(self) -> None:
        network._TAILNET_REVERSE_CACHE.clear()
        self.addCleanup(network._TAILNET_REVERSE_CACHE.clear)
        with patch.object(network, "_load_tailnet", return_value=({}, {})), \
             patch.object(network.threading, "Thread") as thread, \
             self.assertLogs("services.network", level="WARNING") as logs:
            thread.return_value.is_alive.return_value = True
            self.assertEqual(_tailscale_peer_fqdn("rack-beta", "100.100.2.3"), "")
            self.assertEqual(_tailscale_peer_fqdn("rack-beta", "100.100.2.3"), "")
        thread.assert_called_once()
        thread.return_value.join.assert_called_once_with(2.0)
        self.assertIn("timed out", logs.output[0])

    def test_route_candidates_merge_sources_and_keep_selected_profile(self) -> None:
        candidates = [
            cp.ConnectionProfile("callback", "callback", "100.100.2.3", "callback", True),
            cp.ConnectionProfile("inventory", "inventory", "100.100.2.3", "inventory", True),
            cp.ConnectionProfile("tailnet", "tailnet", "100.100.2.3", "tailscale", True),
            cp.ConnectionProfile("tailnet_fqdn", "tailnet_fqdn", "rack-beta.tail.ts.net", "tailscale_fqdn", True, fqdn="rack-beta.tail.ts.net"),
        ]
        with patch("routes.connection_profiles._node_ops_inventory_view", return_value=[{"name": "route-node"}]), \
             patch("routes.connection_profiles.gather_profiles", return_value=candidates):
            payload = self.client.get("/api/nodes/route-node/connection-profiles").json
            with patch("routes.connection_profiles.select_profile", return_value=candidates[1]):
                inventory_selected = self.client.get("/api/nodes/route-node/connection-profiles").json
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual(payload["selected"], "callback")
        self.assertEqual(payload["candidates"][0]["id"], "callback")
        self.assertEqual(payload["candidates"][0]["sources"], ["callback", "inventory", "tailscale"])
        self.assertEqual(payload["candidates"][1]["fqdn"], "rack-beta.tail.ts.net")
        self.assertEqual(inventory_selected["candidates"][0]["id"], inventory_selected["selected"])
        self.assertEqual(inventory_selected["candidates"][0]["sources"], ["callback", "inventory", "tailscale"])

    def test_route_candidates_keep_distinct_ports_and_credentials(self) -> None:
        candidates = [
            cp.ConnectionProfile("callback", "callback", "100.100.2.3", "callback", port=22, user="ops"),
            cp.ConnectionProfile("inventory", "inventory", "100.100.2.3", "inventory", port=2222, user="ops"),
            cp.ConnectionProfile("tailnet", "tailnet", "100.100.2.3", "tailscale", user="admin"),
        ]
        with patch("routes.connection_profiles._node_ops_inventory_view", return_value=[{"name": "route-node"}]), \
             patch("routes.connection_profiles.gather_profiles", return_value=candidates):
            payload = self.client.get("/api/nodes/route-node/connection-profiles").json
        self.assertEqual(len(payload["candidates"]), 3)

    def test_inventory_fqdn_merges_certificate_metadata_without_losing_selection(self) -> None:
        with patch("routes.connection_profiles._node_ops_inventory_view", return_value=[
            {"name": "route-node", "address": "rack-beta.tail.ts.net"}
        ]), patch_in("_tailscale_peer_fqdn", ["services.connection_profiles"], return_value="rack-beta.tail.ts.net"), \
             patch_in("_name_resolves", ["services.connection_profiles"], return_value=True):
            payload = self.client.get("/api/nodes/route-node/connection-profiles").json
        fqdn = next(p for p in payload["candidates"] if p["address"] == "rack-beta.tail.ts.net")
        self.assertEqual(fqdn["sources"], ["inventory", "tailscale_fqdn"])
        self.assertEqual(fqdn["fqdn"], "rack-beta.tail.ts.net")
        self.assertTrue(fqdn["tailnet"])
        self.assertIn(payload["selected"], [p["id"] for p in payload["candidates"]])

    def test_routes_dialog_displays_sources_and_certificate_hostname(self) -> None:
        checks = self.browser_probe(
            "/nodes", ["connection_routes.js"],
            """
window.fetch = async () => ({ok: true, json: async () => ({
  ok: true, strategy: "prefer_tailnet", selected: "callback", candidates: [
    {id: "callback", kind: "callback", source: "callback", sources: ["callback", "inventory"],
     address: "100.100.2.3", tailnet: true, port: 22, usable: true, route: {interface: "eth0"}},
    {id: "tailnet_fqdn", kind: "tailnet_fqdn", source: "tailscale_fqdn",
     address: "rack-beta.tail.ts.net", fqdn: "rack-beta.tail.ts.net", tailnet: true,
     port: 22, usable: false, route: {error: "Hostname route is resolved by SSH at connection time."}}
  ]
})});
""",
            """
await NodeRoutes.open("rack-beta");
const cards = document.querySelectorAll(".route-path");
check(cards.length === 2, "one card per destination");
check(cards[0].textContent.includes("callback, inventory"), "merged provenance shown");
check(cards[0].textContent.includes("Preferred by Node Ops"), "preferred target retained");
check(cards[1].textContent.includes("Tailnet FQDN (MagicDNS)"), "FQDN identified");
check(cards[1].textContent.includes("Certificate hostname: rack-beta.tail.ts.net"), "certificate hostname shown");
check(cards[1].textContent.includes("unverified / transit"), "unresolved FQDN remains visible");
""",
        )
        self.assertEqual(len(checks), 6)

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

    def test_route_details_report_interface_gateway_and_address_family(self) -> None:
        result = subprocess.CompletedProcess([], 0, '[{"dev":"eth0","gateway":"172.18.0.1","prefsrc":"172.18.0.2"}]', "")
        with patch.object(cp.subprocess, "run", return_value=result) as run:
            route = cp.describe_route("192.168.1.20")
        self.assertEqual(route, {"interface": "eth0", "gateway": "172.18.0.1", "source_ip": "172.18.0.2", "family": "IPv4", "error": ""})
        self.assertEqual(run.call_args.args[0], ["ip", "-json", "route", "get", "192.168.1.20"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2)
        with patch.object(cp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '[{"dev":"tailscale0"}]', "")):
            self.assertEqual(cp.describe_route("fd7a:115c:a1e0::1")["family"], "IPv6")

    def test_route_lookup_failure_is_explicit_and_names_are_not_guessed(self) -> None:
        for result in (subprocess.CompletedProcess([], 1, "", "Network unreachable"), subprocess.CompletedProcess([], 0, "bad json", "")):
            with patch.object(cp.subprocess, "run", return_value=result):
                self.assertTrue(cp.describe_route("192.168.1.20")["error"])
        for error in (FileNotFoundError("ip"), subprocess.TimeoutExpired("ip", 2)):
            with patch.object(cp.subprocess, "run", side_effect=error):
                self.assertIn("unavailable", cp.describe_route("192.168.1.20")["error"])
        with patch.object(cp.subprocess, "run") as run:
            self.assertIn("connection time", cp.describe_route("rack-node.local")["error"])
            run.assert_not_called()

    def test_route_enrichment_is_opt_in_and_reuses_duplicate_address_lookup(self) -> None:
        candidates = [
            cp.ConnectionProfile("callback", "callback", "192.168.1.20", "callback"),
            cp.ConnectionProfile("inventory", "inventory", "192.168.1.20", "inventory"),
        ]
        route = {"interface": "eth0", "gateway": "", "source_ip": "", "family": "IPv4", "error": ""}
        with patch("routes.connection_profiles._node_ops_inventory_view", return_value=[{"name": "route-node", "address": "192.168.1.20"}]), \
             patch("routes.connection_profiles.gather_profiles", return_value=candidates), \
             patch("routes.connection_profiles.describe_route", return_value=route) as describe:
            plain = self.client.get("/api/nodes/route-node/connection-profiles").json
            describe.assert_not_called()
            detailed = self.client.get("/api/nodes/route-node/connection-profiles?routes=1").json
            describe.assert_called_once_with("192.168.1.20")
        self.assertNotIn("route", plain["candidates"][0])
        self.assertEqual(len(detailed["candidates"]), 1)
        self.assertEqual(detailed["candidates"][0]["route"], route)
