from __future__ import annotations

import json
import base64
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

try:
    from .test_support import BaseCallbackSiteTest, patch_in
    from . import test_sd_cloud_init_render as sd_render
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in
    import test_sd_cloud_init_render as sd_render

import network_toolkit as toolkit
from domain_defaults import IMAGE_CATALOG, NODE_PROVISION_PROFILES
from services.callback_payload import _normalize_profile
from services.node_inventory import _node_ops_inventory_view
from services.node_state import _default_profiles_for_group, _profile_map
from services.sd_prepare import _map_cloud_init_options_to_env, _redact_sd_secret_options, _sanitize_sd_prepare_payload
from tailscale_policy import build_policy

AUTH_KEY = sd_render.AUTH_KEY
HASH = "$6$testsalt$" + "a" * 86
OPTIONS = {
    "toolkit_terminal_password_hash": HASH,
    "toolkit_enable_funnel": False,
    "toolkit_wifi_ssid": "Device uplink",
    "toolkit_wifi_password": "test-wifi-password",
    "toolkit_wifi_country": "US",
    "tailscale_auth_key": AUTH_KEY,
}


class NetworkToolkitTests(BaseCallbackSiteTest):
    def render(self, funnel: bool = False):
        env: dict[str, str] = {}
        _map_cloud_init_options_to_env(env, "standard", {**OPTIONS, "toolkit_enable_funnel": funnel})
        return sd_render.SdCloudInitRenderTests().render(
            R_PROFILE=toolkit.PROFILE, R_DIST="standard",
            CLOUD_INIT_TAILSCALE_AUTH_KEY=AUTH_KEY, **env,
        )

    def test_class_is_distinct_from_normal_pi_modules_and_images(self) -> None:
        self.assertEqual(_default_profiles_for_group(toolkit.GROUP), ["baseline", "tailscale", toolkit.PROFILE])
        self.assertEqual(_default_profiles_for_group("rpi_3bplus_active"), ["baseline", "tailscale", "telegraf"])
        modules = [p["id"] for p in NODE_PROVISION_PROFILES if toolkit.GROUP in p["groups"]]
        self.assertEqual(set(modules), {"baseline", "tailscale", toolkit.PROFILE})
        images = [e for e in IMAGE_CATALOG if toolkit.PROFILE in e["profiles"]]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["image_url"], toolkit.IMAGE_URL)
        self.assertEqual(images[0]["arch"], "arm64")
        self.assertIn("lite", images[0]["image_url"])

    def test_callback_discovery_keeps_appliance_class(self) -> None:
        self.assertEqual(_normalize_profile({"profile": toolkit.PROFILE}, "pi3bplus-tools", "id"), toolkit.PROFILE)
        self.client.post("/api/cloud-init/callback", json={
            "hostname": "toolkit-test", "profile": toolkit.PROFILE,
            "stage": "hardware-check", "hardware": {"model": "Raspberry Pi 3 Model B Plus", "architecture": "aarch64"},
        })
        with patch_in("_inventory_nodes", return_value=[]):
            node = next(n for n in _node_ops_inventory_view() if n["name"] == "toolkit-test")
        self.assertEqual(node["group"], toolkit.GROUP)

    def test_dashboard_exposes_class_and_profile_scoped_inputs(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<option value="network-toolkit">', html)
        self.assertIn('id="network-toolkit-section"', html)
        self.assertIn('data-option-profile="network-toolkit"', html)
        self.assertIn('data-option-default="US"', html)
        self.assertIn('id="opt_toolkit_wifi_password"', html)
        self.assertIn('value="US"', html)
        self.assertIn('autocomplete="off" required', html)

    def test_option_validation_rejects_invalid_credentials_and_wifi(self) -> None:
        self.assertEqual(toolkit.validate_options(OPTIONS, boot=True), [])
        invalid = {
            "toolkit_terminal_password_hash": "operator:plaintext\ninjected",
            "toolkit_wifi_ssid": "x" * 33,
            "toolkit_wifi_password": "short",
            "toolkit_wifi_country": "USA",
            "toolkit_enable_funnel": "yes",
        }
        for key, value in invalid.items():
            with self.subTest(key=key):
                self.assertTrue(toolkit.validate_options({**OPTIONS, key: value}, boot=True))

    def test_sanitizing_never_converts_toolkit_into_k3s(self) -> None:
        payload = _sanitize_sd_prepare_payload({
            "profile": toolkit.PROFILE, "distribution": "standard",
            "cloud_init_options": {**OPTIONS, "k3s_config": "node-name: stale", "install_telegraf": True},
        })
        self.assertEqual(payload["distribution"], "standard")
        self.assertNotIn("k3s_config", payload["cloud_init_options"])
        self.assertFalse(payload["cloud_init_options"]["install_telegraf"])
        self.assertTrue(payload["cloud_init_options"]["tailscale_enable"])
        self.assertNotIn("config_errors", payload)
        for distribution, extra in (
            ("k3s", {}),
            ("standard", {"tailscale_advertise_routes": ["192.168.1.0/24"]}),
            ("standard", {"tailscale_exit_node": "100.100.100.1"}),
        ):
            with self.subTest(distribution=distribution, extra=extra):
                invalid = _sanitize_sd_prepare_payload({
                    "profile": toolkit.PROFILE, "distribution": distribution,
                    "cloud_init_options": {**OPTIONS, **extra},
                })
                self.assertTrue(invalid["config_errors"])

    def test_prepare_rejects_invalid_settings_before_starting_job(self) -> None:
        with patch_in("_ensure_prereqs", return_value=[]), patch("routes.sd.threading.Thread") as thread:
            response = self.client.post("/api/sd/prepare", json={
                "profile": toolkit.PROFILE, "hostname": "toolkit-test", "device": "/dev/fake",
                "ssh_public_key": "ssh-ed25519 test", "callback_url": "http://example.invalid/callback",
                "cloud_init_options": {**OPTIONS, "toolkit_terminal_password_hash": "invalid"},
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("password hash", " ".join(response.get_json()["errors"]))
        thread.assert_not_called()

    def test_cli_rejects_invalid_toolkit_credentials_before_device_access(self) -> None:
        env = dict(os.environ)
        _map_cloud_init_options_to_env(env, "standard", {**OPTIONS, "toolkit_terminal_password_hash": "invalid"})
        env["CLOUD_INIT_TAILSCALE_AUTH_KEY"] = AUTH_KEY
        env["CLOUD_INIT_K3S_SERVER_HOST"] = "127.0.0.1"
        env["CLOUD_INIT_DISTRIBUTION"] = "standard"
        result = subprocess.run([
            "bash", str(toolkit.ROLE_DIR.parents[1] / "scripts/prepare_sd_cloud_init.sh"),
            "--profile", toolkit.PROFILE, "--hostname", "toolkit-test",
            "--device", "/dev/not-a-real-device", "--ssh-key-file", "/does-not-exist",
            "--callback-url", "http://example.invalid/callback",
        ], env=env, text=True, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Browser password hash", result.stderr)
        self.assertNotIn("Device does not exist", result.stderr)
        self.assertNotIn("Downloading image", result.stdout)

    def test_secrets_are_not_saved_or_echoed_in_sd_templates(self) -> None:
        public = _redact_sd_secret_options(OPTIONS)
        for key in ("tailscale_auth_key", *toolkit.SECRET_OPTIONS):
            self.assertNotIn(key, public)
        response = self.client.post("/api/sd/options-profiles", json={
            "name": "toolkit", "profile": toolkit.PROFILE, "options": OPTIONS,
        })
        self.assertEqual(response.status_code, 200)
        for data in (response.get_json(), self.client.get("/api/sd/options-profiles").get_json()):
            text = json.dumps(data)
            self.assertNotIn(HASH, text)
            self.assertNotIn(OPTIONS["toolkit_wifi_password"], text)
            self.assertNotIn(AUTH_KEY, text)
            self.assertIn(toolkit.PROFILE, text)

    def test_generated_cloud_init_uses_shared_runtime_and_small_package_set(self) -> None:
        code, data, err = self.render()
        self.assertEqual(code, 0, err)
        files = sd_render.SdCloudInitRenderTests.files(data)
        for entry in toolkit.render_files(OPTIONS):
            self.assertEqual(files[entry["path"]]["content"], entry["content"], entry["path"])
        for package in ("picocom", "minicom", "ethtool", "tcpdump", "ttyd", "nginx-light"):
            self.assertIn(package, data["packages"])
        for package in ("telegraf", "qemu-guest-agent", "fortune-mod", "cowsay"):
            self.assertNotIn(package, data["packages"])
        self.assertEqual(data["ssh_pwauth"], False)
        self.assertNotIn("/etc/rancher/k3s/config.yaml", files)
        self.assertIn("rack_role=network-toolkit", files["/etc/rack-profile"]["content"])
        env_file = files["/etc/rack-node.env"]["content"]
        self.assertNotIn(HASH, env_file)
        self.assertNotIn(OPTIONS["toolkit_wifi_password"], env_file)
        for entry in files.values():
            content = entry["content"]
            if content.startswith("#!/bin/bash"):
                check = subprocess.run(["bash", "-n"], input=content, text=True, capture_output=True)
                self.assertEqual(check.returncode, 0, check.stderr)

    def test_funnel_is_explicit_and_runs_only_after_activation(self) -> None:
        for funnel in (False, True):
            code, data, err = self.render(funnel)
            self.assertEqual(code, 0, err)
            commands = sd_render.SdCloudInitRenderTests.runcmd(data)
            self.assertFalse(any("get.k3s.io" in command for command in commands))
            activation = commands.index("/usr/local/bin/rack-toolkit-activate")
            self.assertLess(next(i for i, c in enumerate(commands) if c == "/usr/local/bin/rack-tailscale-bootstrap"), activation)
            enable = [i for i, c in enumerate(commands) if "enable --now rack-toolkit-funnel.service" in c]
            self.assertEqual(len(enable), int(funnel))
            if enable:
                self.assertGreater(enable[0], activation)

    def test_network_config_quotes_wifi_and_has_no_ethernet_default_route(self) -> None:
        config = toolkit.network_config({**OPTIONS, "toolkit_wifi_ssid": 'A: "B" # C'})
        roundtrip = yaml.safe_load(yaml.safe_dump(config))
        self.assertEqual(roundtrip, config)
        self.assertTrue(config["wifis"]["wlan0"]["dhcp4"])
        self.assertFalse(config["ethernets"]["eth0"]["dhcp4"])
        self.assertFalse(config["ethernets"]["eth0"]["dhcp6"])
        self.assertNotIn("routes", config["ethernets"]["eth0"])
        self.assertNotIn("gateway4", config["ethernets"]["eth0"])
        self.assertEqual(config["renderer"], "NetworkManager")

    @unittest.skipUnless(Path("/usr/libexec/netplan/configure").is_file(), "offline Netplan configure backend is needed")
    def test_netplan_renders_actual_wifi_ssid_and_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "etc/netplan/50-cloud-init.yaml"
            source.parent.mkdir(parents=True)
            source.write_text(yaml.safe_dump({"network": toolkit.network_config(OPTIONS)}))
            source.chmod(0o600)
            result = subprocess.run(
                ["/usr/libexec/netplan/configure", "--root-dir", tmp],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            connections = [p.read_text() for p in root.glob("run/NetworkManager/system-connections/*")]
            wifi = [text for text in connections if "ssid=" in text]
            self.assertEqual(len(wifi), 1)
            self.assertIn("ssid=Device uplink", wifi[0])
            self.assertIn("psk=test-wifi-password", wifi[0])

    def test_proxy_requires_auth_and_terminal_is_unprivileged(self) -> None:
        files = {f["path"]: f for f in toolkit.render_files(OPTIONS)}
        nginx = files["/etc/nginx/sites-enabled/network-toolkit"]["content"]
        self.assertIn("listen 127.0.0.1:7681", nginx)
        self.assertIn("auth_basic_user_file", nginx)
        self.assertNotIn("auth_basic off", nginx)
        self.assertIn("proxy_set_header Upgrade", nginx)
        terminal = files["/etc/systemd/system/rack-toolkit-terminal.service"]["content"]
        self.assertIn("User=network-console", terminal)
        self.assertIn("SupplementaryGroups=dialout", terminal)
        self.assertIn("--interface 127.0.0.1", terminal)
        self.assertIn("--check-origin", terminal)
        self.assertNotIn("User=root", terminal)
        sudoers = files["/etc/sudoers.d/network-toolkit"]["content"]
        self.assertNotIn("NOPASSWD: ALL", sudoers)
        funnel = files["/etc/systemd/system/rack-toolkit-funnel.service"]["content"]
        self.assertIn("ExecStartPre=/usr/local/bin/rack-toolkit-check", funnel)
        self.assertIn("--https=443 http://127.0.0.1:7681", funnel)
        self.assertIn("rack-toolkit-firewall.service", funnel)

    def test_device_helper_rejects_invalid_input_without_executing_commands(self) -> None:
        helper = toolkit.ROLE_DIR / "files" / "rack-device-network"
        for args in (["static", "127.0.0.1/8"], ["static", "bad;sh"], ["static", "0.0.0.0/0"], ["up"], ["static", "224.0.0.1/24"], ["static", "192.168.1.2/0"], ["static", "100.64.1.2/24"]):
            with self.subTest(args=args), patch.object(sys, "argv", [str(helper), *args]), patch("subprocess.run") as run:
                with self.assertRaises(SystemExit):
                    runpy.run_path(str(helper), run_name="__main__")
                run.assert_not_called()
        with patch.object(sys, "argv", [str(helper), "static", "192.168.1.2/24"]), patch("subprocess.run") as run:
            runpy.run_path(str(helper), run_name="__main__")
        self.assertEqual(run.call_count, 2)
        command = run.call_args_list[0].args[0]
        self.assertIn("rack-device", command)
        self.assertIn("ipv4.never-default", command)
        self.assertNotIn("wlan0", command)

    def test_node_ops_rejects_invalid_hash_without_running_ansible(self) -> None:
        from services.node_runs import _run_node_profile

        with patch("services.node_runs.subprocess.run") as run:
            result = _run_node_profile("toolkit", _profile_map()[toolkit.PROFILE], "apply", True, {
                "toolkit_terminal_password_hash": "not-a-hash",
            }, node_group=toolkit.GROUP)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("not-a-hash", result["output"])
        run.assert_not_called()

    def test_policy_toolkit_permission_is_opt_in_and_not_a_rack_tag(self) -> None:
        base_options = {"admin": "group:admins", "homelab_tag": "tag:homelab"}
        baseline = build_policy(base_options)
        self.assertNotIn("nodeAttrs", baseline["policy"])
        result = build_policy({**base_options, "toolkit_funnel": True, "ssh_user": "adminuser"})
        self.assertTrue(result["ok"], result["errors"])
        policy = result["policy"]
        self.assertEqual(policy["nodeAttrs"], [{"target": ["tag:network-toolkit"], "attr": ["funnel"]}])
        grants = [g for g in policy["grants"] if "tag:network-toolkit" in g["dst"]]
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["ip"], ["tcp:22"])
        self.assertIn("tag:network-toolkit", result["summary"]["tags"])
        self.assertFalse(any("tag:network-toolkit" in grant["src"] for grant in policy["grants"]))
        self.assertEqual(policy["ssh"][-1]["users"], ["adminuser"])
        invalid = build_policy({**base_options, "toolkit_funnel": True, "homelab_tag": "tag:network-toolkit"})
        self.assertFalse(invalid["ok"])
        invalid = build_policy({**base_options, "toolkit_funnel": True, "ssh_user": "network-console"})
        self.assertFalse(invalid["ok"])

    def test_tailscale_module_cannot_give_toolkit_rack_tags_or_exit_node(self) -> None:
        from services.node_runs import _run_node_profile

        for inputs in ({"advertise_tags": "tag:homelab"}, {"advertise_exit_node": "true"}):
            with self.subTest(inputs=inputs), patch("services.node_runs.subprocess.run") as run:
                result = _run_node_profile("toolkit", _profile_map()["tailscale"], "apply", True, inputs, node_group=toolkit.GROUP)
            self.assertEqual(result["status"], "failed")
            run.assert_not_called()

    @unittest.skipUnless(os.environ.get("TOOLKIT_TEST_NGINX") or shutil.which("nginx"), "nginx executable is needed for the HTTP integration test")
    def test_nginx_requires_login_before_proxying_http_or_websocket(self) -> None:
        nginx_binary = os.environ.get("TOOLKIT_TEST_NGINX") or shutil.which("nginx")
        password = "integration-test-password-not-used-in-production"
        password_hash = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"], input=password,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        files = {f["path"]: f for f in toolkit.render_files({**OPTIONS, "toolkit_terminal_password_hash": password_hash})}

        class Backend(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"authenticated backend")

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as tmp, HTTPServer(("127.0.0.1", 0), Backend) as backend:
            backend_thread = threading.Thread(target=backend.serve_forever)
            backend_thread.start()
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            auth_file = Path(tmp) / "htpasswd"
            auth_file.write_text(files["/etc/nginx/network-toolkit.htpasswd"]["content"])
            server = files["/etc/nginx/sites-enabled/network-toolkit"]["content"]
            server = server.replace("127.0.0.1:7681", f"127.0.0.1:{port}")
            server = server.replace("127.0.0.1:7682", f"127.0.0.1:{backend.server_port}")
            server = server.replace("/etc/nginx/network-toolkit.htpasswd", str(auth_file))
            config = Path(tmp) / "nginx.conf"
            config.write_text(
                f"daemon off; master_process off; pid {tmp}/nginx.pid; error_log {tmp}/error.log;\n"
                f"events {{}} http {{ access_log off; proxy_temp_path {tmp}/proxy; client_body_temp_path {tmp}/body;\n"
                f"fastcgi_temp_path {tmp}/fastcgi; uwsgi_temp_path {tmp}/uwsgi; scgi_temp_path {tmp}/scgi;\n{server}\n}}\n"
            )
            try:
                validation = subprocess.run([nginx_binary, "-t", "-p", tmp + "/", "-c", str(config)], capture_output=True, text=True)
                self.assertEqual(validation.returncode, 0, validation.stderr)
                with subprocess.Popen([nginx_binary, "-p", tmp + "/", "-c", str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    try:
                        url = f"http://127.0.0.1:{port}/"
                        for attempt in range(50):
                            try:
                                with urlopen(url, timeout=2) as response:
                                    self.fail(f"Unauthenticated request returned {response.status}")
                            except HTTPError as error:
                                self.assertEqual(error.code, 401)
                                error.close()
                                break
                            except URLError:
                                if process.poll() is not None or attempt == 49:
                                    process.terminate()
                                    self.fail("nginx did not start: " + process.communicate()[1].decode())
                                time.sleep(0.05)
                        for path in ("/", "/ws"):
                            with self.assertRaises(HTTPError) as failure:
                                urlopen(Request(url + path.lstrip("/"), headers={"Upgrade": "websocket", "Connection": "Upgrade"}), timeout=2)
                            self.assertEqual(failure.exception.code, 401)
                            failure.exception.close()
                        for secret, expected in (("wrong-password", 401), (password, 200)):
                            token = base64.b64encode(f"operator:{secret}".encode()).decode()
                            request = Request(url, headers={"Authorization": "Basic " + token})
                            if expected == 401:
                                with self.assertRaises(HTTPError) as failure:
                                    urlopen(request, timeout=2)
                                self.assertEqual(failure.exception.code, expected)
                                failure.exception.close()
                            else:
                                with urlopen(request, timeout=2) as response:
                                    self.assertEqual(response.status, expected)
                                    self.assertEqual(response.read(), b"authenticated backend")
                    finally:
                        process.terminate()
                        process.communicate(timeout=5)
            finally:
                backend.shutdown()
                backend_thread.join(timeout=5)
