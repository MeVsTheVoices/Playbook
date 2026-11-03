"""Test-terminal page, targets and named checks."""

from __future__ import annotations

from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.storage
import services.terminal_checks


class TerminalCheckTests(BaseCallbackSiteTest):
    def test_verifier_terminal_page_uses_sidebar_navigation(self) -> None:
        response = self.client.get("/test-terminal")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('class="sidebar"', text)
        self.assertIn('href="/test-terminal"', text)
        self.assertIn('id="sidebar-toggle"', text)
        self.assertIn('/static/test_terminal.css', text)
        self.assertIn('/static/test_terminal.js', text)

    def test_test_terminal_checks_catalog(self) -> None:
        response = self.client.get("/api/test-terminal/checks")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        checks = payload.get("checks") or []
        check_ids = [c.get("id") for c in checks]
        self.assertIn("identity", check_ids)
        self.assertIn("cloud_init", check_ids)
        self.assertIn("remote_inventory", check_ids)

    def test_remote_inventory_check_command_covers_remote_attributes(self) -> None:
        check = services.terminal_checks.TERMINAL_CHECKS.get("remote_inventory")
        self.assertIsNotNone(check)
        command = str(check.get("command", ""))
        self.assertIn("hostnamectl", command)
        self.assertIn("cloud-init status --long", command)
        self.assertIn("ip -brief address", command)
        self.assertIn("lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL", command)
        self.assertIn("systemctl is-active ssh", command)
        self.assertIn("sudo -n cat /etc/rancher/k3s/config.yaml", command)

    def test_test_terminal_targets_include_callback_hosts(self) -> None:
        self.client.post(
            "/api/cloud-init/events/clear",
            json={},
        )
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-target-01",
                "fqdn": "rpi5-target-01.lab.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.78",
            },
        )

        response = self.client.get("/api/test-terminal/targets")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        targets = payload.get("targets") or []
        hosts = [t.get("host") for t in targets]
        self.assertIn("rpi5-target-01", hosts)
        self.assertIn("rpi5-target-01.lab.local", hosts)
        self.assertIn("198.51.100.78", hosts)

    def test_test_terminal_targets_hostname_alias_points_to_connect_host(self) -> None:
        self.client.post(
            "/api/cloud-init/events/clear",
            json={},
        )
        self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "he-test-node-1-aug-7",
                "group": "manual",
                "address": "10.255.45.84",
                "username": "jdizzle",
                "certificate": "rack-bootstrap-ed25519",
            },
        )
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "he-test-node-1-aug-7",
                "instance_id": "iid-he-aug-7",
                "source_ip": "192.168.174.199",
                "stage": "phone_home",
            },
        )

        response = self.client.get("/api/test-terminal/targets")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        targets = payload.get("targets") or []
        alias_target = next((t for t in targets if t.get("host") == "he-test-node-1-aug-7"), None)
        self.assertIsNotNone(alias_target)
        self.assertEqual(alias_target.get("connect_host"), "192.168.174.199")

        ip_target = next((t for t in targets if t.get("host") == "192.168.174.199"), None)
        self.assertIsNotNone(ip_target)
        self.assertEqual(ip_target.get("source"), "source_ip")

    def test_test_terminal_targets_include_manual_nodes_from_node_ops_inventory(self) -> None:
        self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "manual-terminal-01",
                "group": "manual",
                "address": "192.168.174.238",
                "username": "jdizzle",
                "certificate": "rack-bootstrap-ed25519",
            },
        )

        response = self.client.get("/api/test-terminal/targets")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        targets = payload.get("targets") or []
        manual_target = next((t for t in targets if t.get("host") == "192.168.174.238"), None)
        self.assertIsNotNone(manual_target)
        self.assertEqual(manual_target.get("connect_host"), "192.168.174.238")

    def test_test_terminal_start_rejects_invalid_check_id(self) -> None:
        response = self.client.post(
            "/api/test-terminal/start",
            json={
                "host": "10.0.0.10",
                "username": "ops",
                "key_name": "rack-bootstrap-ed25519",
                "check_id": "run-anything",
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("invalid check_id", payload.get("error", ""))

    def test_test_terminal_start_uses_named_check_command(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        with patch("threading.Thread") as thread_cls, patch_in("_seed_known_host"):
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "10.0.0.10",
                    "username": "ops",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            job = services.terminal_checks.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertTrue(command)
            self.assertEqual(job.get("check_id"), "identity")
            self.assertIn("hostname && whoami && uname -a", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn(f"UserKnownHostsFile={services.storage.KNOWN_HOSTS_FILE}", command)

        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            services.terminal_checks.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_resolves_hostname_to_callback_ip(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "he-test-node-1-aug-7",
                "group": "manual",
                "address": "10.255.45.243",
                "username": "jdizzle",
                "certificate": "rack-bootstrap-ed25519",
            },
        )
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "he-test-node-1-aug-7",
                "instance_id": "iid-he-test-node-1",
                "source_ip": "192.168.174.199",
                "stage": "phone_home",
            },
        )

        with patch("threading.Thread"):
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "he-test-node-1-aug-7",
                    "username": "jdizzle",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            job = services.terminal_checks.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertTrue(command)
            self.assertIn("jdizzle@192.168.174.199", command)
            self.assertEqual(job.get("requested_host"), "he-test-node-1-aug-7")
            self.assertEqual(job.get("resolved_host"), "192.168.174.199")

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            services.terminal_checks.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_resolves_fqdn_to_callback_ip(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "he-test-node-2",
                "fqdn": "he-test-node-2.lab.local",
                "instance_id": "iid-he-test-node-2",
                "source_ip": "192.168.174.210",
                "stage": "phone_home",
            },
        )

        with patch("threading.Thread"):
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "he-test-node-2.lab.local",
                    "username": "jdizzle",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            job = services.terminal_checks.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@192.168.174.210", command)
            self.assertEqual(job.get("requested_host"), "he-test-node-2.lab.local")
            self.assertEqual(job.get("resolved_host"), "192.168.174.210")
            services.terminal_checks.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_prefers_inventory_when_callback_ip_not_usable(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "he-test-node-transit",
                "group": "manual",
                "address": "192.168.174.240",
                "username": "jdizzle",
                "certificate": "rack-bootstrap-ed25519",
            },
        )
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "he-test-node-transit",
                "instance_id": "iid-he-test-node-transit",
                "source_ip": "10.255.45.84",
                "stage": "phone_home",
            },
        )

        with patch("threading.Thread"):
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "he-test-node-transit",
                    "username": "jdizzle",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            job = services.terminal_checks.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@192.168.174.240", command)
            self.assertEqual(job.get("resolved_host"), "192.168.174.240")
            services.terminal_checks.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_falls_back_to_raw_host_when_unmapped(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        with patch("threading.Thread"):
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "custom-lab-host",
                    "username": "jdizzle",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            job = services.terminal_checks.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@custom-lab-host", command)
            self.assertEqual(job.get("requested_host"), "custom-lab-host")
            self.assertEqual(job.get("resolved_host"), "custom-lab-host")
            services.terminal_checks.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_defaults_to_jdizzle_username(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        with patch("threading.Thread"):
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "10.0.0.10",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with services.terminal_checks.TEST_TERMINAL_LOCK:
            job = services.terminal_checks.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@10.0.0.10", command)
            services.terminal_checks.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_returns_400_when_private_key_is_unreadable(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        with patch_in("_ssh_private_key_preflight", return_value="SSH private key is not readable"):
            response = self.client.post(
                "/api/test-terminal/start",
                json={
                    "host": "10.0.0.10",
                    "username": "jdizzle",
                    "key_name": "rack-bootstrap-ed25519",
                    "check_id": "identity",
                    "port": 22,
                },
            )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("not readable", payload.get("error", ""))

    def test_test_terminal_targets_include_labeled_source_ips(self) -> None:
        self.client.post(
            "/api/cloud-init/events/clear",
            json={},
        )
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "testlab-3",
                "instance_id": "iid-lan-3",
            },
            environ_overrides={"REMOTE_ADDR": "10.255.45.243"},
        )
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "testlab-3",
                "instance_id": "iid-lan-3",
                "source_ip": "192.168.174.199",
            },
        )

        response = self.client.get("/api/test-terminal/targets")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        targets = payload.get("targets", [])
        hosts = [item.get("host") for item in targets]
        self.assertIn("192.168.174.199", hosts)

        ip_target = next((item for item in targets if item.get("host") == "192.168.174.199"), None)
        self.assertIsNotNone(ip_target)
        self.assertEqual(ip_target.get("source"), "source_ip")
        self.assertEqual(ip_target.get("hostname"), "testlab-3")
        self.assertTrue(bool(ip_target.get("usable_callback_ip")))
        self.assertEqual(ip_target.get("label_hint"), "testlab-3")
