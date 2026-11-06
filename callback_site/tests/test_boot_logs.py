"""Boot log exchange: SSH pull after a callback, storage and the API."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.boot_logs


class BootLogTests(BaseCallbackSiteTest):
    def _patched_node(self, run_result):
        node = {"name": "rpi5-01", "address": "10.0.0.5", "node_username": "ops", "certificate": ""}
        stack = [
            patch_in("_node_ops_inventory_view", ["services.boot_logs"], return_value=[node]),
            patch_in("_preferred_node_connect_target", ["services.boot_logs"], return_value=("10.0.0.5", "inventory")),
            patch_in("_ssh_private_key_preflight", ["services.boot_logs"], return_value=""),
            patch_in("_seed_known_host", ["services.boot_logs"]),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)
        run = patch("subprocess.run", return_value=run_result)
        run.start()
        self.addCleanup(run.stop)

    def test_fetch_stores_log_and_api_lists_and_reads_it(self) -> None:
        self._patched_node(SimpleNamespace(returncode=0, stdout="=== node ===\nrpi5-01\n", stderr=""))

        response = self.client.post("/api/nodes/rpi5-01/boot-logs/fetch")
        self.assertEqual(response.status_code, 200)
        log = response.get_json()["log"]
        self.assertEqual(log["status"], "ok")
        self.assertIn("ops@10.0.0.5", log["target"])

        listed = self.client.get("/api/nodes/rpi5-01/boot-logs").get_json()["logs"]
        self.assertEqual([entry["id"] for entry in listed], [log["id"]])
        text = self.client.get(f"/api/nodes/rpi5-01/boot-logs/{log['id']}").get_json()["text"]
        self.assertIn("rpi5-01", text)

    def test_failed_fetch_is_recorded_with_the_reason(self) -> None:
        self._patched_node(SimpleNamespace(returncode=255, stdout="", stderr="Permission denied (publickey)."))

        response = self.client.post("/api/nodes/rpi5-02/boot-logs/fetch")
        log = response.get_json()["log"]
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(log["status"], "failed")

        node = {"name": "rpi5-03", "address": "10.0.0.6", "node_username": "ops", "certificate": ""}
        with patch_in("_node_ops_inventory_view", ["services.boot_logs"], return_value=[node]):
            failed = services.boot_logs._fetch_boot_logs("rpi5-03")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Permission denied", failed["error"])
        self.assertEqual(self.client.get(f"/api/nodes/rpi5-03/boot-logs/{failed['id']}").status_code, 404)

    def test_only_last_ten_logs_are_kept_per_node(self) -> None:
        self._patched_node(SimpleNamespace(returncode=0, stdout="log\n", stderr=""))
        for _ in range(12):
            services.boot_logs._fetch_boot_logs("keep-node")
        self.assertEqual(len(services.boot_logs._list_boot_logs("keep-node")), services.boot_logs.KEEP_PER_NODE)

    def test_rejects_path_like_names(self) -> None:
        self.assertIn(self.client.get("/api/nodes/../boot-logs").status_code, (400, 404))
        self.assertEqual(self.client.get("/api/nodes/.hidden/boot-logs").status_code, 400)
        self.assertEqual(self.client.get("/api/nodes/rpi5-01/boot-logs/.x").status_code, 400)
        self.assertEqual(services.boot_logs._safe_name(".."), "")

    def test_callback_schedules_one_fetch_per_node_and_skips_hardware_check(self) -> None:
        services.boot_logs._last_auto_fetch.clear()
        self.addCleanup(services.boot_logs._last_auto_fetch.clear)
        event = {"hostname": "rpi5-09", "stage": "runcmd", "source_ip": "10.0.0.9"}

        with patch.dict("os.environ", {"CALLBACK_AUTO_BOOT_LOGS": "1"}), patch("services.boot_logs.threading.Thread") as thread:
            self.assertTrue(services.boot_logs._schedule_boot_log_fetch(event))
            self.assertFalse(services.boot_logs._schedule_boot_log_fetch(event))
            self.assertFalse(services.boot_logs._schedule_boot_log_fetch({**event, "hostname": "rpi5-10", "stage": "hardware-check"}))
            self.assertEqual(thread.call_count, 1)

        with patch("services.boot_logs.threading.Thread") as thread:
            self.assertFalse(services.boot_logs._schedule_boot_log_fetch({**event, "hostname": "rpi5-11"}))
            thread.assert_not_called()

    def test_dashboard_has_boot_log_panel(self) -> None:
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="boot-log-panel"', page)
        self.assertIn("boot_logs.js", page)
