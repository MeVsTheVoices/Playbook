from __future__ import annotations

import json
import base64
import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

from services import helm, helm_postrender, kubernetes, tailscale_operator


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

    def bundled(self):
        def commands(args, **kwargs):
            if args[:3] == ["helm", "show", "chart"]:
                return (helm.BUNDLED_CHARTS["dockerbot"] / "Chart.yaml").read_text()
            if args[:3] == ["helm", "show", "values"]:
                return (helm.BUNDLED_CHARTS["dockerbot"] / "values.yaml").read_text()
            return self.command(args, **kwargs)
        with patch.object(helm, "_command", side_effect=commands):
            response = self.client.post("/api/helm/configurations/import", json={"bundled": "dockerbot"})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["configuration"]

    def test_bundled_chart_snapshots_guide_and_detects_changes(self):
        record = self.bundled()
        self.assertEqual(record["bundled"], "dockerbot")
        self.assertIn("Reset Token", record["guide"])
        self.assertIn("dockerbot", [item["id"] for item in self.client.get("/api/helm/configurations").json["bundled"]])
        snapshot = helm._snapshot_path(record["id"])
        self.assertEqual(helm._chart_digest(snapshot), record["commit"])
        with patch.object(helm, "_checkout", side_effect=AssertionError("bundled chart must not fetch Git")):
            response = self.client.post(f"/api/helm/configurations/{record['id']}/preview", json={"revision": 1})
        self.assertEqual(response.status_code, 200, response.json)
        (snapshot / "values.yaml").write_text("changed: true\n")
        response = self.client.post(f"/api/helm/configurations/{record['id']}/preview", json={"revision": 1})
        self.assertEqual(response.status_code, 502)
        self.assertIn("snapshot changed", response.json["error"])

    def test_imported_chart_guide_and_missing_guide(self):
        record = self.imported()
        self.assertEqual(record["guide"], "")
        def with_guide(repository, root, commit=""):
            result = self.checkout(repository, root, commit)
            chart = root / "charts" / "hello-world"
            (chart / "README.md").write_text("README")
            (chart / "PLAYBOOK_GUIDE.md").write_text("<script>not executable</script>")
            return result
        with patch.object(helm, "_checkout", side_effect=with_guide):
            record = self.imported()
        self.assertEqual(record["guide"], "<script>not executable</script>")

    def test_dockerbot_token_uses_stdin_and_is_not_persisted(self):
        record = self.bundled()
        token = "test-token-value"
        path = f"/api/helm/configurations/{record['id']}/dockerbot-token"
        response = self.client.post(path, json={"revision": 1, "token": token})
        self.assertEqual(response.status_code, 400)
        with patch.object(helm, "_helm_connection", return_value=[]), \
             patch.object(helm.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "secret/dockerbot-token", "")) as run:
            response = self.client.post(path, json={"revision": 1, "token": token, "confirm": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn(token, response.get_data(as_text=True))
        self.assertNotIn(token, helm.CONFIG_FILE.read_text())
        args, kwargs = run.call_args
        self.assertNotIn(token, str(args))
        manifest = json.loads(kwargs["input"])
        self.assertEqual(manifest["metadata"], {"name": "dockerbot-token", "namespace": "playbook-helm"})
        self.assertEqual(base64.b64decode(manifest["data"]["token"]).decode(), token)
        self.assertIn("--server-side", args[0])

    def test_token_rejects_stale_untrusted_and_hides_subprocess_errors(self):
        record = self.bundled()
        path = f"/api/helm/configurations/{record['id']}/dockerbot-token"
        for payload in (
            {"revision": 0, "token": "token", "confirm": True},
            {"revision": 1, "token": "bad token", "confirm": True},
        ):
            self.assertEqual(self.client.post(path, json=payload).status_code, 400)
        with patch.object(helm, "_helm_connection", return_value=[]), \
             patch.object(helm.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "sensitive-token")):
            response = self.client.post(path, json={"revision": 1, "token": "sensitive-token", "confirm": True})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sensitive-token", response.get_data(as_text=True))
        other = self.imported()
        response = self.client.post(f"/api/helm/configurations/{other['id']}/dockerbot-token",
                                    json={"revision": 1, "token": "token", "confirm": True})
        self.assertEqual(response.status_code, 400)
        anonymous = self.client.application.test_client(authenticated=False)
        response = anonymous.post(path, json={"revision": 1, "token": "token", "confirm": True})
        self.assertEqual(response.status_code, 401)

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
        for control in ("helm-selection", "helm-panel", "helm-values", "helm-save", "helm-preview", "helm-deploy", "helm-submission", "helm-ts-mode", "helm-guide", "helm-dockerbot-token"):
            self.assertIn(control, html)
        self.assertIn('<details id="helm-panel" class="tailnet-api-panel">', html)
        self.assertLess(html.index('id="helm-selection"'), html.index('id="helm-panel"'))

    def test_helm_editor_expands_when_selection_changes_in_browser(self):
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for the Helm accordion browser test")
        root = Path(__file__).resolve().parents[1]
        page = self.client.get("/kubernetes").get_data(as_text=True)
        page = re.sub(r'<script\b[^>]*\bsrc=[^>]*>.*?</script>', "", page, flags=re.DOTALL)
        page = re.sub(r"<link\b[^>]*>", "", page)
        record = {"id": "saved", "name": "Saved", "release": "app", "namespace": "apps", "values": "{}",
                  "repository": "https://github.com/example/chart", "chart_path": "chart", "chart_name": "app",
                  "chart_version": "1", "commit": "abc", "last_submission": None}
        script = """
window.fetch = async (url) => ({ok: true, json: async () => url.includes("tailscale-operator")
  ? {ok: true, status: {installed: true, ready_replicas: 1}}
  : {ok: true, configurations: [%s], examples: [%s]}});
""" % (json.dumps(record), json.dumps(helm.EXAMPLES[0])) + (root / "static/helm.js").read_text() + """
setTimeout(() => {
  const panel = document.getElementById("helm-panel");
  const select = document.getElementById("helm-selection");
  const result = {initial: panel.open};
  select.value = "example:0"; select.dispatchEvent(new Event("change"));
  result.example = panel.open;
  panel.open = false;
  select.value = "saved"; select.dispatchEvent(new Event("change"));
  result.saved = panel.open;
  result.editor = !document.getElementById("helm-editor").hidden;
  document.body.innerHTML = '<pre id="helm-result"></pre>';
  document.getElementById("helm-result").textContent = JSON.stringify(result);
}, 100);
"""
        page = page.replace("</body>", f"<script>{script}</script></body>")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "page.html"
            target.write_text(page)
            result = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
                 f"--user-data-dir={directory}/chrome", "--virtual-time-budget=3000", "--dump-dom", target.as_uri()],
                text=True, capture_output=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="helm-result">(.*?)</pre>', result.stdout, re.DOTALL)
        self.assertIsNotNone(match, result.stderr)
        self.assertEqual(json.loads(html.unescape(match.group(1))), {"initial": False, "example": True, "saved": True, "editor": True})

    def test_browser_bundled_guide_and_token_submission(self):
        record = {"id": "saved", "name": "Dockerbot", "release": "bot", "namespace": "apps",
                  "values": "bot: {}", "repository": "", "chart_path": "charts/dockerbot",
                  "chart_name": "dockerbot", "chart_version": "1", "commit": "abc", "revision": 1,
                  "bundled": "dockerbot", "guide": "<img src=x onerror=alert(1)>", "last_submission": None}
        bootstrap = """
let submitted;
window.confirm = () => true;
window.fetch = async (url, options) => {
  if (url.includes("tailscale-operator")) return {ok: true, json: async () => ({ok: true, status: {}})};
  if (url.endsWith("/dockerbot-token")) {
    submitted = JSON.parse(options.body);
    return {ok: false, json: async () => ({ok: false, error: "Secret rejected"})};
  }
  return {ok: true, json: async () => ({ok: true, configurations: [%s], examples: [], bundled: [{id: "dockerbot", name: "Dockerbot"}]})};
};
""" % json.dumps(record)
        probe = """
await tick();
const select = document.getElementById("helm-selection");
check([...select.options].some(option => option.value === "bundled:dockerbot"), "bundled chart listed");
select.value = "bundled:dockerbot"; select.dispatchEvent(new Event("change"));
check(document.getElementById("helm-repository-fields").hidden, "bundled chart hides repository inputs");
check(document.getElementById("helm-status").textContent.includes("Import defaults"), "bundled import instruction shown");
select.value = "saved"; select.dispatchEvent(new Event("change"));
check(document.getElementById("helm-guide").value === "<img src=x onerror=alert(1)>", "guide displayed as text");
check(!document.getElementById("helm-dockerbot-token-panel").hidden, "token panel displayed");
const token = document.getElementById("helm-dockerbot-token");
token.value = "test-token";
document.getElementById("helm-dockerbot-token-save").click();
await tick();
check(submitted.token === "test-token" && submitted.confirm, "token submitted with confirmation");
check(token.value === "", "token cleared after failure");
check(document.getElementById("helm-status").textContent === "Secret rejected", "failure displayed");
document.getElementById("helm-values").dispatchEvent(new Event("input"));
check(document.getElementById("helm-dockerbot-token-save").disabled, "unsaved values block token submission");
"""
        self.browser_probe("/kubernetes", ["helm.js"], bootstrap, probe)

    def test_tailscale_settings_are_saved_and_checked_before_deployment(self):
        record = self.imported()
        exposure = {"mode": "https", "service": "web", "hostname": "n8n", "port": "5678", "tags": "tag:k8s"}
        response = self.client.put(f"/api/helm/configurations/{record['id']}", json={
            "name": record["name"], "release": record["release"], "namespace": record["namespace"],
            "values": record["values"], "revision": 1, "tailscale": exposure,
        })
        self.assertEqual(response.status_code, 200, response.json)
        saved = response.json["configuration"]
        self.assertEqual(saved["tailscale"]["hostname"], "n8n")
        with patch.object(tailscale_operator, "operator_status", return_value={"installed": False, "error": ""}):
            response = self.client.post(f"/api/helm/configurations/{record['id']}/deploy", json={"revision": 2, "confirm": True})
        self.assertEqual(response.status_code, 502)
        self.assertIn("installed and ready", response.json["error"])
        self.assertFalse(any(args[:3] == ["helm", "upgrade", "--install"] for args in self.commands))
        with patch.object(tailscale_operator, "operator_status", return_value={"installed": True, "ready_replicas": 1, "error": ""}), \
             patch.object(helm, "_helm_connection", return_value=[]):
            response = self.client.post(f"/api/helm/configurations/{record['id']}/deploy", json={"revision": 2, "confirm": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["configuration"]["last_submission"]["tailscale"], saved["tailscale"])
        self.assertTrue(any("--post-renderer" in args for args in self.commands if args[:2] == ["helm", "template"]))

    def test_rejects_bad_tailscale_configurations(self):
        for exposure in (
            {"mode": "https", "service": "web", "hostname": "web", "port": "0"},
            {"mode": "service", "service": "../web", "hostname": "web"},
            {"mode": "service", "service": "web", "hostname": "web", "tags": "bad"},
            {"mode": "https", "service": "web", "hostname": "web", "port": "80", "proxy_group": "ha", "tags": "tag:k8s"},
        ):
            with self.subTest(exposure=exposure), self.assertRaises(ValueError):
                helm._tailscale(exposure)

    def test_locked_chart_dependencies_use_isolated_repository_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory)
            (chart / "Chart.yaml").write_text("dependencies:\n- name: valkey\n  version: 1.0.0\n")
            (chart / "Chart.lock").write_text("dependencies:\n- name: valkey\n  version: 1.0.0\n  repository: https://example.org/charts\n")
            with patch.object(helm, "_command", return_value="") as command:
                helm._prepare_dependencies(chart)
            self.assertEqual(command.call_count, 2)
            self.assertEqual(command.call_args_list[0].args[0][:3], ["helm", "repo", "add"])
            self.assertIn("--repository-config", command.call_args_list[1].args[0])

    def test_unlocked_unvendored_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory)
            (chart / "Chart.yaml").write_text("dependencies:\n- name: missing\n  version: 1.0.0\n")
            with self.assertRaisesRegex(ValueError, "Chart.lock"):
                helm._prepare_dependencies(chart)


