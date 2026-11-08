from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

from services import helm, kubernetes


class HelmConfigurationTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = patch.object(helm, "CONFIG_FILE", Path(directory.name) / "configurations.json")
        storage.start()
        self.addCleanup(storage.stop)
        checkout = patch.object(helm, "_checkout", side_effect=self.checkout)
        checkout.start()
        self.addCleanup(checkout.stop)
        self.commands = []
        commands = patch.object(helm, "_command", side_effect=self.command)
        commands.start()
        self.addCleanup(commands.stop)

    def checkout(self, repository, root, commit=""):
        chart = root / "charts" / "hello-world"
        chart.mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: hello-world\nversion: 0.1.0\n")
        return commit or "a" * 40

    def command(self, args, **kwargs):
        self.commands.append(args)
        if args[:3] == ["helm", "show", "chart"]:
            return "name: hello-world\nversion: 0.1.0\ntype: application\n"
        if args[:3] == ["helm", "show", "values"]:
            return "replicaCount: 1\n"
        if args[:2] == ["helm", "template"]:
            return "apiVersion: v1\nkind: Service\n"
        if args[0] == "kubectl":
            return json.dumps({"current-context": "docker-desktop", "clusters": [{"cluster": {"server": "https://example:6443"}}]})
        if args[:3] == ["helm", "upgrade", "--install"]:
            return '{"version": 1}'
        self.fail(f"Unexpected command: {args}")

    def imported(self):
        response = self.client.post("/api/helm/configurations/import", json={"repository": helm.EXAMPLES[0]["repository"]})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["configuration"]

    def test_import_defaults_pins_source_and_does_not_deploy(self):
        record = self.imported()
        self.assertEqual(record["values"], "replicaCount: 1\n")
        self.assertEqual(record["defaults"], record["values"])
        self.assertEqual(record["commit"], "a" * 40)
        self.assertIsNone(record["last_submission"])
        self.assertFalse(any("upgrade" in args for args in self.commands))
        saved = self.client.get("/api/helm/configurations").json
        self.assertEqual(saved["configurations"], [record])
        self.assertEqual(saved["examples"], helm.EXAMPLES)

    def test_save_reopen_and_preview_keeps_imported_defaults(self):
        record = self.imported()
        response = self.client.put(f"/api/helm/configurations/{record['id']}", json={
            "name": "Two replicas", "release": record["release"], "namespace": "playbook-helm",
            "values": "replicaCount: 2\n", "revision": 1, "repository": "https://github.com/evil/replacement",
        })
        self.assertEqual(response.status_code, 200, response.json)
        saved = self.client.get("/api/helm/configurations").json["configurations"][0]
        self.assertEqual(saved["values"], "replicaCount: 2\n")
        self.assertEqual(saved["defaults"], "replicaCount: 1\n")
        self.assertEqual(saved["repository"], record["repository"])
        preview = self.client.post(f"/api/helm/configurations/{record['id']}/preview", json={"revision": 2})
        self.assertEqual(preview.status_code, 200, preview.json)
        self.assertIn("kind: Service", preview.json["manifest"])
        self.assertFalse(any("upgrade" in args for args in self.commands))

    def test_rejects_invalid_repository_and_chart_traversal(self):
        for repository in ("http://github.com/helm/examples", "https://user@github.com/helm/examples", "https://github.com/helm/examples?x=1", "https://example.com/helm/examples"):
            response = self.client.post("/api/helm/configurations/import", json={"repository": repository})
            self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/helm/configurations/import", json={"repository": helm.EXAMPLES[0]["repository"], "chart_path": "../outside"})
        self.assertEqual(response.status_code, 400)

    def test_missing_chart_is_explicit(self):
        with patch.object(helm, "_checkout", side_effect=lambda repo, root, commit="": root.mkdir() or "a" * 40):
            response = self.client.post("/api/helm/configurations/import", json={"repository": helm.EXAMPLES[0]["repository"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("no Helm charts", response.json["error"])

    def test_multiple_charts_require_an_explicit_path(self):
        def multiple(repository, root, commit=""):
            self.checkout(repository, root, commit)
            other = root / "charts" / "other"
            other.mkdir()
            (other / "Chart.yaml").write_text("name: other\n")
            return "a" * 40
        with patch.object(helm, "_checkout", side_effect=multiple):
            response = self.client.post("/api/helm/configurations/import", json={"repository": helm.EXAMPLES[0]["repository"]})
            self.assertEqual(response.status_code, 400)
            self.assertIn("charts/hello-world", response.json["error"])
            self.assertIn("charts/other", response.json["error"])
            response = self.client.post("/api/helm/configurations/import", json={
                "repository": helm.EXAMPLES[0]["repository"], "chart_path": "charts/hello-world",
            })
            self.assertEqual(response.status_code, 200, response.json)

    def test_invalid_values_and_stale_revision_do_not_overwrite(self):
        record = self.imported()
        payload = {"name": "Test", "release": record["release"], "namespace": "playbook-helm", "revision": 1}
        for values in ("- a list", "a: [", "x" * 262145):
            response = self.client.put(f"/api/helm/configurations/{record['id']}", json={**payload, "values": values})
            self.assertEqual(response.status_code, 400)
        stale = self.client.post(f"/api/helm/configurations/{record['id']}/preview", json={"revision": 0})
        self.assertEqual(stale.status_code, 400)
        self.assertEqual(helm.configurations()[0], record)

    def test_deploy_requires_confirmation_and_retains_submitted_values(self):
        record = self.imported()
        path = f"/api/helm/configurations/{record['id']}/deploy"
        response = self.client.post(path, json={"revision": 1})
        self.assertEqual(response.status_code, 400)
        with patch.object(helm, "_helm_connection", return_value=[]):
            response = self.client.post(path, json={"revision": 1, "confirm": True})
        self.assertEqual(response.status_code, 200, response.json)
        saved = helm.configurations()[0]
        self.assertEqual(saved["last_submission"]["status"], "deployed")
        self.assertEqual(saved["last_submission"]["context"], "docker-desktop")
        self.assertEqual(saved["last_submission"]["values"], saved["values"])
        self.assertTrue(any("--wait" in args for args in self.commands))
        response = self.client.put(f"/api/helm/configurations/{record['id']}", json={
            "name": saved["name"], "release": saved["release"], "namespace": saved["namespace"],
            "revision": saved["revision"], "values": "replicaCount: 2\n",
        })
        self.assertEqual(response.status_code, 200, response.json)
        edited = helm.configurations()[0]
        self.assertEqual(edited["values"], "replicaCount: 2\n")
        self.assertEqual(edited["last_submission"]["values"], "replicaCount: 1\n")

    def test_failed_deploy_is_saved_as_failed_not_success(self):
        record = self.imported()
        def fail_upgrade(args, **kwargs):
            if args[:3] == ["helm", "upgrade", "--install"]:
                raise RuntimeError("cluster unavailable")
            return self.command(args, **kwargs)
        with patch.object(helm, "_helm_connection", return_value=[]), patch.object(helm, "_command", side_effect=fail_upgrade):
            response = self.client.post(f"/api/helm/configurations/{record['id']}/deploy", json={"revision": 1, "confirm": True})
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.json["ok"])
        self.assertEqual(helm.configurations()[0]["last_submission"]["status"], "failed")

    def test_corrupt_storage_does_not_silently_reset(self):
        helm.CONFIG_FILE.write_text("broken JSON")
        response = self.client.get("/api/helm/configurations")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(helm.CONFIG_FILE.read_text(), "broken JSON")

    def test_dashboard_has_configuration_controls(self):
        html = self.client.get("/kubernetes").get_data(as_text=True)
        for control in ("helm-selection", "helm-values", "helm-save", "helm-preview", "helm-deploy", "helm-submission"):
            self.assertIn(control, html)


class KubernetesClientRuntimeTests(BaseCallbackSiteTest):
    def test_configured_client_is_detected_without_k3s(self):
        with patch.object(kubernetes, "_has_client_kubeconfig", return_value=True), \
             patch.object(kubernetes, "_service_is_active", return_value=False), \
             patch.object(kubernetes.shutil, "which", side_effect=lambda name: "/bin/kubectl" if name == "kubectl" else None), \
             patch.object(kubernetes.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, '{"gitVersion":"v1.36.1"}', "")):
            runtime = kubernetes._detect_kubernetes_runtime(force_refresh=True)
        self.assertTrue(runtime["available"])
        self.assertEqual(runtime["active_service"], "kubectl")

    def test_client_authentication_error_is_not_hidden(self):
        with patch.object(kubernetes, "_has_client_kubeconfig", return_value=True), \
             patch.object(kubernetes, "_service_is_active", return_value=True), \
             patch.object(kubernetes.shutil, "which", return_value="/bin/tool"), \
             patch.object(kubernetes.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "Unauthorized")):
            runtime = kubernetes._detect_kubernetes_runtime(force_refresh=True)
        self.assertFalse(runtime["available"])
        self.assertEqual(runtime["reason"], "Unauthorized")

    def test_explicit_client_does_not_fall_back_to_other_cluster(self):
        with patch.object(kubernetes, "_has_client_kubeconfig", return_value=True), \
             patch.object(kubernetes.shutil, "which", side_effect=lambda name: f"/bin/{name}"), \
             patch.object(kubernetes.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "Unauthorized")) as run:
            _, error = kubernetes._run_kubectl_json(["get", "nodes"])
            self.assertIn("Unauthorized", error)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "/bin/kubectl")
