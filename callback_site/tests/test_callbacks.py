"""Cloud-init callback intake, source IP, certificates and reports."""

from __future__ import annotations

import json
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in


class CallbackEndpointTests(BaseCallbackSiteTest):
    def test_callback_event_fragment(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-99",
                "instance_id": "iid-99",
                "fqdn": "rpi5-99.example.invalid",
                "profile": "pi5",
                "stage": "phone_home",
            },
        )
        self.assertEqual(response.status_code, 200)

        fragment = self.client.get("/fragments/cloud-init/event-rows")
        self.assertEqual(fragment.status_code, 200)
        text = fragment.get_data(as_text=True)
        self.assertIn("rpi5-99", text)
        self.assertIn("phone_home", text)

    def test_callback_source_ip_and_cert_capture(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-100",
                "instance_id": "iid-100",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "203.0.113.10, 10.0.0.1",
                "X-SSL-Client-Subject": "CN=rpi5-100",
                "X-SSL-Client-Fingerprint": "AA:BB:CC",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("hostname") == "rpi5-100"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertEqual(latest["source_ip"], "203.0.113.10")
        self.assertEqual(latest["source_ip_source"], "x-forwarded-for")
        self.assertEqual(latest["cert_subject"], "CN=rpi5-100")
        self.assertEqual(latest["cert_fingerprint"], "AA:BB:CC")

    def test_callback_dashboard_payload_shows_network_envelope(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-audit-01",
                "instance_id": "iid-audit-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "192.168.147.23, 10.255.45.243",
                "User-Agent": "Cloud-Init/24.4-0ubuntu1~24.04.2",
            },
        )
        self.assertEqual(response.status_code, 200)

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        text = self.page_text(page)
        self.assertIn("source_ip_source", text)
        self.assertIn("forwarded_for", text)
        self.assertIn("remote_addr", text)
        self.assertIn("source: ", text)

    def test_callback_source_ip_fallback_to_x_real_ip(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-101",
                "instance_id": "iid-101",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Real-IP": "198.51.100.22",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("hostname") == "rpi5-101"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertEqual(latest["source_ip"], "198.51.100.22")
        self.assertEqual(latest["source_ip_source"], "x-real-ip")

    def test_callback_source_ip_marks_transit_remote_addr(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "testlab-1",
                "instance_id": "iid-lan-1",
                "fqdn": "testlab-1.local",
            },
            environ_overrides={"REMOTE_ADDR": "10.255.45.243"},
        )

        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-lan-1"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertEqual(latest["source_ip"], "10.255.45.243")
        self.assertEqual(latest["source_ip_source"], "remote_addr_transit")

    def test_callback_source_ip_backfills_prior_transit_event(self) -> None:
        first = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "testlab-2",
                "instance_id": "iid-lan-2",
                "fqdn": "testlab-2.local",
            },
            environ_overrides={"REMOTE_ADDR": "10.255.45.243"},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "testlab-2",
                "instance_id": "iid-lan-2",
                "fqdn": "testlab-2.local",
            },
            headers={"X-Forwarded-For": "192.168.174.88"},
        )
        self.assertEqual(second.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-lan-2"]
        self.assertGreaterEqual(len(matching), 2)
        self.assertTrue(all(e.get("source_ip") == "192.168.174.88" for e in matching[:2]))

    def test_callback_dedupes_near_identical_repeat(self) -> None:
        first = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "dup-node-01",
                "instance_id": "iid-dup-01",
                "profile": "pi5",
                "stage": "runcmd",
            },
            headers={"X-Forwarded-For": "192.168.174.111"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.get_json().get("deduped", False))

        second = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "dup-node-01",
                "instance_id": "iid-dup-01",
                "profile": "pi5",
                "stage": "runcmd",
            },
            headers={"X-Forwarded-For": "192.168.174.111"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json().get("deduped", False))

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [
            e
            for e in events
            if e.get("hostname") == "dup-node-01" and e.get("instance_id") == "iid-dup-01" and e.get("stage") == "runcmd"
        ]
        self.assertEqual(len(matching), 1)

    def test_callback_harmonizes_missing_identity_and_cert_fields(self) -> None:
        first = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "harm-node-01",
                "instance_id": "iid-harm-01",
            },
            environ_overrides={"REMOTE_ADDR": "10.255.45.243"},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "harm-node-01",
                "instance_id": "iid-harm-01",
                "fqdn": "harm-node-01.lab.local",
                "profile": "pi5",
                "stage": "runcmd",
            },
            headers={
                "X-Forwarded-For": "192.168.174.120",
                "X-SSL-Client-Subject": "CN=harm-node-01",
                "X-SSL-Client-Fingerprint": "AA:12:34:56",
            },
        )
        self.assertEqual(second.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-harm-01"]
        self.assertGreaterEqual(len(matching), 2)
        self.assertTrue(all(e.get("source_ip") == "192.168.174.120" for e in matching[:2]))
        self.assertTrue(all(e.get("fqdn") == "harm-node-01.lab.local" for e in matching[:2]))
        self.assertTrue(all(e.get("profile") == "pi5" for e in matching[:2]))
        self.assertTrue(all(e.get("stage") == "runcmd" for e in matching[:2]))
        self.assertTrue(all(e.get("cert_subject") == "CN=harm-node-01" for e in matching[:2]))
        self.assertTrue(all(bool(e.get("cert_present", False)) for e in matching[:2]))

    def test_callback_dataset_field_is_stripped(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-102",
                "instance_id": "iid-102",
                "profile": "pi5",
                "stage": "phone_home",
                "dataset": {"very": "large"},
                "meta": {
                    "dataset": "inner",
                    "tags": [{"dataset": "nested-list"}, {"ok": True}],
                },
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("hostname") == "rpi5-102"]
        self.assertTrue(matching)
        latest = matching[0]
        raw_text = json.dumps(latest.get("raw", {}), sort_keys=True)
        self.assertNotIn("\"dataset\"", raw_text)

    def test_callback_profile_inferred_from_hostname_when_missing(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-42",
                "instance_id": "iid-42",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-42"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertEqual(latest["profile"], "pi5")
        self.assertEqual(latest["stage"], "phone_home")

    def test_callback_profile_falls_back_to_recent_event(self) -> None:
        first = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "edge-node-01",
                "instance_id": "iid-edge-01",
                "profile": "pi3bplus",
                "stage": "hardware-check",
            },
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "edge-node-01",
                "instance_id": "iid-edge-01",
            },
        )
        self.assertEqual(second.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-edge-01"]
        self.assertGreaterEqual(len(matching), 2)
        latest = matching[0]
        self.assertEqual(latest["profile"], "pi3bplus")
        self.assertEqual(latest["stage"], "hardware-check")

    def test_initial_callback_triggers_auto_closer_check(self) -> None:
        with patch_in("_start_node_provision_job", ["services.node_views"], return_value="initial-job") as apply_mock:
            response = self.client.post(
                "/api/cloud-init/callback",
                json={
                    "hostname": "auto-check-node",
                    "instance_id": "iid-auto-check",
                    "profile": "pi5",
                    "stage": "phone_home",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(apply_mock.called)
        self.assertEqual(apply_mock.call_args.args[:3], (["auto-check-node"], "check", True))
        self.assertEqual(response.json["closer_job_id"], "initial-job")

    def test_initial_callback_triggers_auto_closer_check_for_runcmd(self) -> None:
        with patch_in("_start_node_provision_job", ["services.node_views"], return_value="initial-job") as apply_mock:
            response = self.client.post(
                "/api/cloud-init/callback",
                json={
                    "hostname": "auto-check-runcmd-node",
                    "instance_id": "iid-auto-check-runcmd",
                    "profile": "pi5",
                    "stage": "runcmd",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(apply_mock.called)
        self.assertEqual(apply_mock.call_args.args[:3], (["auto-check-runcmd-node"], "check", True))

    def test_callback_is_stored_before_initial_job_and_retry_does_not_reschedule(self) -> None:
        from services.node_inventory import _node_ops_inventory_view
        payload = {"hostname": "persist-before-check", "profile": "pi5", "stage": "phone_home", "source_ip": "192.0.2.25"}

        def scheduled(*args):
            node = next(item for item in _node_ops_inventory_view() if item["name"] == payload["hostname"])
            self.assertEqual(node["callback_source_ip"], payload["source_ip"])
            return "persisted-job"

        with patch_in("_start_node_provision_job", ["services.node_views"], side_effect=scheduled) as start:
            first = self.client.post("/api/cloud-init/callback", json=payload)
            second = self.client.post("/api/cloud-init/callback", json=payload)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["closer_job_id"], "persisted-job")
            self.assertEqual(second.json["closer_job_id"], "")
            start.assert_called_once()

    def test_initial_callback_returns_while_closer_worker_is_still_running(self) -> None:
        import threading
        from services import provision_jobs

        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def worker(*args):
            started.set()
            release.wait(5)
            finished.set()

        try:
            with patch("services.node_views._start_node_provision_job", provision_jobs._start_node_provision_job), \
                 patch.object(provision_jobs, "_run_node_provision_job", side_effect=worker):
                response = self.client.post("/api/cloud-init/callback", json={
                    "hostname": "async-closer-test", "profile": "pi5", "stage": "phone_home", "source_ip": "192.0.2.26",
                })
                self.assertEqual(response.status_code, 200)
                self.addCleanup(provision_jobs.PROVISION_JOBS.pop, response.json["closer_job_id"], None)
                self.assertTrue(started.wait(1))
                self.assertFalse(finished.is_set(), "Callback waited for the closer worker to finish")
        finally:
            release.set()
            if started.is_set():
                self.assertTrue(finished.wait(1))

    def test_callback_node_report_is_stored_and_retrievable(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-report-01",
                "instance_id": "iid-report-01",
                "profile": "pi5",
                "stage": "runcmd",
                "node_report": {
                    "kernel": "Linux test",
                    "cpu": "A72",
                },
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        match = next((e for e in events if e.get("instance_id") == "iid-report-01"), None)
        self.assertIsNotNone(match)
        report_id = str(match.get("node_report_id", ""))
        self.assertTrue(report_id)
        self.assertNotIn("node_report", match.get("raw", {}))

        report_response = self.client.get(f"/api/cloud-init/reports/{report_id}")
        self.assertEqual(report_response.status_code, 200)
        report_payload = report_response.get_json()
        self.assertTrue(report_payload["ok"])
        self.assertEqual(report_payload["report"].get("hostname"), "rpi5-report-01")
        self.assertEqual(report_payload["report"].get("report", {}).get("cpu"), "A72")

    def test_callback_node_report_storage_failure_does_not_break_callback(self) -> None:
        with patch_in("_store_node_report", side_effect=PermissionError("readonly node reports")):
            response = self.client.post(
                "/api/cloud-init/callback",
                json={
                    "hostname": "rpi5-report-fallback-01",
                    "instance_id": "iid-report-fallback-01",
                    "profile": "pi5",
                    "stage": "hardware-check",
                    "node_report": {
                        "kernel": "Linux test",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        match = next((e for e in events if e.get("instance_id") == "iid-report-fallback-01"), None)
        self.assertIsNotNone(match)
        self.assertFalse(bool(match.get("node_report_present", False)))
        self.assertEqual(str(match.get("node_report_id", "")), "")

    def test_callback_cert_presence_from_alt_header(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-cert-01",
                "instance_id": "iid-cert-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "SSL-Client-Cert": "-----BEGIN CERTIFICATE-----...",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-cert-01"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertTrue(latest["cert_present"])
        self.assertTrue(latest["cert_header_present"])

    def test_callback_cert_presence_from_proxy_environment(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-cert-proxy-01",
                "instance_id": "iid-cert-proxy-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            environ_overrides={
                "SSL_CLIENT_S_DN": "CN=rpi5-cert-proxy-01",
                "SSL_CLIENT_CERT": "-----BEGIN CERTIFICATE-----\nproxy-cert\n-----END CERTIFICATE-----",
                "SSL_CLIENT_SHA256": "AA:BB:CC:DD",
            },
        )
        self.assertEqual(response.status_code, 200)

        events_response = self.client.get("/api/cloud-init/events")
        self.assertEqual(events_response.status_code, 200)
        events = events_response.get_json()["events"]
        matching = [e for e in events if e.get("instance_id") == "iid-cert-proxy-01"]
        self.assertTrue(matching)
        latest = matching[0]
        self.assertTrue(latest["cert_present"])
        self.assertEqual(latest["cert_subject"], "CN=rpi5-cert-proxy-01")
        self.assertEqual(latest["cert_fingerprint"], "AA:BB:CC:DD")

    def test_callback_ignores_empty_payload_noise(self) -> None:
        response = self.client.post(
            "/api/cloud-init/callback",
            data="",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("ignored"))

    def test_callback_backlog_clear(self) -> None:
        create_resp = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-clear-01",
                "instance_id": "iid-clear-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
        )
        self.assertEqual(create_resp.status_code, 200)

        before = self.client.get("/api/cloud-init/events")
        self.assertEqual(before.status_code, 200)
        self.assertTrue(before.get_json().get("events"))

        clear_resp = self.client.post("/api/cloud-init/events/clear", json={})
        self.assertEqual(clear_resp.status_code, 200)
        clear_payload = clear_resp.get_json()
        self.assertTrue(clear_payload.get("ok"))
        self.assertGreaterEqual(int(clear_payload.get("cleared", 0)), 1)

        after = self.client.get("/api/cloud-init/events")
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.get_json().get("events"), [])
