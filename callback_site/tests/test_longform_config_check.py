from __future__ import annotations

import json
from unittest.mock import patch

import routes.sd
import services.node_runs
import services.sd_prepare

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

VALID_K3S_CONFIG = 'server: "https://100.65.24.76:6443"\ntoken: join-token\nnode-label:\n  - "role=control-plane"\n'
BAD_YAML = "server: [unclosed\nnode-label: - x\n"


class LongformConfigCheckTests(BaseCallbackSiteTest):
    def check(self, text: str, kind: str = "k3s_config"):
        return self.client.post("/api/config/check", json={"kind": kind, "text": text})

    def manual_kubernetes_node(self, name: str) -> str:
        created = self.client.post(
            "/api/nodes/manual-nodes",
            json={"name": name, "group": "manual", "address": "192.0.2.55", "username": "jdizzle", "certificate": "rack-bootstrap-ed25519"},
        )
        self.assertEqual(created.status_code, 200)
        self.addCleanup(self.client.delete, f"/api/nodes/manual-nodes/{name}")
        configured = self.client.post("/api/nodes/node-configs", json={"node": name, "profile_ids": ["kubernetes"]})
        self.assertEqual(configured.status_code, 200)
        return name

    def test_valid_k3s_config_passes(self) -> None:
        response = self.check(VALID_K3S_CONFIG)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["errors"], [])

    def test_empty_k3s_config_passes_because_defaults_apply(self) -> None:
        self.assertTrue(self.check("").get_json()["ok"])

    def test_yaml_syntax_error_reports_line(self) -> None:
        data = self.check(BAD_YAML).get_json()
        self.assertFalse(data["ok"])
        self.assertIn("line", data["errors"][0])

    def test_non_mapping_is_rejected(self) -> None:
        self.assertFalse(self.check("- just\n- a list\n").get_json()["ok"])

    def test_server_must_be_https_host_port(self) -> None:
        self.assertFalse(self.check("server: 100.65.24.76\n").get_json()["ok"])
        self.assertTrue(self.check("server: https://[fd00::1]:6443\n").get_json()["ok"])

    def test_reserved_node_role_label_is_rejected(self) -> None:
        data = self.check('node-label:\n  - "node-role.kubernetes.io/control-plane=true"\n').get_json()
        self.assertFalse(data["ok"])

    def test_secrets_encryption_on_warns_without_failing(self) -> None:
        data = self.check("secrets-encryption: true\n").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["warnings"]), 1)

    def test_cluster_init_on_a_joining_node_is_an_error(self) -> None:
        data = self.check("server: https://100.65.24.76:6443\ntoken: x\ncluster-init: true\n").get_json()
        self.assertFalse(data["ok"])
        self.assertIn("cluster-init", data["errors"][0])
        # A first server has no 'server' line, so cluster-init is legitimate there.
        self.assertTrue(self.check("cluster-init: true\n").get_json()["ok"])

    def test_a_joining_node_without_a_token_is_warned(self) -> None:
        data = self.check("server: https://100.65.24.76:6443\n").get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(any("token" in w for w in data["warnings"]))
        self.assertEqual(self.check(VALID_K3S_CONFIG).get_json()["warnings"], [])

    def test_a_node_address_outside_tailscale_is_warned(self) -> None:
        for key in ("node-ip", "advertise-address"):
            with self.subTest(key=key):
                data = self.check(f"server: https://100.65.24.76:6443\ntoken: x\n{key}: 192.168.174.20\n").get_json()
                self.assertTrue(data["ok"])
                self.assertTrue(any(key in w and "Tailscale" in w for w in data["warnings"]), data["warnings"])
                clean = self.check(f"server: https://100.65.24.76:6443\ntoken: x\n{key}: 100.70.57.110\n").get_json()
                self.assertEqual(clean["warnings"], [])

    def test_unknown_kind_and_oversized_text_are_rejected(self) -> None:
        self.assertEqual(self.check("a: b", kind="nope").status_code, 400)
        self.assertFalse(self.check("a: " + "x" * 70000).get_json()["ok"])

    def test_node_input_save_rejects_bad_k3s_config(self) -> None:
        node_name = self.manual_kubernetes_node("longform-check-node")

        bad = self.client.post(
            "/api/nodes/node-inputs",
            json={"node": node_name, "profile_inputs": {"kubernetes": {"k3s_config": BAD_YAML}}},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertIn("YAML syntax error", bad.get_json()["error"])

        good = self.client.post(
            "/api/nodes/node-inputs",
            json={"node": node_name, "profile_inputs": {"kubernetes": {"k3s_config": VALID_K3S_CONFIG}}},
        )
        self.assertEqual(good.status_code, 200)

    def test_node_input_merge_keeps_saved_secret_on_blank_save_and_redacts_it(self) -> None:
        node_name = self.manual_kubernetes_node("longform-merge-node")
        first = {"node_role": "control-plane", "server_token": "merge-test-secret", "node_ip": "100.103.181.86"}
        self.assertEqual(
            self.client.post("/api/nodes/node-inputs", json={"node": node_name, "profile_inputs": {"kubernetes": first}}).status_code,
            200,
        )
        blank_token = {"server_token": "", "node_ip": "100.68.5.28"}
        self.assertEqual(
            self.client.post("/api/nodes/node-inputs", json={"node": node_name, "profile_inputs": {"kubernetes": blank_token}}).status_code,
            200,
        )

        profiles = self.client.get("/api/nodes/profiles").get_json()
        saved = profiles["node_inputs"][node_name]["kubernetes"]
        self.assertEqual(saved["server_token"], "__CONFIGURED__")
        self.assertEqual(saved["node_role"], "control-plane")
        self.assertEqual(saved["node_ip"], "100.68.5.28")
        self.assertNotIn("merge-test-secret", json.dumps(profiles))

    def test_node_input_save_reports_modules_not_selected_on_the_node(self) -> None:
        node_name = self.manual_kubernetes_node("longform-unselected-node")
        response = self.client.post(
            "/api/nodes/node-inputs",
            json={"node": node_name, "profile_inputs": {"longhorn": {"storage_path": "/var/lib/longhorn"}}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ignored_unselected_modules"], ["longhorn"])
        saved = self.client.get("/api/nodes/profiles").get_json()["node_inputs"].get(node_name, {})
        self.assertNotIn("longhorn", saved)

    def test_sd_prepare_rejects_bad_k3s_config_before_queueing(self) -> None:
        body = {
            "profile": "pi5",
            "hostname": "rack-node-bad",
            "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAA test",
            "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
            "distribution": "k3s",
            "cloud_init_options": {"k3s_config": BAD_YAML},
        }
        with services.sd_prepare.JOBS_LOCK:
            jobs_before = len(services.sd_prepare.JOBS)
        with patch_in("_ensure_prereqs", return_value=[]), patch("threading.Thread") as thread:
            response = self.client.post("/api/sd/prepare", json=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("K3s config", json.dumps(response.get_json()["errors"]))
        thread.assert_not_called()
        with services.sd_prepare.JOBS_LOCK:
            self.assertEqual(len(services.sd_prepare.JOBS), jobs_before)

    def test_role_k3s_config_renders_as_valid_multiline_yaml(self) -> None:
        # Regression: hand-built strings with a literal "\n" wrote one-line YAML that K3s refused to parse.
        import re
        import subprocess
        import tempfile
        from pathlib import Path

        import yaml

        repo_root = Path(__file__).resolve().parents[2]
        ansible = repo_root / ".venv" / "bin" / "ansible-playbook"
        if not ansible.exists():
            self.skipTest("repo ansible-playbook not available")

        for role in ("raspberry_pi_3bplus", "raspberry_pi5"):
            tasks = (repo_root / "roles" / role / "tasks" / "main.yml").read_text(encoding="utf-8")
            anchor = "- name: Write structured K3s control-plane config" if role == "raspberry_pi_3bplus" else "- name: Write structured K3s worker config"
            self.assertIn(anchor, tasks)
            block = re.search(r"dest: /etc/rancher/k3s/config\.yaml\n(?:.*\n)*?    content: >-\n((?:      .*\n)+)", tasks[tasks.index(anchor):])
            self.assertIsNotNone(block, f"config content block not found in {role}")

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "config.yaml"
                play = Path(tmp) / "play.yml"
                expression = "".join("    " + line + "\n" for line in block.group(1).splitlines())
                play.write_text(
                    "- hosts: localhost\n  gather_facts: false\n  vars:\n"
                    "    inventory_hostname: test-node\n"
                    f"    {role}_k3s_config_override: ''\n"
                    f"    {role}_k3s_endpoint: 'https://100.65.24.76:6443'\n"
                    f"    {role}_k3s_token: 'K10abc:def::server:1e81'\n"
                    f"    {role}_k3s_node_ip: '100.70.57.110'\n"
                    f"    {role}_k3s_security_enabled: false\n"
                    f"    {role}_k3s_disable: ['servicelb']\n"
                    f"    {role}_lw_netpol: true\n"
                    f"    {role}_lw_cloud: false\n"
                    f"    {role}_lw_helm: false\n"
                    f"    {role}_taint: true\n"
                    f"    {role}_max_pods: '20'\n"
                    "  tasks:\n    - ansible.builtin.copy:\n"
                    f"        dest: {out}\n        content: >-\n" + expression,
                    encoding="utf-8",
                )
                result = subprocess.run(
                    [str(ansible), "-i", "localhost,", "-c", "local", str(play)],
                    capture_output=True, text=True, timeout=120, cwd=str(repo_root),
                )
                self.assertEqual(result.returncode, 0, msg=result.stdout[-800:] + result.stderr[-400:])
                text = out.read_text(encoding="utf-8")

            self.assertNotIn("\\n", text, f"{role} wrote a literal backslash-n")
            self.assertGreater(len(text.splitlines()), 4, f"{role} config is not multi-line")
            data = yaml.safe_load(text)
            self.assertEqual(data["token"], "K10abc:def::server:1e81")
            self.assertEqual(data["node-ip"], "100.70.57.110")
            self.assertEqual(data["node-name"], "test-node")

    def test_role_node_ip_prefers_input_then_tailscale_then_empty(self) -> None:
        import re
        import subprocess
        import tempfile
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        ansible = repo_root / ".venv" / "bin" / "ansible-playbook"
        if not ansible.exists():
            self.skipTest("repo ansible-playbook not available")

        cases = (
            ("explicit", "'100.1.1.1'", ["100.9.9.9"], "100.1.1.1"),
            ("blank input uses tailscale", "''", ["100.9.9.9"], "100.9.9.9"),
            ("both missing is empty so the assert fires", "''", [], ""),
        )
        for role in ("raspberry_pi_3bplus", "raspberry_pi5"):
            tasks = (repo_root / "roles" / role / "tasks" / "main.yml").read_text(encoding="utf-8")
            match = re.search(rf"    {role}_k3s_node_ip: >-\n((?:      .*\n)+)", tasks)
            self.assertIsNotNone(match, f"node_ip expression not found in {role}")
            expression = "".join("    " + line + "\n" for line in match.group(1).splitlines())

            for label, node_ip_input, tailscale_lines, expected in cases:
                with self.subTest(role=role, case=label), tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "ip.txt"
                    play = Path(tmp) / "play.yml"
                    play.write_text(
                        "- hosts: localhost\n  gather_facts: false\n  vars:\n"
                        f"    provision_inputs: {{node_ip: {node_ip_input}}}\n"
                        f"    {role}_tailscale_ip: {{stdout_lines: {tailscale_lines}}}\n"
                        "  tasks:\n    - ansible.builtin.set_fact:\n"
                        f"        {role}_k3s_node_ip: >-\n" + expression
                        + f"    - ansible.builtin.copy:\n        dest: {out}\n"
                        f"        content: \"{{{{ {role}_k3s_node_ip }}}}\"\n",
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        [str(ansible), "-i", "localhost,", "-c", "local", str(play)],
                        capture_output=True, text=True, timeout=120, cwd=str(repo_root),
                    )
                    self.assertEqual(result.returncode, 0, msg=result.stdout[-600:] + result.stderr[-300:])
                    self.assertEqual(out.read_text(encoding="utf-8").strip(), expected)

    def test_tests_never_target_the_live_callback_site(self) -> None:
        # Regression: the hardware-check step POSTed fake nodes to the real site on every test run.
        self.assertTrue(services.node_runs._default_callback_url().startswith("http://127.0.0.1:9/"))

    def test_pages_wire_the_blur_check(self) -> None:
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-longform-check="k3s_config"', home)
        self.assertIn("config_check.js", home)
        self.assertIn("config_check.js", self.client.get("/nodes").get_data(as_text=True))
        self.assertEqual(self.client.get("/static/config_check.js").status_code, 200)