class HelmTailscaleRendererTests(BaseCallbackSiteTest):
    SERVICE = """apiVersion: v1
kind: Service
metadata:
  name: app
spec:
  ports:
  - port: 5678
"""

    def config(self, **overrides):
        return {**helm._tailscale({"mode": "https", "service": "app", "hostname": "n8n", "port": "5678"}),
                "namespace": "apps", "release": "demo", **overrides}

    def test_https_ingress_has_valid_backend_and_operator_annotations(self):
        result = list(yaml.safe_load_all(helm_postrender.render(self.SERVICE, self.config(proxy_class="custom", tags="tag:k8s"))))
        ingress = result[1]
        self.assertEqual(ingress["metadata"]["annotations"], {"tailscale.com/proxy-class": "custom", "tailscale.com/tags": "tag:k8s"})
        self.assertEqual(ingress["spec"]["tls"], [{"hosts": ["n8n"]}])
        self.assertEqual(ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"], {"name": "app", "port": {"number": 5678}})

    def test_service_exposure_and_proxy_group(self):
        standalone = list(yaml.safe_load_all(helm_postrender.render(self.SERVICE, self.config(mode="service"))))[0]
        self.assertEqual(standalone["metadata"]["annotations"]["tailscale.com/expose"], "true")
        grouped = list(yaml.safe_load_all(helm_postrender.render(self.SERVICE, self.config(mode="service", proxy_group="ha"))))[0]
        self.assertEqual(grouped["spec"]["loadBalancerClass"], "tailscale")
        self.assertEqual(grouped["metadata"]["annotations"]["tailscale.com/proxy-group"], "ha")
        self.assertNotIn("tailscale.com/expose", grouped["metadata"]["annotations"])

    def test_missing_service_wrong_port_and_conflicting_annotations_fail(self):
        for text, config in (
            (self.SERVICE, self.config(service="missing")),
            (self.SERVICE, self.config(port="80")),
            (self.SERVICE.replace("  name: app", "  name: app\n  annotations:\n    tailscale.com/hostname: other"), self.config(mode="service")),
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                helm_postrender.render(text, config)

    def test_missing_service_explains_how_to_select_or_disable_exposure(self):
        with self.assertRaisesRegex(ValueError, "Rendered Services: app.*exposure to Off"):
            helm_postrender.render(self.SERVICE, self.config(service="missing"))
        with self.assertRaisesRegex(ValueError, "renders no Services.*leave exposure Off"):
            helm_postrender.render("apiVersion: apps/v1\nkind: Deployment\n", self.config(service="missing"))
        other_namespace = self.SERVICE.replace("  name: app", "  name: app\n  namespace: other")
        with self.assertRaisesRegex(ValueError, "renders no Services"):
            helm_postrender.render(other_namespace, self.config(service="missing"))


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
