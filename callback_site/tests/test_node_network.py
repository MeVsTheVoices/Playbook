"""Read-only network facts in Node Ops."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

import services.node_network as network


class NodeNetworkTests(BaseCallbackSiteTest):
    def test_ansible_facts_are_filtered_and_node_connection_is_used(self) -> None:
        facts = {
            "ansible_system": "Linux",
            "ansible_interfaces": ["wlan0", "veth-abc"],
            "ansible_wlan0": {
                "type": "wifi", "active": True, "macaddress": "00:11:22:33:44:55",
                "mtu": 1500, "ssid": "wifi", "password": "do-not-return",
                "ipv4": {"address": "192.0.2.9", "prefix": "24", "broadcast": "192.0.2.255"},
                "ipv6": [{"address": "fe80::1", "prefix": "64", "scope": "link", "secret": "no"}],
            },
            "ansible_veth_abc": {"active": False},
            "ansible_default_ipv4": {"interface": "wlan0", "gateway": "192.0.2.1"},
            "ansible_dns": {"nameservers": ["192.0.2.53"], "search": ["private"]},
            "ansible_unrelated_secret": "do-not-return",
        }
        node = {"name": "alpha", "address": "192.0.2.9", "node_username": "ops", "certificate": "node-key"}

        def run(cmd, **kwargs):
            inventory = json.loads(Path(cmd[2]).read_text())
            host = inventory["all"]["hosts"]["network_target"]
            self.assertEqual(host["ansible_host"], "192.0.2.9")
            self.assertEqual(host["ansible_user"], "ops")
            self.assertTrue(host["ansible_ssh_private_key_file"].endswith("/node-key"))
            self.assertFalse(host["ansible_become"])
            self.assertIn("gather_subset=!all,network", cmd)
            self.assertEqual(kwargs["timeout"], 150)
            return SimpleNamespace(returncode=0, stdout="network_target | SUCCESS => " + json.dumps({"ansible_facts": facts}), stderr="")

        with patch("routes.nodes._node_ops_inventory_view", return_value=[node]), \
             patch.object(network, "_node_ops_inventory_view", return_value=[node]), \
             patch.object(network, "_preferred_node_connect_target", return_value=("192.0.2.9", "inventory")), \
             patch.object(network, "_ssh_private_key_preflight", return_value=""), \
             patch.object(network, "_seed_known_host"), \
             patch.object(network.subprocess, "run", side_effect=run):
            response = self.client.get("/api/nodes/network?node=alpha")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        result = response.get_json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["network"]["interfaces"][0]["ipv4"]["address"], "192.0.2.9")
        self.assertEqual(result["network"]["interfaces"][1]["name"], "veth-abc")
        self.assertEqual(result["network"]["dns_servers"], ["192.0.2.53"])
        self.assertNotIn("do-not-return", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_errors_and_invalid_nodes_do_not_report_success(self) -> None:
        with patch("routes.nodes._node_ops_inventory_view", return_value=[]), \
             patch("routes.nodes.read_node_network") as read:
            self.assertEqual(self.client.get("/api/nodes/network").status_code, 400)
            self.assertEqual(self.client.get("/api/nodes/network?node=missing").status_code, 404)
            read.assert_not_called()
        node = {"name": "alpha", "address": "192.0.2.9"}
        cases = [
            SimpleNamespace(returncode=1, stdout="", stderr="SSH denied"),
            SimpleNamespace(returncode=0, stdout="not json", stderr=""),
            SimpleNamespace(returncode=0, stdout='ok => {"ansible_facts":{"ansible_system":"Linux"}}', stderr=""),
            SimpleNamespace(returncode=0, stdout='ok => {"ansible_facts":{"ansible_system":"FreeBSD","ansible_interfaces":[]}}', stderr=""),
        ]
        with patch.object(network, "_node_ops_inventory_view", return_value=[node]), \
             patch.object(network, "_preferred_node_connect_target", return_value=("192.0.2.9", "inventory")), \
             patch.object(network, "_ssh_private_key_preflight", return_value=""), \
             patch.object(network, "_seed_known_host"), \
             patch.object(network.subprocess, "run", side_effect=cases + [subprocess.TimeoutExpired("ansible", 150)]):
            for _ in range(len(cases) + 1):
                result = network.read_node_network("alpha")
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
                self.assertNotIn("network", result)

    def test_network_view_is_read_only_and_ignores_stale_responses(self) -> None:
        self.browser_probe("/nodes", ["nodes/state.js", "nodes/network.js"], """
