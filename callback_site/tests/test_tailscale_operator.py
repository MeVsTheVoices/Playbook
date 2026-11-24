from __future__ import annotations

import base64
import html as html_lib
import json
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

from services import helm, tailscale_operator
from tailscale_policy import build_policy

SECRET = "tskey-client-kExampleCNTRL-abcdefgh"  # gitleaks:allow (fake test value)
MACHINE_KEY = "privkey:fake-machine-key-for-tests"  # gitleaks:allow (fake test value)


def _b64(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return base64.b64encode(text.encode()).decode()


class TailscaleOperatorTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        self.commands: list[list[str]] = []
        self.values_seen: list[tuple[str, int]] = []
        self.installed = False
        self.flannel = {"nuc": "100.65.24.76", "pi": "100.107.115.85"}
        self.logged_in = True
        self.tailnet_name = "rack-k8s.example.ts.net"
        self.logs = '{"level":"info","msg":"API server proxy in auth mode is listening on :443"}\n{"level":"info","msg":"Startup complete, operator running"}\n'
        for name, value in (("_command", self.command), ("_helm_connection", lambda: [])):
            patcher = patch.object(helm, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, args, **kwargs):
        self.commands.append(list(args))
        if args[:2] == ["helm", "list"]:
            release = [{"name": "tailscale-operator", "status": "deployed", "chart": "tailscale-operator-1.90.0", "app_version": "v1.90.0"}]
            return json.dumps(release if self.installed else [])
        if args[:3] == ["helm", "get", "values"]:
            return json.dumps({"apiServerProxyConfig": {"mode": "true"}, "operatorConfig": {"hostname": "rack-k8s"}, "oauth": {"clientSecret": SECRET}})
        if "deployment" in args:
            return json.dumps({"spec": {"replicas": 1}, "status": {"readyReplicas": 1}})
        if "pods" in args:
            pod = {"metadata": {"name": "operator-abc", "creationTimestamp": "2026-11-15T01:25:39Z"}, "spec": {"nodeName": "pi"},
                   "status": {"phase": "Running", "startTime": "2026-11-15T01:25:39Z", "containerStatuses": [{"ready": True, "restartCount": 2}]}}
            return json.dumps({"items": [pod]})
        if "secret" in args:
            data = {"_machinekey": _b64(MACHINE_KEY)}
            if self.logged_in:
                data.update({"_current-profile": _b64("profile-8215"), "rack-k8s.example.ts.net.crt": _b64("cert"),
                             "_profiles": _b64({"8215": {"Name": self.tailnet_name, "NodeID": "nTEST", "PrivateNodeKey": MACHINE_KEY}})})
            return json.dumps({"data": data})
        if "logs" in args:
            return self.logs
        if "nodes" in args:
            return json.dumps({"items": [{"metadata": {"name": name, "annotations": {"flannel.alpha.coreos.com/public-ip": ip}}} for name, ip in self.flannel.items()]})
        if "clusterrolebinding" in args:
            raise RuntimeError("kubectl failed: NotFound")
        if args[:3] == ["helm", "upgrade", "--install"]:
            path = Path(args[args.index("--values") + 1])
            self.values_seen.append((path.read_text(), stat.S_IMODE(path.stat().st_mode)))
            self.installed = True
            return json.dumps({"version": 1})
        if "apply" in args:
            return "created"
        raise AssertionError(f"unexpected command {args}")

    def post(self, body):
        return self.client.post("/api/kubernetes/tailscale-operator", json=body)

    def test_status_reports_proxy_without_secrets(self) -> None:
        self.installed = True
        response = self.client.get("/api/kubernetes/tailscale-operator")
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        status = payload["status"]
        self.assertEqual((status["proxy_mode"], status["hostname"], status["ready_replicas"]), ("auth", "rack-k8s", 1))
        self.assertEqual(status["kubeconfig_command"], "tailscale configure kubeconfig rack-k8s")
        self.assertTrue(status["proxy_url"].startswith("https://rack-k8s."))
        self.assertFalse(status["readers_binding"])
        self.assertNotIn(SECRET, response.get_data(as_text=True))

    def test_status_connects_by_the_suffixed_name_when_another_cluster_holds_the_hostname(self) -> None:
        self.installed = True
        status = tailscale_operator.operator_status()
        self.assertFalse(status["hostname_taken"])
        self.tailnet_name = "rack-k8s-1.example.ts.net"
        status = tailscale_operator.operator_status()
        self.assertTrue(status["hostname_taken"])
        self.assertEqual(status["tailnet_hostname"], "rack-k8s-1")
        self.assertEqual(status["kubeconfig_command"], "tailscale configure kubeconfig rack-k8s-1")

    def test_live_checks_report_a_working_operator_without_keys(self) -> None:
        self.installed = True
        response = self.client.get("/api/kubernetes/tailscale-operator")
        live = response.get_json()["status"]["live"]
        self.assertTrue(live["healthy"], live["verdict"])
        self.assertEqual((live["pod"]["node"], live["pod"]["restarts"], live["pod"]["ready"]), ("pi", 2, True))
        self.assertEqual((live["tailnet"]["name"], live["tailnet"]["node_id"], live["tailnet"]["certificate"]), ("rack-k8s.example.ts.net", "nTEST", True))
        self.assertTrue(live["logs"]["startup_complete"] and live["logs"]["proxy_listening"])
        self.assertTrue(live["network"]["ok"])
        self.assertNotIn(MACHINE_KEY, response.get_data(as_text=True))

    def test_live_checks_name_the_first_problem(self) -> None:
        self.installed = True
        self.flannel["pi"] = "192.168.8.249"
        live = tailscale_operator.operator_status()["live"]
        self.assertFalse(live["healthy"])
        self.assertIn("Flannel", live["verdict"])
        self.assertEqual([row["tailscale"] for row in live["network"]["nodes"]], [True, False])
        self.flannel["pi"] = "100.107.115.85"
        self.logged_in = False
        self.assertIn("not joined the tailnet", tailscale_operator.operator_status()["live"]["verdict"])
        self.logged_in = True
        self.logs += '{"level":"error","msg":"lookup kubernetes.default.svc on 10.43.0.10:53: i/o timeout"}\n'
        live = tailscale_operator.operator_status()["live"]
        self.assertIn("timeouts", live["verdict"])
        self.assertEqual(live["logs"]["recent_errors"], 1)

    def test_first_install_writes_private_values_and_enables_proxy(self) -> None:
        response = self.post({"confirm": True, "client_id": "kExampleCNTRL", "client_secret": SECRET, "readers_binding": True})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertNotIn(SECRET, response.get_data(as_text=True))
        text, mode = self.values_seen[0]
        self.assertEqual(mode, 0o600)
        self.assertIn("mode: 'true'", text)
        self.assertIn("hostname: tailscale-operator", text)
        install = next(cmd for cmd in self.commands if cmd[:3] == ["helm", "upgrade", "--install"])
        self.assertNotIn(SECRET, " ".join(install), "the secret only travels in the values file")
        self.assertIn("https://pkgs.tailscale.com/helmcharts", install)
        self.assertNotIn("--reset-then-reuse-values", install)
        self.assertTrue(any("apply" in cmd for cmd in self.commands), "read-only binding applied")

    def test_upgrade_can_reuse_installed_credentials(self) -> None:
        self.installed = True
        response = self.post({"confirm": True, "hostname": "rack-k8s", "proxy_mode": "noauth"})
        self.assertEqual(response.status_code, 200, response.get_json())
        text, _ = self.values_seen[0]
        self.assertNotIn("clientId", text)
        self.assertIn("mode: noauth", text)
        install = next(cmd for cmd in self.commands if cmd[:3] == ["helm", "upgrade", "--install"])
        self.assertIn("--reset-then-reuse-values", install)

    def test_rejects_unconfirmed_or_invalid_requests(self) -> None:
        for body in (
            {"client_id": "kExampleCNTRL", "client_secret": SECRET},
            {"confirm": True},
            {"confirm": True, "client_id": "kExampleCNTRL"},
            {"confirm": True, "client_id": "kExampleCNTRL", "client_secret": "not-a-secret"},
            {"confirm": True, "client_id": "kExampleCNTRL", "client_secret": SECRET, "hostname": "--set=x"},
            {"confirm": True, "client_id": "kExampleCNTRL", "client_secret": SECRET, "proxy_mode": "open"},
        ):
            with self.subTest(body={k: v for k, v in body.items() if k != "client_secret"}):
                self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(any(cmd[:3] == ["helm", "upgrade", "--install"] for cmd in self.commands))

    def test_kubernetes_page_has_operator_card(self) -> None:
        text = self.client.get("/kubernetes").get_data(as_text=True)
        self.assertIn('id="tailnet-api"', text)
        self.assertIn("tailnet_api.js", text)
        self.assertIn('id="tailnet-api-client-secret" type="password"', text)
        self.assertIn('id="tailnet-api-live" name="tailnet-api-panels"', text)
        self.assertIn('id="tailnet-api-setup" name="tailnet-api-panels"', text)

    def _accordion_in_browser(self, installed: bool) -> dict:
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for the accordion browser test")
        root = Path(__file__).resolve().parents[1]
        page = self.client.get("/kubernetes").get_data(as_text=True)
        page = re.sub(r'<script\b[^>]*\bsrc=[^>]*>.*?</script>', "", page, flags=re.DOTALL)
        page = re.sub(r"<link\b[^>]*>", "", page)
        live = {"healthy": True, "verdict": "Working", "pod": {"found": True, "ready": True, "name": "op", "restarts": 0},
                "tailnet": {"logged_in": True, "name": "op.ts.net", "node_id": "n1"}, "logs": {"startup_complete": True},
                "network": {"ok": True, "nodes": []}, "checked_at": "2026-11-15T00:00:00+00:00"}
        status = {"installed": installed, "hostname": "op", "replicas": 1, "ready_replicas": 1, "readers_binding": False,
                  **({"live": live} if installed else {})}
        probe = """
const stub = %s;
window.fetch = async () => ({ok: true, json: async () => ({ok: true, status: stub})});
const settle = () => new Promise((resolve) => setTimeout(resolve, 50));
const state = () => ({live: document.getElementById("tailnet-api-live").open, setup: document.getElementById("tailnet-api-setup").open,
  title: document.getElementById("tailnet-api-setup-title").textContent});
""" % json.dumps(status) + (root / "static/tailnet_api.js").read_text() + """
(async () => {
  const out = {};
  await settle();
  out.initial = state();
  const other = out.initial.live ? "tailnet-api-setup" : "tailnet-api-live";
  document.getElementById(other).open = true;
  await settle();
  out.afterUser = state();
  document.getElementById("tailnet-api-refresh").click();
  await settle();
  out.afterRefresh = state();
  document.body.innerHTML = '<pre id="accordion-result"></pre>';
  document.getElementById("accordion-result").textContent = JSON.stringify(out);
})();
"""
        page = page.replace("</body>", f"<script>{probe}</script></body>")
        with tempfile.TemporaryDirectory(prefix="tailnet-accordion-") as tmp:
            target = Path(tmp) / "page.html"
            target.write_text(page)
            result = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
                 f"--user-data-dir={tmp}/chrome", "--virtual-time-budget=3000", "--dump-dom", target.as_uri()],
                text=True, capture_output=True, timeout=30,
            )
        match = re.search(r'<pre id="accordion-result">(.*?)</pre>', result.stdout, re.DOTALL)
        self.assertIsNotNone(match, result.stdout[-2000:] + result.stderr[-2000:])
        return json.loads(html_lib.unescape(match.group(1)))

    def test_live_checks_open_when_they_come_back(self) -> None:
        result = self._accordion_in_browser(installed=True)
        self.assertEqual((result["initial"]["live"], result["initial"]["setup"]), (True, False))
        self.assertEqual(result["initial"]["title"], "Reconfigure / upgrade operator")
        self.assertEqual((result["afterUser"]["live"], result["afterUser"]["setup"]), (False, True), "one panel at a time")
        self.assertEqual((result["afterRefresh"]["live"], result["afterRefresh"]["setup"]), (False, True), "refresh keeps the user's choice")

    def test_install_opens_when_there_is_nothing_live(self) -> None:
        result = self._accordion_in_browser(installed=False)
        self.assertEqual((result["initial"]["live"], result["initial"]["setup"]), (False, True))
        self.assertEqual(result["initial"]["title"], "Install operator")
        self.assertEqual((result["afterUser"]["live"], result["afterUser"]["setup"]), (True, False))


