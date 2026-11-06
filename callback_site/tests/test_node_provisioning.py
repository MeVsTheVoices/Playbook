"""Node Ops pages, module inputs, provisioning runs and hardware checks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import BaseCallbackSiteTest, patch_in

import services.node_runs
import services.node_state
import services.storage


class NodeProvisioningTests(BaseCallbackSiteTest):
    def test_nodes_page_exposes_profile_segment_and_galaxy_controls(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("User-defined profile segments", text)
        self.assertIn("Ansible Galaxy requirements", text)

    def test_profile_segments_api_round_trips(self) -> None:
        response = self.client.post(
            "/api/nodes/profile-segments",
            json={
                "profile_id": "docker",
                "segment_name": "docker-override",
                "config_block": "version: '3.8'\nservices:\n  app:\n    image: example",
                "defaults_json": '{"image":"example:latest"}',
                "galaxy_requirements": ["community.general", "ansible.posix"],
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile_id"], "docker")

        loaded = self.client.get("/api/nodes/profile-segments")
        self.assertEqual(loaded.status_code, 200)
        loaded_payload = loaded.get_json()
        self.assertTrue(loaded_payload["ok"])
        self.assertIn("docker", loaded_payload["segments"])
        self.assertEqual(loaded_payload["segments"]["docker"][0]["name"], "docker-override")

    def test_node_ops_page_exposes_manual_node_selector(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn('id="manual-node-selector"', text)
        self.assertIn("Add / Update Manual Node", text)
        self.assertIn('id="prune-stale-nodes"', text)
        self.assertIn('/api/state/source-of-truth', text)

    def test_node_ops_page_guides_manual_node_edit_flow(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Choose a discovered or saved node", text)
        self.assertIn("Create new node", text)

    def test_node_ops_page_uses_compact_profile_dropdown(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn('id="profile-select"', text)
        self.assertIn('id="module-status-body"', text)
        self.assertIn("Active On Node", text)

    def test_node_ops_page_exposes_module_configuration_section(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn('id="module-configuration"', text)
        self.assertIn('id="module-status-body"', text)
        self.assertIn('id="module-detail"', text)
        self.assertIn('id="module-save-inputs"', text)
        self.assertIn('id="module-check"', text)
        self.assertIn('id="module-apply"', text)
        self.assertIn('id="provision-node"', text)
        self.assertIn('id="run-ansible"', text)
        self.assertIn("renderModuleSection", text)
        self.assertIn("data-module-field", text)
        self.assertIn("data-module-type", text)
        self.assertIn('spec.input_type || "text"', text)
        self.assertIn('"light-worker" : "worker"', text)
        self.assertIn("Rack layout:", text)

    def test_node_ops_page_exposes_longhorn_storage_guidance(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = self.page_text(response)
        self.assertIn("Use a stable mounted path for Longhorn data now", text)
        self.assertIn("default replica count at 2", text)

    def test_settings_reset_node_ops(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        nodes = summary.get_json().get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for settings reset test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["telegraf"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        set_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "telegraf": {
                        "influx_endpoint": "https://influx.example:8086",
                        "influx_bucket": "telegraf",
                        "influx_org": "homelab",
                        "influx_access_key": "access",
                        "influx_secret_key": "secret",
                    }
                },
            },
        )
        self.assertEqual(set_inputs.status_code, 200)

        reset = self.client.post("/api/settings/reset", json={"scope": "node_ops"})
        self.assertEqual(reset.status_code, 200)
        reset_payload = reset.get_json()
        self.assertTrue(reset_payload["ok"])
        self.assertEqual(reset_payload["scope"], "node_ops")

        after_profiles = self.client.get("/api/nodes/profiles")
        self.assertEqual(after_profiles.status_code, 200)
        after_payload = after_profiles.get_json()
        self.assertEqual(after_payload.get("node_configs"), {})
        self.assertEqual(after_payload.get("node_inputs"), {})

    def test_settings_reset_node_ops_deletes_ssh_material(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "reset-me-key", "comment": "test", "mode": "generate"},
        )
        self.assertEqual(create_key.status_code, 200)

        before = self.client.get("/api/ssh/keys")
        self.assertEqual(before.status_code, 200)
        before_keys = before.get_json().get("keys") or []
        self.assertTrue(any(k.get("key_name") == "reset-me-key" for k in before_keys))

        reset = self.client.post("/api/settings/reset", json={"scope": "node_ops"})
        self.assertEqual(reset.status_code, 200)
        reset_payload = reset.get_json()
        self.assertTrue(reset_payload["ok"])
        self.assertGreaterEqual(int(reset_payload.get("reset", {}).get("deleted_ssh_files", 0)), 1)

        after = self.client.get("/api/ssh/keys")
        self.assertEqual(after.status_code, 200)
        after_keys = after.get_json().get("keys") or []
        self.assertFalse(any(k.get("key_name") == "reset-me-key" for k in after_keys))

    def test_node_profile_assignment_and_check(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        summary_payload = summary.get_json()
        self.assertTrue(summary_payload["ok"])

        nodes = summary_payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for node provisioning test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["baseline", "tailscale"]},
        )
        self.assertEqual(set_profiles.status_code, 200)
        set_payload = set_profiles.get_json()
        self.assertTrue(set_payload["ok"])
        self.assertEqual(set_payload["profile_ids"], ["baseline", "tailscale"])

        save_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "tailscale": {
                        "auth_key": "tskey-auth-test",
                        "tailnet": "tail00c2ce.ts.net",
                    },
                    "baseline": {
                        "timezone": "UTC",
                    },
                },
            },
        )
        self.assertEqual(save_inputs.status_code, 200)
        self.assertTrue(save_inputs.get_json()["ok"])

        run_check = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": False},
        )
        self.assertEqual(run_check.status_code, 200)
        run_payload = run_check.get_json()
        self.assertTrue(run_payload["ok"])
        self.assertEqual(run_payload["mode"], "check")
        self.assertEqual(len(run_payload["results"]), 1)

        node_result = run_payload["results"][0]
        self.assertEqual(node_result["node"], node_name)
        self.assertGreaterEqual(len(node_result["results"]), 1)
        self.assertTrue(all(item["status"] == "planned" for item in node_result["results"]))

        summary_after = self.client.get("/api/nodes/summary")
        self.assertEqual(summary_after.status_code, 200)
        payload_after = summary_after.get_json()
        match = [n for n in payload_after.get("nodes", []) if n.get("name") == node_name]
        self.assertTrue(match)
        self.assertEqual(match[0].get("selected_profiles"), ["baseline", "tailscale"])
        latest_check = match[0].get("latest_check", {})
        self.assertEqual(latest_check.get("status"), "planned")
        self.assertEqual(latest_check.get("mode"), "check")

    def test_nodes_profiles_exposes_longhorn_profile_inputs(self) -> None:
        response = self.client.get("/api/nodes/profiles")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))

        profiles = payload.get("profiles", [])
        match = next((p for p in profiles if str(p.get("id", "")) == "longhorn"), None)
        self.assertIsNotNone(match)
        self.assertEqual(match.get("groups"), ["rpi5_active"])

        specs = match.get("input_specs", []) if isinstance(match, dict) else []
        spec_ids = {str(spec.get("id", "")) for spec in specs if isinstance(spec, dict)}
        self.assertIn("storage_path", spec_ids)
        self.assertIn("default_replica_count", spec_ids)
        self.assertIn("set_default_storage_class", spec_ids)
        self.assertIn("install_chart", spec_ids)

    def test_nodes_profiles_expose_typed_module_fields(self) -> None:
        response = self.client.get("/api/nodes/profiles")
        self.assertEqual(response.status_code, 200)
        profiles = {p.get("id"): p for p in response.get_json().get("profiles", [])}

        tailscale_specs = {s["id"]: s for s in profiles["tailscale"]["input_specs"]}
        self.assertIn("hostname", tailscale_specs)
        self.assertEqual(tailscale_specs["advertise_tags"].get("input_type"), "csv")
        self.assertEqual(tailscale_specs["advertise_exit_node"].get("input_type"), "checkbox")
        self.assertEqual(tailscale_specs["enable_ssh"].get("input_type"), "checkbox")

        kubernetes_specs = {s["id"]: s for s in profiles["kubernetes"]["input_specs"]}
        self.assertEqual(kubernetes_specs["k3s_config"].get("input_type"), "textarea")
        self.assertEqual(kubernetes_specs["k3s_security_baseline"].get("input_type"), "checkbox")
        self.assertNotIn("cluster_name", kubernetes_specs)
        self.assertNotIn("pod_cidr", kubernetes_specs)
        self.assertNotIn("service_cidr", kubernetes_specs)

        baseline_specs = {s["id"]: s for s in profiles["baseline"]["input_specs"]}
        self.assertEqual(baseline_specs["packages"].get("input_type"), "csv")
        self.assertTrue(baseline_specs["enable_hardware_checks"].get("default"))

        telegraf_ids = {s["id"] for s in profiles["telegraf"]["input_specs"]}
        self.assertIn("telegraf_config_url", telegraf_ids)

        longhorn_specs = {s["id"]: s for s in profiles["longhorn"]["input_specs"]}
        self.assertEqual(longhorn_specs["install_chart"].get("input_type"), "checkbox")
        self.assertTrue(longhorn_specs["install_chart"].get("default"))

        kubernetes_specs = {s["id"]: s for s in profiles["kubernetes"]["input_specs"]}
        self.assertTrue(kubernetes_specs["server_token"].get("sensitive"))
        self.assertEqual(kubernetes_specs["node_role"].get("input_type"), "select")
        self.assertEqual(kubernetes_specs["node_role"].get("options"), ["worker"])
        self.assertEqual(
            kubernetes_specs["node_role"].get("options_by_group"),
            {
                "rpi5_active": ["worker", "control-plane", "utility"],
                "rpi4_active": ["worker", "control-plane", "utility"],
                "rpi_3bplus_active": ["light-worker", "utility", "control-plane"],
            },
        )

    def test_node_module_input_saves_merge_and_redact_secrets(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        nodes = summary.get_json().get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes for module input merge test")
        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["tailscale", "kubernetes"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        first_save = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "tailscale": {"auth_key": "test-tail-key-secret", "tailnet": "tail00c2ce.ts.net"},
                    "kubernetes": {
                        "node_role": "control-plane",
                        "server_endpoint": "https://100.65.24.76:6443",
                        "server_token": "test-k3s-token-secret",
                        "node_ip": "100.103.181.86",
                    },
                },
            },
        )
        self.assertEqual(first_save.status_code, 200)

        second_save = self.client.post(
            "/api/nodes/node-inputs",
            json={"node": node_name, "profile_inputs": {"kubernetes": {"node_ip": "100.103.181.86"}}},
        )
        self.assertEqual(second_save.status_code, 200)

        profiles_response = self.client.get("/api/nodes/profiles")
        saved = profiles_response.get_json()["node_inputs"][node_name]
        self.assertEqual(saved["tailscale"]["tailnet"], "tail00c2ce.ts.net")
        self.assertEqual(saved["tailscale"]["auth_key"], "__CONFIGURED__")
        self.assertEqual(saved["kubernetes"]["server_token"], "__CONFIGURED__")
        self.assertEqual(saved["kubernetes"]["node_ip"], "100.103.181.86")
        self.assertNotIn("test-tail-key-secret", json.dumps(profiles_response.get_json()))
        self.assertNotIn("test-k3s-token-secret", json.dumps(profiles_response.get_json()))

        summary_response = self.client.get("/api/nodes/summary")
        self.assertNotIn("test-k3s-token-secret", json.dumps(summary_response.get_json()))

    def test_node_provision_check_mode_uses_background_job_when_ansible_enabled(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        nodes = payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for async provision job test")

        node_name = nodes[0]["name"]
        response = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": True},
        )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("job_id", payload)
        self.assertEqual(payload["mode"], "check")

        status_response = self.client.get(f"/api/nodes/provision/jobs/{payload['job_id']}")
        self.assertEqual(status_response.status_code, 200)
        job = status_response.get_json()["job"]
        self.assertIn(job["status"], {"queued", "running", "completed", "failed"})

    def test_node_provision_apply_mode_persists_plan_only_results(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        nodes = payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for apply-mode provisioning test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["baseline", "docker"]},
        )
        self.assertEqual(set_profiles.status_code, 200)
        self.assertTrue(set_profiles.get_json()["ok"])

        apply_result = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "apply", "run_ansible": False},
        )
        self.assertEqual(apply_result.status_code, 200)
        apply_payload = apply_result.get_json()
        self.assertTrue(apply_payload["ok"])
        self.assertEqual(apply_payload["mode"], "apply")
        self.assertEqual(apply_payload["results"][0]["node"], node_name)

        state = services.node_state._load_node_provisioning()
        node_results = state.get("results", {}).get(node_name, {})
        self.assertTrue(node_results)
        self.assertTrue(all(item["status"] == "planned" for item in node_results.values()))

    def test_node_provision_supports_profile_filter_for_single_module(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        nodes = summary.get_json().get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for module filter test")

        node_name = nodes[0]["name"]
        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["baseline", "tailscale"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        response = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": False, "profile_ids": ["baseline"]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        node_results = payload["results"][0]["results"]
        self.assertEqual([item["profile_id"] for item in node_results], ["baseline"])

    def test_node_provision_rejects_non_array_profile_filter(self) -> None:
        response = self.client.post(
            "/api/nodes/provision",
            json={"node": "", "mode": "check", "run_ansible": False, "profile_ids": "baseline"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("profile_ids must be an array", response.get_json().get("error", ""))

    def test_run_node_profile_uses_address_based_temp_inventory(self) -> None:
        with patch.object(services.node_runs.logger, "info"), patch.object(shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch_in("_ssh_private_key_preflight",
            return_value="",
        ):
            result = services.node_runs._run_node_profile(
                "he-test-node-1-aug-7",
                {"id": "docker", "tags": ["docker"]},
                mode="apply",
                run_ansible=True,
                profile_inputs={},
                node_group="rpi5_active",
                node_address="192.0.2.10",
            )

        self.assertEqual(result["status"], "ok")
        cmd = run_mock.call_args.args[0]
        self.assertIn("-i", cmd)
        inventory_index = cmd.index("-i") + 1
        inventory_path = cmd[inventory_index]
        self.assertNotEqual(inventory_path, str(services.storage.INVENTORY_FILE))
        self.assertTrue(Path(inventory_path).exists())
        inventory_text = Path(inventory_path).read_text(encoding="utf-8")
        self.assertIn("192.0.2.10", inventory_text)
        self.assertIn("he-test-node-1-aug-7", inventory_text)
        self.assertIn("ansible_user: jdizzle", inventory_text)
        self.assertIn("ansible_ssh_private_key_file", inventory_text)
        self.assertIn("rack-bootstrap-ed25519", inventory_text)
        self.assertIn("PreferredAuthentications=publickey", inventory_text)

    def test_run_node_profile_nests_active_group_under_family_group(self) -> None:
        with patch.object(services.node_runs.logger, "info"), patch.object(shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch_in("_ssh_private_key_preflight",
            return_value="",
        ):
            services.node_runs._run_node_profile(
                "node-3b-01",
                {"id": "tailscale", "tags": ["tailscale"]},
                mode="apply",
                run_ansible=True,
                profile_inputs={},
                node_group="rpi_3bplus_active",
                node_address="192.0.2.20",
            )

        cmd = run_mock.call_args.args[0]
        inventory_index = cmd.index("-i") + 1
        inventory_text = Path(cmd[inventory_index]).read_text(encoding="utf-8")
        self.assertIn("rpi_3bplus:", inventory_text)
        self.assertIn("rpi_3bplus_active:", inventory_text)
        self.assertIn("node-3b-01", inventory_text)

    def test_run_node_profile_uses_appdata_backed_ssh_key_path(self) -> None:
        with patch.object(services.node_runs.logger, "info"), patch.object(shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch_in("_ssh_private_key_preflight",
            return_value="",
        ):
            services.node_runs._run_node_profile(
                "node-01",
                {"id": "docker", "tags": ["docker"]},
                mode="apply",
                run_ansible=True,
                profile_inputs={},
                node_group="rpi5_active",
                node_address="192.0.2.10",
            )

        cmd = run_mock.call_args.args[0]
        inventory_index = cmd.index("-i") + 1
        inventory_path = Path(cmd[inventory_index])
        inventory_text = inventory_path.read_text(encoding="utf-8")
        expected_key_path = str(services.storage.KEYS_DIR / "rack-bootstrap-ed25519")
        self.assertIn(expected_key_path, inventory_text)
        self.assertNotIn("/home/jdizzle/Playbook/callback_site/data/ssh_keys/rack-bootstrap-ed25519", inventory_text)

    def test_run_node_profile_logs_start_and_completion(self) -> None:
        with patch.object(services.node_runs.logger, "info") as log_info, patch.object(shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch_in("_ssh_private_key_preflight",
            return_value="",
        ):
            result = services.node_runs._run_node_profile(
                "node-01",
                {"id": "docker", "tags": ["docker"]},
                mode="apply",
                run_ansible=True,
                profile_inputs={},
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(run_mock.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(any("Starting provisioning" in str(call.args[0]) for call in log_info.call_args_list))
        self.assertTrue(any("Completed provisioning" in str(call.args[0]) for call in log_info.call_args_list))

    def test_node_inputs_ignore_unselected_profiles(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        nodes = payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for profile input scope test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["tailscale"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        save_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "tailscale": {
                        "auth_key": "tskey-auth-test",
                        "tailnet": "tail00c2ce.ts.net",
                    },
                    "telegraf": {
                        "influx_endpoint": "https://influx.example:8086",
                        "influx_bucket": "telegraf",
                        "influx_org": "homelab",
                        "influx_access_key": "access",
                        "influx_secret_key": "secret",
                    },
                },
            },
        )
        self.assertEqual(save_inputs.status_code, 200)

        profiles_state = self.client.get("/api/nodes/profiles")
        self.assertEqual(profiles_state.status_code, 200)
        node_inputs = profiles_state.get_json().get("node_inputs", {}).get(node_name, {})
        self.assertIn("tailscale", node_inputs)
        self.assertNotIn("telegraf", node_inputs)

    def test_node_provision_check_mode_does_not_require_profile_inputs(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        nodes = payload.get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for input requirement test")

        node_name = nodes[0]["name"]

        set_profiles = self.client.post(
            "/api/nodes/node-configs",
            json={"node": node_name, "profile_ids": ["telegraf"]},
        )
        self.assertEqual(set_profiles.status_code, 200)

        check_missing = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": False},
        )
        self.assertEqual(check_missing.status_code, 200)
        missing_payload = check_missing.get_json()
        self.assertTrue(missing_payload["ok"])
        self.assertEqual(missing_payload["mode"], "check")
        self.assertEqual(missing_payload["results"][0]["mode"], "check")

    def test_apply_node_provisioning_includes_hardware_trigger_in_check_mode(self) -> None:
        fake_state = {"results": {}}
        with patch_in("_profile_map", return_value={"baseline": {"id": "baseline", "tags": ["baseline"]}}), patch_in("_load_node_provisioning",
            return_value=fake_state,
        ), patch_in("_node_profile_inputs", return_value={}), patch_in("_node_ops_inventory_view",
            return_value=[{"name": "node-01", "group": "rpi5_active", "address": "192.0.2.10"}],
        ), patch_in("_run_node_profile",
            return_value={
                "profile_id": "baseline",
                "status": "ok",
                "mode": "check",
                "checked_at": "2026-01-01T00:00:00+00:00",
                "output": "ok",
            },
        ), patch_in("_run_hardware_check_callback",
            return_value={
                "profile_id": "hardware_check_trigger",
                "status": "ok",
                "mode": "check",
                "checked_at": "2026-01-01T00:00:01+00:00",
                "output": "triggered",
            },
        ) as trigger_mock, patch_in("_save_node_provisioning"):
            result = services.node_runs._apply_node_provisioning("node-01", ["baseline"], mode="check", run_ansible=True)

        self.assertEqual(result["node"], "node-01")
        self.assertEqual(result["mode"], "check")
        self.assertTrue(any(item.get("profile_id") == "hardware_check_trigger" for item in result["results"]))
        trigger_mock.assert_called_once()

    def test_long_run_does_not_overwrite_results_saved_for_other_nodes_meanwhile(self) -> None:
        services.node_state._save_node_provisioning({"node_configs": {}, "results": {}, "node_inputs": {}, "manual_nodes": {}})
        self.addCleanup(services.node_state._save_node_provisioning, {"node_configs": {}, "results": {}, "node_inputs": {}, "manual_nodes": {}})

        def slow_run(node_name, profile, **kwargs):
            # Another node finishes and saves while this one is still running.
            state = services.node_state._load_node_provisioning()
            state["results"]["node-b"] = {"baseline": {"status": "ok", "checked_at": "2026-01-01T00:00:00+00:00"}}
            services.node_state._save_node_provisioning(state)
            return {"profile_id": "baseline", "status": "ok", "mode": "check", "checked_at": "2026-01-01T00:00:05+00:00", "output": ""}

        with patch_in("_profile_map", return_value={"baseline": {"id": "baseline", "tags": ["baseline"]}}), patch_in(
            "_node_ops_inventory_view", return_value=[{"name": "node-a", "group": "rpi5_active", "address": "192.0.2.10"}]
        ), patch_in("_run_node_profile", side_effect=slow_run):
            services.node_runs._apply_node_provisioning("node-a", ["baseline"], mode="apply", run_ansible=True)

        results = services.node_state._load_node_provisioning()["results"]
        self.assertIn("node-b", results)
        self.assertIn("node-a", results)

    def test_run_hardware_check_callback_fails_fast_when_private_key_unreadable(self) -> None:
        with patch_in("_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-01",
                    "group": "rpi5_active",
                    "address": "192.0.2.10",
                    "node_username": "jdizzle",
                    "certificate": "rack-bootstrap-ed25519",
                }
            ],
        ), patch_in("_ssh_private_key_preflight", return_value="SSH private key is not readable"):
            result = services.node_runs._run_hardware_check_callback("node-01", node_group="rpi5_active", node_address="192.0.2.10")

        self.assertEqual(result.get("profile_id"), "hardware_check_trigger")
        self.assertEqual(result.get("status"), "failed")
        self.assertIn("not readable", str(result.get("output", "")))

    def test_run_hardware_check_callback_uses_ansible_setup_facts(self) -> None:
        with patch_in("_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-01",
                    "group": "rpi5_active",
                    "address": "192.0.2.10",
                    "node_username": "jdizzle",
                    "certificate": "rack-bootstrap-ed25519",
                }
            ],
        ), patch_in("_ssh_private_key_preflight", return_value=""), patch_in("_seed_known_host",
        ), patch_in("_default_callback_url",
            return_value="http://127.0.0.1:9/api/cloud-init/callback",  # closed port: never the live site
        ), patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout='{"ansible_facts": {"ansible_memtotal_mb": 2048, "ansible_product_name": "RPi 5"}}', stderr=""),
        ) as run_mock:
            result = services.node_runs._run_hardware_check_callback("node-01", node_group="rpi5_active", node_address="192.0.2.10")

        self.assertEqual(result.get("status"), "ok")
        command_texts = [" ".join(str(part) for part in call.args[0]) for call in run_mock.call_args_list]
        setup_cmd = next((text for text in command_texts if "ansible.builtin.setup" in text), "")
        self.assertTrue(setup_cmd, f"Expected ansible setup command in calls: {command_texts}")
        self.assertNotIn("ansible.builtin.shell", setup_cmd)
        self.assertNotIn("python3 - <<'PY'", setup_cmd)

    def test_run_hardware_check_callback_failed_output_includes_target_source_and_auth_hint(self) -> None:
        failure_output = "".join(
            [
                "Pseudo-terminal will not be allocated because stdin is not a terminal.\n",
                "Warning: Connection is not using a post-quantum key exchange algorithm.\n",
                'node-01 | UNREACHABLE! => {"changed": false, "msg": "Failed to connect to the host via ssh: Permission denied (publickey,password)", "unreachable": true}',
            ]
        )
        with patch_in("_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-01",
                    "group": "rpi5_active",
                    "address": "10.255.45.243",
                    "callback_source_ip": "192.168.2.20",
                    "node_username": "jdizzle",
                    "certificate": "rack-bootstrap-ed25519",
                }
            ],
        ), patch_in("_ssh_private_key_preflight", return_value=""), patch_in("_seed_known_host",
        ), patch.object(
            subprocess,
            "run",
            return_value=SimpleNamespace(returncode=4, stdout="", stderr=failure_output),
        ):
            result = services.node_runs._run_hardware_check_callback("node-01", node_group="rpi5_active", node_address="10.255.45.243")

        self.assertEqual(result.get("status"), "failed")
        text = str(result.get("output", ""))
        self.assertIn("SSH authentication failed", text)
        self.assertIn("post-quantum warning is informational", text)
        self.assertIn("Connect target source: callback", text)

    def test_nodes_telegraf_test_uses_current_profile_inputs(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        nodes = summary.get_json().get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for telegraf test endpoint")

        node_name = nodes[0]["name"]

        with patch_in("_telegraf_influx_dummy_check",
            return_value={"ok": True, "checks": [{"name": "health", "ok": True}]},
        ) as check_fn:
            response = self.client.post(
                "/api/nodes/telegraf/test",
                json={
                    "node": node_name,
                    "profile_inputs": {
                        "telegraf": {
                            "influx_endpoint": "https://influx.example:8086",
                            "influx_bucket": "telegraf",
                            "influx_org": "homelab",
                            "influx_access_key": "access",
                            "influx_secret_key": "secret",
                        }
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        check_fn.assert_called_once()
        called_inputs = check_fn.call_args.args[0]
        self.assertEqual(called_inputs.get("influx_endpoint"), "https://influx.example:8086")
        self.assertEqual(called_inputs.get("influx_bucket"), "telegraf")

    def test_nodes_telegraf_test_reports_missing_inputs(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        nodes = summary.get_json().get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for telegraf validation test")

        node_name = nodes[0]["name"]
        response = self.client.post(
            "/api/nodes/telegraf/test",
            json={
                "node": node_name,
                "profile_inputs": {
                    "telegraf": {
                        "influx_endpoint": "",
                        "influx_bucket": "",
                        "influx_org": "",
                        "influx_access_key": "",
                        "influx_secret_key": "",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        result = payload.get("result", {})
        self.assertEqual(result.get("error"), "missing required telegraf inputs")
        self.assertTrue(result.get("missing"))

        save_inputs = self.client.post(
            "/api/nodes/node-inputs",
            json={
                "node": node_name,
                "profile_inputs": {
                    "telegraf": {
                        "influx_endpoint": "https://influx.example:8086",
                        "influx_bucket": "telegraf",
                        "influx_org": "homelab",
                        "influx_access_key": "access",
                        "influx_secret_key": "secret",
                    }
                },
            },
        )
        self.assertEqual(save_inputs.status_code, 200)
        self.assertTrue(save_inputs.get_json()["ok"])

        check_ok = self.client.post(
            "/api/nodes/provision",
            json={"node": node_name, "mode": "check", "run_ansible": False},
        )
        self.assertEqual(check_ok.status_code, 200)
        check_payload = check_ok.get_json()
        self.assertTrue(check_payload["ok"])
