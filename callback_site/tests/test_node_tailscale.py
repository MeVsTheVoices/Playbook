"""Node-local Tailscale snapshots and the on-demand Node Ops dialog."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

import services.node_tailscale as tailscale


class NodeTailscaleTests(BaseCallbackSiteTest):
    def request_snapshot(self, stdout: str = "", error: str = ""):
        with patch("routes.nodes._node_ops_inventory_view", return_value=[{"name": "alpha"}]), \
                patch.object(tailscale, "_run_remote_script", return_value={
                    "stdout": stdout, "error": error, "target": "ops@192.0.2.1 (inventory)",
                }) as remote:
            response = self.client.get("/api/nodes/tailscale?node=alpha")
        remote.assert_called_once_with("alpha", tailscale.STATUS_SCRIPT)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.get_json()

    def test_snapshot_is_from_the_node_and_filters_sensitive_fields(self) -> None:
        peer = {
            "HostName": "beta", "Online": False, "Relay": "sea",
            "LastSeen": "2026-01-01T00:00:00Z", "RxBytes": 123,
            "PrimaryRoutes": ["10.0.0.0/24"], "PublicKey": "do-not-return",
        }
        status = {
            "BackendState": "Running", "Version": "1.90.0",
            "Self": {"HostName": "alpha", "TailscaleIPs": ["100.64.0.1"], "UserID": 42},
            "Peer": {"public-key": peer}, "Health": ["Test health warning"],
            "AuthURL": "do-not-return", "User": {"42": {"LoginName": "do-not-return"}},
            "ExitNodeStatus": {"Online": True},
        }
        body = self.request_snapshot(json.dumps(status))
        self.assertTrue(body["ok"])
        self.assertEqual(body["node"], "alpha")
        self.assertEqual(body["target"], "ops@192.0.2.1 (inventory)")
        self.assertIn("read_at", body)
        self.assertEqual(body["status"]["Peer"][0]["RxBytes"], 123)
        self.assertEqual(body["status"]["Peer"][0]["PrimaryRoutes"], ["10.0.0.0/24"])
        self.assertEqual(body["status"]["Health"], ["Test health warning"])
        self.assertNotIn("do-not-return", json.dumps(body))
        self.assertNotIn("UserID", body["status"]["Self"])

    def test_stopped_or_logged_out_daemon_is_still_a_valid_snapshot(self) -> None:
        for state in ("Stopped", "NeedsLogin"):
            body = self.request_snapshot(json.dumps({"BackendState": state, "Self": None, "Peer": None}))
            self.assertTrue(body["ok"])
            self.assertEqual(body["status"]["BackendState"], state)
            self.assertEqual(body["status"]["Peer"], [])
            self.assertEqual(body["status"]["Self"], {})

    def test_ssh_and_cli_failures_are_explicit(self) -> None:
        for error in ("Connection timed out", "SSH private key not found", "Tailscale is not installed", "Access denied"):
            body = self.request_snapshot(error=error)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], error)
            self.assertNotIn("status", body)

    def test_invalid_or_unexpected_output_is_not_success(self) -> None:
        for output in ("", "not json", "[]", "{}", '{"BackendState": "Running", "Peer": []}',
                       '{"BackendState": "Running", "Peer": {"key": null}}'):
            body = self.request_snapshot(output)
            self.assertFalse(body["ok"], output)
            self.assertIn("Tailscale status", body["error"])

    def test_unknown_missing_or_archived_nodes_do_not_trigger_ssh(self) -> None:
        with patch("routes.nodes._node_ops_inventory_view", return_value=[]), \
                patch.object(tailscale, "_run_remote_script") as remote:
            self.assertEqual(self.client.get("/api/nodes/tailscale").status_code, 400)
            self.assertEqual(self.client.get("/api/nodes/tailscale?node=retired").status_code, 404)
        remote.assert_not_called()

    def test_fixed_script_only_asks_for_status(self) -> None:
        result = subprocess.run(["bash", "-n"], input=tailscale.STATUS_SCRIPT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tailscale status --json", tailscale.STATUS_SCRIPT)
        self.assertNotIn("sudo", tailscale.STATUS_SCRIPT)
        self.assertNotIn("debug prefs", tailscale.STATUS_SCRIPT)

    def test_remote_script_reports_missing_cli_and_daemon_failure(self) -> None:
        missing = subprocess.run(
            ["/bin/bash"], input=tailscale.STATUS_SCRIPT,
            env={"PATH": "/nonexistent"}, capture_output=True, text=True,
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Tailscale is not installed", missing.stderr)
        failed = subprocess.run(
            ["/bin/bash"],
            input='tailscale() { echo "Cannot connect to tailscaled" >&2; return 1; }\n' + tailscale.STATUS_SCRIPT,
            capture_output=True, text=True,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Cannot connect to tailscaled", failed.stderr)
        self.assertEqual(failed.stdout, "")

    def test_dialog_queries_refreshes_reports_errors_and_ignores_stale_responses(self) -> None:
        checks = self.browser_probe(
            "/nodes",
            ["nodes/tailscale.js", "nodes/state.js", "nodes/node_table.js"],
            """
const requests = [];
window.fetch = (url, options) => new Promise(resolve => requests.push({url, options, resolve}));
function reply(index, data, ok = true) {
  requests[index].resolve({ok, json: async () => data});
}
window.escapeHtml = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
window.profileById = () => null;
""",
            """
nodes = [{name: "alpha", selected_profiles: []}];
renderRows();
document.querySelector('[data-action="tailscale"]').click();
const dialog = document.querySelector('[aria-labelledby="node-tailscale-title"]');
const refresh = dialog.querySelector("[data-refresh]");
check(dialog.open && requests.length === 1, "row action opens dialog and asks the node");
check(requests[0].url.endsWith("?node=alpha"), "selected node is queried");
check(requests[0].options.cache === "no-store", "fresh read requested");
check(refresh.disabled, "refresh disabled while reading");
reply(0, {ok: true, target: "ops@alpha", read_at: "now", status: {
  BackendState: "Running", Self: {HostName: "alpha"}, Health: ["health warning"],
  Peer: [{HostName: "<img src=x onerror=alert(1)>", Online: false, Relay: "sea"}]
}});
await tick();
check(dialog.textContent.includes("health warning"), "health warnings shown");
check(dialog.textContent.includes("Offline") && dialog.textContent.includes("sea"), "peer state and relay shown");
check(!dialog.querySelector("img"), "node output is text, never HTML");
check(dialog.querySelector("pre").textContent.includes("BackendState"), "filtered JSON available");
refresh.click();
check(requests.length === 2, "refresh asks node again");
reply(1, {ok: false, error: "SSH denied"});
await tick();
check(dialog.textContent.includes("SSH denied"), "SSH failure visible");
check(!dialog.querySelector("pre"), "old snapshot removed on failure");
refresh.click();
dialog.close();
NodeTailscale.open("beta");
reply(3, {ok: true, target: "ops@beta", read_at: "later", status: {
  BackendState: "Stopped", Self: {}, Peer: []
}});
await tick();
reply(2, {ok: false, error: "stale failure"});
await tick();
check(dialog.textContent.includes("Stopped") && !dialog.textContent.includes("stale failure"), "stale response cannot replace current node");
check(!refresh.disabled, "refresh enabled after completion");
dialog.close();
""",
        )
        self.assertGreaterEqual(len(checks), 12)
