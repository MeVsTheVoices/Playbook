from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest
import services.openwrt


class OpenWrtProbeTests(BaseCallbackSiteTest):
    def test_openwrt_lives_in_node_ops_and_api_returns_probe_stats(self) -> None:
        old_page = self.client.get("/openwrt")
        self.assertEqual(old_page.status_code, 302)
        self.assertTrue(old_page.headers["Location"].endswith("/nodes#module-configuration"))

        page = self.client.get("/nodes")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn('id="openwrt-host"', text)
        module_config = text[text.index('id="module-configuration"'):text.index('id="user-profile-segments"')]
        self.assertIn('id="openwrt-host" class="openwrt-tools" hidden', module_config, "router tools sit in Module Configuration, hidden until a router is selected")
        self.assertNotIn("openwrt-host-select", text)
        self.assertIn("openwrt-probe", text)
        self.assertIn("openwrt-uci", text)
        self.assertIn("openwrt-push-key", text)
        self.assertNotIn('href="/openwrt"', text)
        self.assertNotIn('href="/openwrt"', self.client.get("/settings").get_data(as_text=True))

        response = self.client.post(
            "/api/openwrt/inspect",
            json={
                "host": "192.168.1.1",
                "port": 22,
                "username": "root",
                "password": "secret",
                "connection": "ssh",
                "become": True,
                "module_hint": "opkg",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("module", payload)
        self.assertIn("stats", payload)
        self.assertIn("inventory_preview", payload)
        self.assertEqual(payload["request"]["host"], "192.168.1.1")
        self.assertNotIn("secret", payload["inventory_preview"])
        self.assertIn('ansible_password: "<redacted>"', payload["inventory_preview"])

    def test_openwrt_push_key_endpoint_reuses_dashboard_keys(self) -> None:
        generated = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "openwrt-test-key", "comment": "openwrt", "mode": "generate"},
        )
        self.assertEqual(generated.status_code, 200)

        # Never run a real ssh here: OpenSSH reads the password from the terminal, so a
        # real attempt against 127.0.0.1 prompts whoever is running the suite.
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=255, stdout="", stderr="stubbed ssh")) as run_mock:
            response = self.client.post(
                "/api/openwrt/push-key",
                json={
                    "host": "127.0.0.1",
                    "port": 22,
                    "username": "root",
                    "password": "secret",
                    "key_name": "openwrt-test-key",
                },
            )
        self.assertTrue(run_mock.called)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("key_name", payload)
        self.assertEqual(payload["key_name"], "openwrt-test-key")

    def test_probe_ssh_target_can_use_password_pipe_without_sshpass(self) -> None:
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="login ok\n", stderr="")) as run_mock:
            result = services.openwrt._probe_ssh_target(host="router", port=22, username="root", password="secret")

        self.assertTrue(result["ok"])
        self.assertEqual(result["details"], "login ok")
        self.assertEqual(run_mock.call_args.kwargs["input"], "secret\n")
        command = run_mock.call_args.args[0]
        self.assertEqual(command[command.index("--") + 1:], ["root@router", "true"])

    def test_openwrt_endpoints_reject_option_like_targets_and_key_paths(self) -> None:
        generated = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "openwrt-reject-key", "comment": "openwrt", "mode": "generate"},
        )
        self.assertIn(generated.status_code, (200, 409))
        with patch("subprocess.run") as run_mock:
            for path, body in (
                ("/api/openwrt/inspect", {"host": "192.168.1.1", "username": "-oProxyCommand=id"}),
                ("/api/openwrt/inspect", {"host": "-oProxyCommand=id", "username": "root"}),
                ("/api/openwrt/push-key", {"host": "192.168.1.1", "username": "-oProxyCommand=id", "key_name": "openwrt-reject-key"}),
                ("/api/openwrt/push-key", {"host": "192.168.1.1", "username": "root", "key_name": "../../etc/passwd"}),
            ):
                with self.subTest(path=path, body=body):
                    response = self.client.post(path, json=body)
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.get_json()["ok"])
        run_mock.assert_not_called()


class OpenWrtUciTests(BaseCallbackSiteTest):
    ROUTER = {"name": "router-a", "group": "openwrt", "address": "192.0.2.1", "node_username": "root", "certificate": "openwrt-uci-key"}

    def setUp(self) -> None:
        super().setUp()
        generated = self.client.post("/api/ssh/generate", json={"key_name": "openwrt-uci-key", "comment": "openwrt", "mode": "generate"})
        self.assertIn(generated.status_code, (200, 409))

    def _read(self, nodes, body, stdout="", returncode=0):
        with patch("services.node_inventory._node_ops_inventory_view", return_value=nodes), \
             patch("services.network._preferred_node_connect_target", return_value=("192.0.2.1", "inventory")), \
             patch("services.openwrt._seed_known_host"), \
             patch("subprocess.run", return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr="denied")) as run_mock:
            response = self.client.post("/api/openwrt/uci", json=body)
        return response, run_mock

    def test_uci_summary_keeps_only_allowlisted_values(self) -> None:
        stdout = "\n".join([
            "release=OpenWrt 23.05.5",
            "system.@system[0].hostname=GL-A1300",
            "network.lan.ipaddr=192.168.8.1",
            "network.wan.proto=dhcp",
            "dhcp.lan.ignore=",
            "wireless.default_radio0.ssid='rack-net'",
            "wireless.default_radio0.key='do-not-return'",
            "wireless.default_radio0.encryption=sae",
            "garbage line",
        ])
        response, run_mock = self._read([self.ROUTER], {"node": "router-a", "port": 22}, stdout)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        facts = {fact["key"]: fact["value"] for fact in payload["facts"]}
        self.assertEqual(facts["system.@system[0].hostname"], "GL-A1300")
        self.assertEqual(facts["wireless.default_radio0.ssid"], "rack-net")
        self.assertNotIn("dhcp.lan.ignore", facts, "empty values are omitted")
        self.assertNotIn("do-not-return", response.get_data(as_text=True))
        self.assertNotIn("wireless.default_radio0.encryption", facts)

        command = run_mock.call_args.args[0]
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(command[command.index("--") + 1:], ["root@192.0.2.1", services.openwrt.UCI_SUMMARY_SCRIPT])
        self.assertNotIn("input", run_mock.call_args.kwargs, "UCI reads never send a password")

    def test_uci_is_only_read_from_openwrt_nodes(self) -> None:
        pi = {**self.ROUTER, "name": "pi-a", "group": "rpi5_active"}
        for nodes, body in (
            ([pi], {"node": "pi-a"}),
            ([self.ROUTER], {"node": "missing"}),
            ([self.ROUTER], {"node": "router-a", "port": 70000}),
        ):
            with self.subTest(body=body):
                response, run_mock = self._read(nodes, body)
                self.assertEqual(response.status_code, 400)
                run_mock.assert_not_called()

    def test_uci_failure_reports_ssh_error(self) -> None:
        response, _ = self._read([self.ROUTER], {"node": "router-a"}, "", returncode=255)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["facts"], [])
        self.assertIn("denied", payload["error"])


class OpenWrtUciScriptTests(BaseCallbackSiteTest):
    def test_wireless_filter_is_a_valid_posix_bracket_expression(self) -> None:
        # BusyBox grep reads "\]" inside brackets as the end of the set, which silently matched nothing.
        self.assertIn("[][A-Za-z0-9_@-]", services.openwrt.UCI_SUMMARY_SCRIPT)
        self.assertNotIn("\\[\\]", services.openwrt.UCI_SUMMARY_SCRIPT)
