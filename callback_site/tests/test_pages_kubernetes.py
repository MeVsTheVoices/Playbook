from __future__ import annotations

from unittest.mock import patch

from test_support import BaseCallbackSiteTest, callback_app


class KubernetesPageTests(BaseCallbackSiteTest):
    def test_dashboard_and_pages_render(self) -> None:
        for path in ("/", "/nodes", "/monitor", "/images", "/tailscale-acl", "/settings", "/test-terminal", "/openwrt", "/kubernetes"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, msg=f"expected 200 for {path}")

    def test_sidebar_hides_kubernetes_link_when_runtime_unavailable(self) -> None:
        with patch.object(
            callback_app,
            "_detect_kubernetes_runtime",
            return_value={"available": False, "active_service": "", "reason": "inactive"},
        ):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn('href="/kubernetes"', text)

    def test_sidebar_shows_kubernetes_link_when_runtime_available(self) -> None:
        with patch.object(
            callback_app,
            "_detect_kubernetes_runtime",
            return_value={"available": True, "active_service": "k3s", "reason": "active"},
        ):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('href="/kubernetes"', text)

    def test_kubernetes_summary_endpoint_returns_summary(self) -> None:
        fake_summary = {
            "ok": True,
            "available": True,
            "runtime": {"available": True, "active_service": "k3s"},
            "nodes_total": 3,
            "nodes_ready": 2,
            "control_plane_nodes": 1,
            "pods_total": 18,
            "pods_running": 16,
            "namespaces_total": 6,
            "last_error": "",
        }
        with patch.object(callback_app, "_kubernetes_local_summary", return_value=fake_summary):
            response = self.client.get("/api/kubernetes/summary")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["nodes_total"], 3)
