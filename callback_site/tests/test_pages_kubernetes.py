from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, callback_app
except ImportError:
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
            "jobs_total": 2,
            "jobs_running": 1,
            "longhorn_namespace_present": True,
            "longhorn_pods_total": 3,
            "longhorn_pods_running": 3,
            "longhorn_ready": True,
            "job_rows": [{"namespace": "default", "name": "refresh-secrets", "status": "Running", "active": 1, "succeeded": 0, "failed": 0}],
            "last_error": "",
        }
        with patch.object(callback_app, "_kubernetes_local_summary", return_value=fake_summary):
            response = self.client.get("/api/kubernetes/summary")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["nodes_total"], 3)
        self.assertEqual(payload["jobs_running"], 1)
        self.assertTrue(payload["longhorn_ready"])

    def test_kubernetes_page_renders_dense_dashboard_sections(self) -> None:
        response = self.client.get("/kubernetes")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Runtime Details", text)
        self.assertIn("Cluster Snapshot", text)
        self.assertIn("Nodes", text)
        self.assertIn("Namespaces", text)
        self.assertIn("Pods", text)
        self.assertIn("Active Jobs", text)
        self.assertIn("Snapshot Notes", text)
        self.assertIn("Raw JSON snapshot", text)
        self.assertIn("Longhorn", text)

    def test_kubernetes_page_refreshes_summary_without_cache(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "templates" / "kubernetes.html").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/kubernetes/summary", { cache: "no-store" })', text)
        self.assertIn('window.addEventListener("focus", loadKubernetesSummary);', text)
        self.assertIn('document.addEventListener("visibilitychange"', text)