const requests = [];
function fetchJsonWithTimeout(url, options) {
  return new Promise(resolve => requests.push({url, options, resolve}));
}
""", """
provisionNode.innerHTML = '<option value="alpha">alpha</option><option value="beta">beta</option>';
document.getElementById("node-network-read").click();
check(requests[0].url.endsWith("node=alpha"), "selected node is queried");
check(requests[0].options.cache === "no-store", "fresh facts requested");
requests[0].resolve({ok: true, json: async () => ({ok: true, target: "alpha", read_at: "now",
  network: {default_ipv4: {gateway: "192.0.2.1"}, dns_servers: ["192.0.2.53"],
    interfaces: [{name: "<img src=x>", active: true, ipv4: {address: "192.0.2.4", prefix: 24}, ipv6: []}]}})});
await tick();
const output = document.getElementById("node-network-result");
check(output.textContent.includes("192.0.2.4/24") && output.textContent.includes("192.0.2.1"), "facts displayed");
check(!output.querySelector("img") && !output.querySelector("input"), "node data is text and view is read-only");
document.getElementById("node-network-read").click();
provisionNode.value = "beta";
resetNodeNetwork();
requests[1].resolve({ok: true, json: async () => ({ok: true, network: {interfaces: [{name: "stale"}]}})});
await tick();
check(!output.textContent.includes("stale"), "old node response ignored");
document.getElementById("node-network-read").click();
requests[2].resolve({ok: true, json: async () => ({ok: false, error: "SSH denied"})});
await tick();
check(document.getElementById("node-network-status").textContent.includes("SSH denied"), "error displayed");
""")

    def test_network_configuration_is_listed_and_stays_read_only(self) -> None:
        self.browser_probe("/nodes", ["nodes/state.js", "nodes/network.js", "nodes/module_config.js"], """
const requests = [];
function fetchJsonWithTimeout(url) {
  requests.push(url);
  return Promise.resolve({ok: true, json: async () => ({ok: true, readable: [], fields: {}})});
}
function renderModulePresets() {}
""", """
nodes = [{name: "alpha", group: "rpi5_active", selected_profiles: ["baseline"]}];
profiles = [{id: "baseline", label: "Baseline", groups: ["*"], tags: ["baseline"], input_specs: []}];
provisionNode.innerHTML = '<option value="alpha">alpha</option>';
renderModuleSection();
const rows = [...document.querySelectorAll("#module-status-body tr")];
const networkRow = rows.find(row => row.textContent.includes("Network Configuration (read-only)"));
check(Boolean(networkRow), "network configuration appears with shared modules");
check(networkRow.textContent.includes("read-only") && networkRow.textContent.includes("Configure"),
  "network facts are marked read-only and configurable");
networkRow.querySelector("button").click();
check(selectedModuleId === "network-facts", "configure selects the network facts view");
check(document.getElementById("node-network").parentElement === moduleDetailEl
  && document.getElementById("node-network").open, "live facts view opens in module detail");
check(document.getElementById("module-save-inputs").closest(".module-actions").hidden
  && document.getElementById("module-status").hidden
  && document.getElementById("module-presets").hidden
  && runAnsible.closest(".module-toggle").hidden, "provisioning and save controls are hidden");
check(requests.length === 0, "selecting network configuration does not start a live-config or write request");
""")
