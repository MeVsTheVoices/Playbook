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
        self.assertTrue(old_page.headers["Location"].endswith("/nodes#openwrt-host"))

        page = self.client.get("/nodes")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn('id="openwrt-host"', text)
        self.assertIn("openwrt-host-select", text)
        self.assertIn("openwrt-probe", text)
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