class TailscaleOperatorPolicyTests(BaseCallbackSiteTest):
    BASE = {"admin": "group:admins", "admin_members": "admin@example.com", "homelab_tag": "tag:homelab"}

    def test_operator_tags_and_api_grant(self) -> None:
        result = build_policy({**self.BASE, "k8s_operator": True})
        self.assertTrue(result["ok"], result["errors"])
        policy = result["policy"]
        self.assertEqual(policy["tagOwners"]["tag:k8s-operator"], ["group:admins"])
        self.assertEqual(policy["tagOwners"]["tag:k8s"], ["tag:k8s-operator"])
        grant = next(g for g in policy["grants"] if g["dst"] == ["tag:k8s-operator"])
        self.assertEqual(grant["ip"], ["tcp:443"])
        self.assertEqual(grant["app"]["tailscale.com/cap/kubernetes"][0]["impersonate"]["groups"], ["system:masters"])
        self.assertIn({"src": "group:admins", "proto": "tcp", "accept": ["tag:k8s-operator:443"]}, policy["tests"])

    def test_read_only_access_and_off_by_default(self) -> None:
        view = build_policy({**self.BASE, "k8s_operator": True, "k8s_api_role": "view"})["policy"]
        grant = next(g for g in view["grants"] if g["dst"] == ["tag:k8s-operator"])
        self.assertEqual(grant["app"]["tailscale.com/cap/kubernetes"][0]["impersonate"]["groups"], ["tailnet-readers"])
        self.assertNotIn("tag:k8s-operator", build_policy(self.BASE)["policy"]["tagOwners"])
        self.assertFalse(build_policy({**self.BASE, "k8s_operator": True, "k8s_api_role": "root"})["ok"])
        self.assertFalse(build_policy({**self.BASE, "k8s_operator": True, "homelab_tag": "tag:k8s"})["ok"])


