from __future__ import annotations

import base64
import json
import stat
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
                             "_profiles": _b64({"8215": {"Name": "rack-k8s.example.ts.net", "NodeID": "nTEST", "PrivateNodeKey": MACHINE_KEY}})})
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
        self.assertIn('id="tailnet-api-live"', text)


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
