from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest
import services.kubernetes as kube


def _node(name: str, ready: bool, minutes_down: int = 0, control_plane: bool = False) -> dict:
    changed = datetime.now(timezone.utc) - timedelta(minutes=minutes_down)
    labels = {"node-role.kubernetes.io/control-plane": "true"} if control_plane else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True" if ready else "Unknown",
                    "lastTransitionTime": changed.strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ],
            "addresses": [{"type": "InternalIP", "address": "10.0.0.9"}],
        },
    }


def _fake_kubectl(items: list) -> object:
    return patch.object(kube, "_run_kubectl_json", return_value=({"items": items}, ""))


class KubernetesNodeClearTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        hostname = patch.object(kube.socket, "gethostname", return_value="nucbox")
        hostname.start()
        self.addCleanup(hostname.stop)

    def test_node_row_reports_how_long_a_node_has_been_not_ready(self) -> None:
        row = kube._node_row(_node("beta", ready=False, minutes_down=42))
        self.assertFalse(row["ready"])
        self.assertTrue(41 <= row["not_ready_minutes"] <= 43)
        self.assertIsNone(kube._node_row(_node("alpha", ready=True))["not_ready_minutes"])

    def test_stale_nodes_split_by_state_and_grace_period(self) -> None:
        items = [
            _node("alpha", True),
            _node("beta", False, 90),
            _node("gamma", False, 3, control_plane=True),
            _node("nucbox", False, 500, control_plane=True),
        ]
        with _fake_kubectl(items):
            found = kube._stale_nodes(10)
        self.assertEqual([n["name"] for n in found["candidates"]], ["beta"])
        self.assertEqual([n["name"] for n in found["waiting"]], ["gamma"])
        self.assertEqual([n["name"] for n in found["protected"]], ["nucbox"])
        self.assertEqual([n["name"] for n in found["ready"]], ["alpha"])

    def test_dry_run_never_deletes(self) -> None:
        with _fake_kubectl([_node("beta", False, 90)]), patch.object(kube, "_delete_node") as delete:
            result = kube._clear_stale_nodes(10, dry_run=True)
        delete.assert_not_called()
        self.assertEqual([n["name"] for n in result["would_remove"]], ["beta"])

    def test_clear_deletes_only_eligible_nodes(self) -> None:
        items = [_node("alpha", True), _node("beta", False, 90), _node("nucbox", False, 90)]
        with _fake_kubectl(items), patch.object(kube, "_delete_node", return_value=(True, "ok")) as delete:
            result = kube._clear_stale_nodes(10, dry_run=False)
        delete.assert_called_once_with("beta")
        self.assertEqual(result["removed"], ["beta"])
        self.assertTrue(result["ok"])

    def test_clear_reports_failures(self) -> None:
        with _fake_kubectl([_node("beta", False, 90)]), patch.object(kube, "_delete_node", return_value=(False, "boom")):
            result = kube._clear_stale_nodes(10, dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], [{"name": "beta", "error": "boom"}])

    def test_delete_node_rejects_bad_names_without_running_anything(self) -> None:
        with patch.object(kube.subprocess, "run") as run:
            for bad in ("", "Bad_Name", "-x", "a b", "x;rm -rf /", "../etc"):
                ok, message = kube._delete_node(bad)
                self.assertFalse(ok, msg=bad)
                self.assertEqual(message, "invalid node name")
        run.assert_not_called()

    def test_delete_node_runs_kubectl_delete(self) -> None:
        completed = type("P", (), {"returncode": 0, "stdout": 'node "beta" deleted\n', "stderr": ""})()
        with patch.object(kube.shutil, "which", side_effect=lambda cmd: "/usr/bin/k3s" if cmd == "k3s" else None), patch.object(
            kube.subprocess, "run", return_value=completed
        ) as run:
            ok, message = kube._delete_node("beta")
        self.assertTrue(ok)
        self.assertIn("deleted", message)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/k3s", "kubectl", "delete", "node", "beta"])

    def test_clear_route_defaults_to_dry_run(self) -> None:
        with _fake_kubectl([_node("beta", False, 90)]), patch.object(kube, "_delete_node") as delete:
            response = self.client.post("/api/kubernetes/nodes/clear", json={})
        self.assertEqual(response.status_code, 200)
        delete.assert_not_called()
        self.assertTrue(response.get_json()["dry_run"])

    def test_clear_route_removes_when_not_dry_run(self) -> None:
        with _fake_kubectl([_node("beta", False, 90)]), patch.object(kube, "_delete_node", return_value=(True, "ok")):
            response = self.client.post("/api/kubernetes/nodes/clear", json={"dry_run": False, "min_minutes": 5})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["removed"], ["beta"])

    def test_clear_route_validates_min_minutes(self) -> None:
        for value in ("abc", -1, 999999):
            response = self.client.post("/api/kubernetes/nodes/clear", json={"min_minutes": value})
            self.assertEqual(response.status_code, 400, msg=str(value))

    def test_remove_route_refuses_ready_and_local_nodes_and_unknown(self) -> None:
        items = [_node("alpha", True), _node("nucbox", False, 90), _node("beta", False, 1)]
        with _fake_kubectl(items), patch.object(kube, "_delete_node") as delete:
            ready = self.client.delete("/api/kubernetes/nodes/alpha")
            local = self.client.delete("/api/kubernetes/nodes/nucbox")
            unknown = self.client.delete("/api/kubernetes/nodes/ghost")
            invalid = self.client.delete("/api/kubernetes/nodes/Bad_Name")
        delete.assert_not_called()
        self.assertEqual(ready.status_code, 409)
        self.assertEqual(ready.get_json()["error"], "node is Ready")
        self.assertEqual(local.status_code, 409)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(invalid.status_code, 400)

    def test_remove_route_deletes_recently_down_node_on_explicit_request(self) -> None:
        with _fake_kubectl([_node("beta", False, 1)]), patch.object(kube, "_delete_node", return_value=(True, "ok")) as delete:
            response = self.client.delete("/api/kubernetes/nodes/beta")
        self.assertEqual(response.status_code, 200)
        delete.assert_called_once_with("beta")

    def test_page_has_clear_controls(self) -> None:
        text = self.client.get("/kubernetes").get_data(as_text=True)
        self.assertIn("Clear old nodes", text)
        self.assertIn('id="kube-clear-old-nodes"', text)
        script = (Path(__file__).resolve().parents[1] / "static" / "kubernetes.js").read_text(encoding="utf-8")
        self.assertIn("/api/kubernetes/nodes/clear", script)
        self.assertIn("data-remove-node", script)


if __name__ == "__main__":
    import unittest

    unittest.main()