class KubernetesRbacBindingTests(BaseCallbackSiteTest):
    URL = "/api/kubernetes/rbac/bindings"

    def setUp(self) -> None:
        super().setUp()
        self.commands: list[list[str]] = []
        self.applied: list[dict] = []
        self.objects = {
            "hand-made": {"metadata": {"name": "hand-made"}, "roleRef": {"name": "cluster-admin"}, "subjects": []},
        }
        for name, value in (("_command", self.command), ("_helm_connection", lambda: [])):
            patcher = patch.object(helm, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def command(self, args, **kwargs):
        self.commands.append(list(args))
        if "apply" in args:
            manifest = json.loads(Path(args[args.index("-f") + 1]).read_text())
            self.applied.append(manifest)
            self.objects[manifest["metadata"]["name"]] = manifest
            return "configured"
        if "delete" in args:
            del self.objects[args[-1]]
            return "deleted"
        if "clusterrolebindings" in args:
            managed = [o for o in self.objects.values() if o["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") == "playbook"]
            return json.dumps({"items": managed})
        if "clusterrolebinding" in args:
            return json.dumps(self.objects.get(args[args.index("clusterrolebinding") + 1], {})) if args[args.index("clusterrolebinding") + 1] in self.objects else ""
        raise AssertionError(f"unexpected command {args}")

    def put(self, name, body):
        return self.client.put(f"{self.URL}/{name}", json={"confirm": True, **body})

    def test_create_read_update_delete_homelab_controller_admin(self) -> None:
        subjects = [{"kind": "Group", "name": "tag:homelab-controller"}]
        created = self.put("tailscale-homelab-controller-admin", {"role": "cluster-admin", "subjects": subjects})
        self.assertEqual(created.status_code, 200, created.get_json())
        self.assertTrue(created.get_json()["created"])
        self.assertEqual(self.applied[-1], {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "tailscale-homelab-controller-admin", "labels": {"app.kubernetes.io/managed-by": "playbook"}},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": "tag:homelab-controller"}],
        })

        listed = self.client.get(self.URL).get_json()["bindings"]
        self.assertEqual([(b["name"], b["role"], b["subjects"]) for b in listed],
                         [("tailscale-homelab-controller-admin", "cluster-admin", subjects)])

        self.commands.clear()
        updated = self.put("tailscale-homelab-controller-admin", {"role": "view", "subjects": subjects + [{"kind": "User", "name": "alice@example.com"}]})
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertFalse(updated.get_json()["created"])
        self.assertTrue(any("delete" in c for c in self.commands), "roleRef is immutable, so a role change recreates the binding")
        self.assertEqual(self.objects["tailscale-homelab-controller-admin"]["roleRef"]["name"], "view")

        deleted = self.client.delete(f"{self.URL}/tailscale-homelab-controller-admin", json={"confirm": True})
        self.assertEqual(deleted.status_code, 200)
        self.assertNotIn("tailscale-homelab-controller-admin", self.objects)

    def test_unmanaged_bindings_are_never_changed(self) -> None:
        body = {"role": "view", "subjects": [{"kind": "Group", "name": "tailnet-readers"}]}
        self.assertEqual(self.put("hand-made", body).status_code, 409)
        self.assertEqual(self.client.delete(f"{self.URL}/hand-made", json={"confirm": True}).status_code, 409)
        self.assertEqual(self.client.delete(f"{self.URL}/missing", json={"confirm": True}).status_code, 404)
        self.assertFalse(any("apply" in c or "delete" in c for c in self.commands))

    def test_rejects_unsafe_input_before_touching_the_cluster(self) -> None:
        group = [{"kind": "Group", "name": "tag:homelab-controller"}]
        for name, body in (
            ("ok-name", {"role": "cluster-admin", "subjects": group, "confirm": False}),
            ("ok-name", {"role": "system:node", "subjects": group}),
            ("ok-name", {"role": "view", "subjects": []}),
            ("ok-name", {"role": "view", "subjects": [{"kind": "ServiceAccount", "name": "default"}]}),
            ("ok-name", {"role": "view", "subjects": [{"kind": "Group", "name": "system:masters"}]}),
            ("ok-name", {"role": "view", "subjects": [{"kind": "User", "name": "bad name"}]}),
            ("Bad_Name", {"role": "view", "subjects": group}),
            ("system-admin", {"role": "view", "subjects": group}),
        ):
            with self.subTest(name=name, body=body):
                response = self.client.put(f"{self.URL}/{name}", json={"confirm": True, **body})
                self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(self.commands, [])
