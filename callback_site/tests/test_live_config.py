"""Live module reads: the remote scripts, output filtering and the endpoint (never a real ssh)."""

from __future__ import annotations

import base64
import re
import subprocess
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest

import services.live_config as live


class RemoteScriptTests(BaseCallbackSiteTest):
    def test_every_reader_is_valid_bash(self) -> None:
        for module_id in live.supported_modules():
            proc = subprocess.run(["bash", "-n"], input=live.build_script(module_id), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{module_id}: {proc.stderr}")

    def test_scripts_only_read(self) -> None:
        forbidden = re.compile(r"\b(rm|mv|cp|tee|chmod|chown|apt(-get)?|dd|mkfs\S*|mount|kill)\b|systemctl (start|stop|restart|enable|disable)|>\s*/etc|>>")
        for module_id in live.supported_modules():
            for line in live.build_script(module_id).splitlines():
                stripped = re.sub(r"[0-9]?>\s*/dev/null|2>&1", "", line)
                self.assertIsNone(forbidden.search(stripped), f"{module_id}: {line}")

    def test_script_travels_as_an_argument_not_on_stdin(self) -> None:
        wrapped = live._wrap_for_ssh("echo hi\n")
        encoded = re.search(r"printf %s '([A-Za-z0-9+/=]+)'", wrapped).group(1)
        self.assertEqual(base64.b64decode(encoded).decode(), "echo hi\n")
        self.assertTrue(wrapped.startswith('bash -c "$('))

    def test_no_reader_looks_for_a_secret(self) -> None:
        for module_id in live.supported_modules():
            script = live.build_script(module_id)
            for secret in ("auth_key", "server_token", "influx_access_key", "influx_secret_key", "token ="):
                self.assertNotIn(secret, script, f"{module_id} reads {secret}")


class ParseTests(BaseCallbackSiteTest):
    def test_keeps_known_fields_and_drops_secrets_and_strangers(self) -> None:
        text = "hostname=alpha\nauth_key=tskey-123\nadvertise_tags=tag:rpi5,tag:homelab\nnonsense=1\nenable_ssh=true\nnot a pair\n"
        parsed = live.parse_live_output(text, "tailscale", "rpi5_active")
        self.assertEqual(parsed, {"hostname": "alpha", "advertise_tags": "tag:rpi5,tag:homelab", "enable_ssh": "true"})

    def test_a_secret_field_is_dropped_even_when_the_node_prints_it(self) -> None:
        parsed = live.parse_live_output("server_token=K10abc\ninflux_access_key=x\nserver_endpoint=https://100.65.24.76:6443\n", "kubernetes", "rpi5_active")
        self.assertEqual(parsed, {"server_endpoint": "https://100.65.24.76:6443"})
        self.assertEqual(live.parse_live_output("influx_access_key=x\ninflux_org=home\n", "telegraf", "rpi5_active"), {"influx_org": "home"})

    def test_checkbox_values_are_normalised(self) -> None:
        parsed = live.parse_live_output("enable_fail2ban=true\nenable_unattended_upgrades=false\n", "baseline", "rpi4_active")
        self.assertEqual(parsed, {"enable_fail2ban": "true", "enable_unattended_upgrades": "false"})

    def test_worker_is_reported_as_light_worker_on_a_3bplus(self) -> None:
        self.assertEqual(live.parse_live_output("node_role=worker\n", "kubernetes", "rpi_3bplus_active"), {"node_role": "light-worker"})
        self.assertEqual(live.parse_live_output("node_role=worker\n", "kubernetes", "rpi5_active"), {"node_role": "worker"})

    def test_a_select_value_the_form_does_not_offer_is_dropped(self) -> None:
        self.assertEqual(live.parse_live_output("node_role=banana\nk3s_ingress=nginx\n", "kubernetes", "rpi5_active"), {})

    def test_readable_ids_are_the_fields_each_script_can_print(self) -> None:
        kube = live.readable_ids("kubernetes")
        self.assertLessEqual({"node_role", "server_endpoint", "k3s_taint", "k3s_ingress"}, kube)
        self.assertNotIn("k3s_config", kube)
        self.assertNotIn("server_token", kube)
        self.assertLessEqual({"hostname", "enable_ssh", "node_ip", "tailnet"}, live.readable_ids("tailscale"))
        self.assertEqual(live.readable_ids("docker"), {"docker_data_root", "registry_mirror"})
        self.assertEqual(live.readable_ids("nothing"), set())


class LiveConfigEndpointTests(BaseCallbackSiteTest):
    def setUp(self) -> None:
        super().setUp()
        self.node = "live-config-node"
        response = self.client.post("/api/nodes/manual-nodes", json={"name": self.node, "group": "rpi5_active", "certificate": "test-cert"})
        self.assertEqual(response.status_code, 200)
        self.addCleanup(self.client.delete, f"/api/nodes/manual-nodes/{self.node}")

    def test_returns_the_fields_read_from_the_node(self) -> None:
        run = {"stdout": "timezone=Europe/London\nenable_fail2ban=true\nauth_key=nope\n", "target": "ops@10.0.0.5 (inventory)", "error": ""}
        with patch.object(live, "_run_remote_script", return_value=run) as remote:
            response = self.client.get(f"/api/nodes/live-config?node={self.node}&module=baseline")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["fields"], {"timezone": "Europe/London", "enable_fail2ban": "true"})
        self.assertIn("timezone", body["readable"])
        self.assertNotIn("auth_key", body["readable"])
        self.assertEqual(body["target"], "ops@10.0.0.5 (inventory)")
        self.assertEqual(remote.call_args.args[0], self.node)

    def test_an_unreachable_node_is_reported_in_the_body(self) -> None:
        run = {"stdout": "", "target": "ops@10.0.0.5 (inventory)", "error": "Connection timed out"}
        with patch.object(live, "_run_remote_script", return_value=run):
            response = self.client.get(f"/api/nodes/live-config?node={self.node}&module=tailscale")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "Connection timed out")
        self.assertEqual(body["fields"], {})

    def test_rejects_bad_requests_without_touching_the_node(self) -> None:
        with patch.object(live, "_run_remote_script") as remote:
            self.assertEqual(self.client.get("/api/nodes/live-config").status_code, 400)
            self.assertEqual(self.client.get(f"/api/nodes/live-config?node={self.node}&module=nope").status_code, 400)
            self.assertEqual(self.client.get("/api/nodes/live-config?node=ghost&module=baseline").status_code, 404)
        remote.assert_not_called()

    def test_a_module_with_no_reader_is_ok_and_empty(self) -> None:
        with patch.object(live, "_run_remote_script") as remote:
            result = live.read_live_config(self.node, "not-a-reader")
        self.assertTrue(result["ok"])
        self.assertEqual((result["fields"], result["readable"]), ({}, []))
        remote.assert_not_called()

    def test_a_missing_ssh_key_is_reported(self) -> None:
        with patch.object(live, "_ssh_private_key_preflight", return_value="SSH private key not found: /x"), \
                patch.object(live, "_node_ops_inventory_view", return_value=[{"name": self.node, "address": "10.0.0.5"}]), \
                patch.object(live, "_preferred_node_connect_target", return_value=("10.0.0.5", "inventory")), \
                patch.object(live, "_seed_known_host"), \
                patch.object(subprocess, "run") as run:
            result = live._run_remote_script(self.node, "echo hi")
        self.assertEqual(result["error"], "SSH private key not found: /x")
        run.assert_not_called()


class LiveConfigPageTests(BaseCallbackSiteTest):
    def test_page_reads_live_on_selection_and_ships_the_new_form_layout(self) -> None:
        text = self.page_text(self.client.get("/nodes"))
        self.assertIn("/api/nodes/live-config", text)
        self.assertIn("loadLiveConfig", text)
        self.assertIn("module-row-selected", text)
        self.assertIn(".module-fields", text)
        self.assertIn(".module-options-section", text)
        self.assertIn("Use this", text)
        self.assertIn('class="module-detail collapsed"', text)
        self.assertIn('classList.remove("collapsed")', text)
        self.assertIn("module-expand", text)
        self.assertNotIn("style.color = \"#b45309\"", text)
