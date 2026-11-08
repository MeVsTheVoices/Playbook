"""Archive persistence, retained history, and explicit failure contracts."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from test_support import BaseCallbackSiteTest
from services import node_archive, node_state
from services.boot_logs import _store_pushed_boot_log, _schedule_boot_log_fetch, _boot_log_auto_worker
from services.events import _append_event, _read_events
from services.storage import DATA_FILE


class NodeArchiveTests(BaseCallbackSiteTest):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = patch.object(node_archive, "ARCHIVE_FILE", Path(directory.name) / "archive.json")
        store.start()
        self.addCleanup(store.stop)

    def test_registry_roundtrip_preserves_timestamp_and_aliases(self):
        node_archive.set_archived("ArchiveProof.lab.local", True)
        original = node_archive.ARCHIVE_FILE.read_text()
        node_archive.set_archived("archiveproof", True)
        self.assertEqual(node_archive.ARCHIVE_FILE.read_text(), original)
        self.assertEqual(node_archive.active_nodes([{"hostname": "ARCHIVEPROOF"}, {"fqdn": "archiveproof.lab.local"}, {"name": "other"}]), [{"name": "other"}])
        node_archive.set_archived("archiveproof", False)
        self.assertEqual(json.loads(node_archive.ARCHIVE_FILE.read_text()), [])

    def test_callbacks_stay_hidden_and_do_not_schedule_work(self):
        name = "archive-callback-proof"
        _append_event({"hostname": name, "received_at": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(self.client.put(f"/api/nodes/{name}/archive", json={"archived": True}).status_code, 200)
        with patch("services.node_views._start_node_provision_job") as job, patch("services.boot_logs.threading.Thread") as thread, patch.dict("os.environ", {"CALLBACK_AUTO_BOOT_LOGS": "1"}):
            response = self.client.post("/api/cloud-init/callback", json={"hostname": name, "profile": "pi5", "stage": "phone_home"})
            self.assertEqual(response.status_code, 200)
            job.assert_not_called()
            thread.assert_not_called()
        events = self.client.get("/api/cloud-init/events").get_json()["events"]
        self.assertNotIn(name, {item.get("hostname") for item in events})
        self.assertGreaterEqual(sum(item.get("hostname") == name for item in _read_events(limit=2000)), 2)
        self.assertNotIn(f'data-archive-node="{name}"', self.client.get("/").get_data(as_text=True))
        fragment = self.client.get("/fragments/cloud-init/event-rows")
        self.assertEqual(fragment.status_code, 200)
        self.assertNotIn(name, fragment.get_data(as_text=True))
        self.assertEqual(self.client.put(f"/api/nodes/{name}/archive", json={"archived": False}).status_code, 200)
        self.assertIn(name, {item.get("hostname") for item in self.client.get("/api/cloud-init/events").get_json()["events"]})

    def test_logs_remain_readable_but_no_ssh_fetch_for_archive(self):
        name = "archive-log-proof"
        entry = _store_pushed_boot_log(name, "kept journal")
        node_archive.set_archived(name, True)
        response = self.client.get(f"/api/nodes/{name}/boot-logs/{entry['id']}")
        self.assertEqual(response.get_json()["text"], "kept journal")
        hosts = self.client.get("/api/nodes/boot-log-hosts").get_json()["hosts"]
        self.assertIn({"name": name, "archived": True}, hosts)
        with patch("services.boot_logs._fetch_boot_logs") as fetch:
            self.assertEqual(self.client.post(f"/api/nodes/{name}/boot-logs/fetch").status_code, 409)
            with patch("services.boot_logs.time.sleep"):
                _boot_log_auto_worker(name, "192.0.2.1")
            fetch.assert_not_called()
        with patch.dict("os.environ", {"CALLBACK_AUTO_BOOT_LOGS": "1"}):
            self.assertFalse(_schedule_boot_log_fetch({"hostname": name}))

    def test_managed_node_without_callback_can_be_archived(self):
        name = "archive-managed-proof"
        state = node_state._load_monitor_state()
        state.setdefault("managed_nodes", {})[name] = {"name": name, "address": "192.0.2.1"}
        node_state._save_monitor_state(state)
        self.assertEqual(self.client.put(f"/api/nodes/{name}/archive", json={"archived": True}).status_code, 200)
        self.assertNotIn(name, {item["name"] for item in self.client.get("/api/monitor/summary").get_json()["nodes"]})
        self.assertIn(name, node_state._load_monitor_state()["managed_nodes"])

    def test_invalid_payload_and_unknown_node_do_not_create_archive(self):
        for payload in ({}, [], {"archived": "true"}, {"archived": 1}):
            self.assertEqual(self.client.put("/api/nodes/invalid-proof/archive", json=payload).status_code, 400)
        self.assertEqual(self.client.put("/api/nodes/.bad/archive", json={"archived": True}).status_code, 400)
        for value in (True, False):
            self.assertEqual(self.client.put("/api/nodes/unknown-archive-proof/archive", json={"archived": value}).status_code, 404)
        self.assertFalse(node_archive.ARCHIVE_FILE.exists())

    def test_corrupt_store_is_reported_and_not_overwritten(self):
        for text in ("{", "{}", '[{"name": "node", "archived_at": "bad"}]'):
            node_archive.ARCHIVE_FILE.write_text(text)
            with self.assertLogs("routes.monitor", level="ERROR"):
                response = self.client.get("/api/nodes/archive")
            self.assertEqual(response.status_code, 500)
            self.assertFalse(response.get_json()["ok"])
            self.assertEqual(node_archive.ARCHIVE_FILE.read_text(), text)

    def test_write_failure_does_not_change_history_or_archive(self):
        _append_event({"hostname": "archive-write-proof"})
        original = DATA_FILE.read_bytes()
        with patch("services.node_archive._atomic_write_text", side_effect=OSError("Disk full")), self.assertLogs("routes.monitor", level="ERROR"):
            response = self.client.put("/api/nodes/archive-write-proof/archive", json={"archived": True})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(DATA_FILE.read_bytes(), original)
        self.assertFalse(node_archive.ARCHIVE_FILE.exists())
