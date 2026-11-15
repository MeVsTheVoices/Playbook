from __future__ import annotations

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


class TailscaleOperatorTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        self.commands: list[list[str]] = []
        self.values_seen: list[tuple[str, int]] = []
        self.installed = False
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
