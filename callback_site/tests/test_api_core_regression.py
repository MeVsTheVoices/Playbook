from __future__ import annotations

import configparser
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from .test_support import APP_DIR, TEST_DATA_DIR, BaseCallbackSiteTest, callback_app, callback_core
except ImportError:
    from test_support import APP_DIR, TEST_DATA_DIR, BaseCallbackSiteTest, callback_app, callback_core


class CallbackSiteTests(BaseCallbackSiteTest):

    def test_ansible_cfg_disables_vault_prompting(self) -> None:
        config = configparser.ConfigParser()
        config.read(Path(__file__).resolve().parents[2] / "ansible.cfg")
        defaults = config["defaults"]
        self.assertEqual(defaults.get("ask_vault_pass", "").lower(), "false")
        self.assertNotIn("vault_identity_list", defaults)

    def test_health_endpoint(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

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

    def test_images_page_exposes_grouped_catalog_and_manual_record_controls(self) -> None:
        response = self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Add image manually", text)
        self.assertIn("Pull", text)
        self.assertIn('id="managed-images-body"', text)
        self.assertIn('id="image-catalog-body"', text)
        self.assertIn('compact-list', text)
        self.assertIn('images.css', text)
        self.assertIn('images.js', text)
        self.assertIn('window.__IMAGE_CATALOG__', text)

    def test_images_page_renders_collapsible_manual_section(self) -> None:
        response = self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('class="collapsible-summary"', text)
        self.assertIn('class="collapsible-indicator"', text)
        self.assertNotIn('id="retrieved-image-manager" open', text)

    def test_images_page_includes_pull_progress_markup(self) -> None:
        response = self.client.get("/images")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="catalog-pull-progress-wrap"', text)
        self.assertIn('id="catalog-pull-progress-fill"', text)
        self.assertIn('🙂', text)

    def test_images_pull_progress_copy_is_concise(self) -> None:
        text = (APP_DIR / "static" / "images.js").read_text(encoding="utf-8")
        self.assertIn("Download in progress.", text)
        self.assertIn("Download complete.", text)
        self.assertIn("Download failed.", text)
        self.assertNotIn("Still working. The bar is moving and the tiles are reacting.", text)

    def test_images_catalog_group_defaults_keep_missing_collapsed(self) -> None:
        text = (APP_DIR / "static" / "images.js").read_text(encoding="utf-8")
        self.assertIn('title: "Missing from cache"', text)
        self.assertIn('open: false', text)
        self.assertIn('title: "Already downloaded"', text)

    def test_home_page_contains_cloud_init_template_controls(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Cloud-Init Optional Settings", text)
        self.assertIn("Profile Selection", text)
        self.assertIn("Save Current As Template", text)
        self.assertIn("Delete Selected Template", text)
        self.assertIn("Apply Security Starter Defaults", text)
        self.assertIn("Reset Optional Fields", text)
        self.assertIn("User Password (optional)", text)
        self.assertIn("Disable direct root SSH login", text)
        self.assertIn("Apply K3s security baseline (optional)", text)
        self.assertIn("Enable unattended upgrades (Debian/Ubuntu)", text)
        self.assertIn("Enable host firewall baseline (ufw/firewalld)", text)
        self.assertIn("Set SELinux to enforcing (RHEL-family)", text)
        self.assertIn("User-facing k3s template", text)
        self.assertIn("fields set", text)
        self.assertIn("fieldset-disclosure", text)
        self.assertIn("▾", text)
        self.assertNotIn("This operation erases the target device", text)

    def test_home_page_explains_required_k3s_fields(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Most users only need the server endpoint here; it tells K3s where the control-plane API lives.", text)
        self.assertIn("server: https://local-machine-ip:6443", text)

    def test_home_page_merges_pi_specific_templates_into_current_options(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('const isPiOnlyProfile = Boolean(profile.pi_only);', text)
        self.assertIn('const currentOptions = gatherCloudInitOptions();', text)
        self.assertIn('applyCloudInitOptions(isPiOnlyProfile ? { ...currentOptions, ...profileOptions } : profileOptions);', text)
        self.assertIn("Loaded Pi-specific template '${name}' and merged it into the current optional settings.", text)

    def test_home_page_collapses_cloud_init_optional_settings_by_default(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('<details class="fieldset" id="cloud-init-optional-settings">', text)
        self.assertIn("Saved templates, k3s config, identity, network, and bootstrap toggles", text)
        self.assertNotIn('<details class="fieldset" open id="cloud-init-optional-settings">', text)

    def test_home_page_places_activity_dock_before_sd_form(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        dock_index = text.index("Operator Activity Dock 🐣✨")
        form_index = text.index("SD Card Imager 🧰")
        self.assertLess(dock_index, form_index)

    def test_home_page_embeds_cached_catalog_metadata(self) -> None:
        entry = next(item for item in callback_app.IMAGE_CATALOG if str(item.get("image_url", "")).strip())
        cache_path = str(callback_app.IMAGE_CACHE_DIR / callback_app._safe_catalog_filename(str(entry.get("image_url", "")), str(entry.get("id", ""))))
        callback_core._append_image_history(
            {
                "recorded_at": "2026-07-01T00:00:00+00:00",
                "status": "ok",
                "source": "image_url",
                "source_value": entry.get("image_url", ""),
                "image_url": entry.get("image_url", ""),
                "download_path": cache_path,
                "expected_sha256": entry.get("image_sha256", ""),
                "observed_sha256": entry.get("image_sha256", ""),
            }
        )

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('"cache_path":', text)
        self.assertIn(str(entry.get("id", "")), text)
        self.assertIn(cache_path, text)

    def test_home_page_auto_selects_single_cloud_init_profile(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Auto-selected the only available template", text)
        self.assertIn("sdOptionProfileOrder.length === 1", text)

    def test_home_page_motd_banner_has_collapsed_variables_pulldown(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="opt_motd_banner"', text)
        self.assertIn("Embeddable variables", text)
        self.assertIn('<details class="motd-vars-pulldown"', text)
        self.assertNotIn('<details class="motd-vars-pulldown" open', text)
        self.assertIn("${HOSTNAME}", text)
        self.assertIn("${PROFILE}", text)
        self.assertIn("${DISTRIBUTION}", text)
        self.assertIn("${CALLBACK_URL}", text)

    def test_home_page_contains_flash_mascot_hooks(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("id=\"flash-mascot\"", text)
        self.assertIn("Happy flasher online", text)
        self.assertIn("flashMascotEl.classList.add(\"active\")", text)
        self.assertIn('SD_ACTIVE_JOB_KEY = "sd-active-job-id"', text)
        self.assertIn('SD_HOSTNAME_INCREMENTED_JOB_KEY = "sd-hostname-incremented-job-id"', text)
        self.assertIn("computeNextHostname", text)
        self.assertIn("maybeIncrementHostnameAfterSuccess", text)
        self.assertIn("resumeActiveJobIfAny", text)
        self.assertIn('href="/test-terminal"', text)

    def test_verifier_terminal_page_uses_sidebar_navigation(self) -> None:
        response = self.client.get("/test-terminal")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('class="sidebar"', text)
        self.assertIn('href="/test-terminal"', text)
        self.assertIn('id="sidebar-toggle"', text)
        self.assertIn('/static/test_terminal.css', text)
        self.assertIn('/static/test_terminal.js', text)

    def test_node_ops_page_exposes_manual_node_selector(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
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
        self.assertIn('id="profile-select"', text)
        self.assertIn("Hold Ctrl/Cmd to select multiple profiles", text)

    def test_node_ops_page_exposes_module_configuration_section(self) -> None:
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="module-configuration"', text)
        self.assertIn('id="module-list"', text)
        self.assertIn('id="module-detail"', text)
        self.assertIn('id="module-save-inputs"', text)
        self.assertIn('id="module-check"', text)
        self.assertIn('id="module-apply"', text)
        self.assertIn("renderModuleSection", text)
        self.assertIn("data-module-field", text)

    def test_monitor_page_excludes_cloud_init_template_builder(self) -> None:
        response = self.client.get("/monitor")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertNotIn("Cloud-init Option Templates", text)
        self.assertNotIn("Cloud-init Option Selection", text)
        self.assertNotIn("No fields staged.", text)
        self.assertIn("Hardware Monitor", text)
        self.assertIn("Hardware Status", text)
        self.assertIn("Cloud-Init Coverage And Security Profile", text)
        self.assertIn("Latest Provision Check Outcomes", text)
        self.assertIn('id="fleet-kpis"', text)

    def test_monitor_page_exposes_discovered_node_closer_controls(self) -> None:
        response = self.client.get("/monitor")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="monitor-node-select"', text)
        self.assertIn('id="prune-stale-monitor"', text)
        self.assertIn('id="monitor-run-check"', text)
        self.assertIn('id="summary-run-ping"', text)
        self.assertIn('id="monitor-run-ping"', text)
        self.assertIn('id="node-form-ping"', text)
        self.assertIn("Single Source of Truth", text)
        self.assertIn('id="sot-kpis"', text)
        self.assertIn('id="sot-body"', text)
        self.assertIn("Discovered node", text)
        self.assertIn("Storage (GB)", text)
        self.assertIn("GB total", text)
        self.assertIn("GB SD", text)
        self.assertIn("GB NVMe", text)
        self.assertIn("nvme_capacity_gb", text)

    def test_settings_page_exposes_stale_prune_controls(self) -> None:
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Prune Stale Nodes", text)
        self.assertIn('id="stale-max-age-hours"', text)
        self.assertIn('id="preview-stale-prune"', text)
        self.assertIn('id="run-stale-prune"', text)
        self.assertIn("Data Sets and Pull Pipelines", text)
        self.assertIn('id="refresh-data-pipelines"', text)
        self.assertIn('id="data-pipelines-grid"', text)
        self.assertIn('/api/state/source-of-truth', text)
        self.assertIn('/api/sd/images', text)

    def test_source_of_truth_endpoint_unifies_image_monitor_and_provision_state(self) -> None:
        cache_path = TEST_DATA_DIR / "image_cache" / "pi5-latest.img.xz"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("image-bytes-placeholder", encoding="utf-8")

        callback_core._append_image_history(
            {
                "recorded_at": "2026-07-12T00:00:00+00:00",
                "profile": "pi5",
                "label": "Pi5 Base",
                "source": "image_url",
                "source_value": "https://example.invalid/pi5.img.xz",
                "download_path": str(cache_path),
                "job_status": "completed",
                "hash_status": "verified",
            }
        )

        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "truth-node-01",
                "profile": "pi5",
                "stage": "hardware-check",
                "raw": {
                    "hardware": {
                        "model": "Raspberry Pi 5",
                        "ram_mb": 8192,
                    }
                },
            },
            headers={"X-Forwarded-For": "198.51.100.20"},
        )

        state = callback_core._load_node_provisioning()
        state.setdefault("node_configs", {})["truth-node-01"] = ["baseline", "telegraf"]
        callback_core._save_node_provisioning(state)

        response = self.client.get("/api/state/source-of-truth")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        unified = payload.get("state", {})
        self.assertIn("counts", unified)
        self.assertIn("images", unified)
        self.assertIn("nodes", unified)

        nodes = unified.get("nodes", [])
        match = next((node for node in nodes if node.get("name") == "truth-node-01"), None)
        self.assertIsNotNone(match)
        self.assertIn("baseline", match.get("selected_profiles", []))
        self.assertEqual(match.get("callback_source_ip"), "198.51.100.20")
        self.assertIn("hardware", match)

        image = (match or {}).get("image", {})
        self.assertEqual(image.get("source"), "image_url")
        self.assertTrue(image.get("local_available"))

        counts = unified.get("counts", {})
        self.assertGreaterEqual(int(counts.get("nodes_in_monitor", 0) or 0), 1)
        self.assertGreaterEqual(int(counts.get("nodes_with_hardware", 0) or 0), 1)

    def test_nodes_summary_uses_canonical_source_of_truth(self) -> None:
        fake_state = {
            "nodes": [{"name": "node-a", "selected_profiles": ["baseline"], "latest_check": {"ok": True}, "in_node_ops_view": True}],
            "provisioning": {
                "config": {"username": "ops", "password_set": True, "webhook_url": "https://example.invalid", "verify_tls": True},
                "node_inputs": {"node-a": {"baseline": {"timezone": "UTC"}}},
                "manual_nodes": {"node-a": {"address": "192.0.2.20"}},
            },
            "images": {"summary": {"total_records": 2}},
        }

        with patch("app._single_source_of_truth_summary", return_value=fake_state):
            response = self.client.get("/api/nodes/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload.get("nodes", [])[0].get("name"), "node-a")
        self.assertEqual(payload.get("config", {}).get("username"), "ops")
        self.assertEqual(payload.get("image_summary", {}).get("total_records"), 2)

    def test_monitor_summary_uses_canonical_source_of_truth(self) -> None:
        fake_state = {
            "nodes": [{"name": "mon-a", "in_monitor_view": True, "hardware": {"received_at": "2026-07-12T00:00:00+00:00"}}],
            "monitor": {
                "cloud_init_options": [{"id": "base", "label": "Base"}],
                "node_cloud_init": {"mon-a": ["base"]},
            }
        }

        with patch("app._single_source_of_truth_summary", return_value=fake_state):
            response = self.client.get("/api/monitor/summary")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload.get("nodes", [])[0].get("name"), "mon-a")
        self.assertEqual(payload.get("cloud_init_options", [])[0].get("id"), "base")

    def test_monitor_summary_resolves_node_cloud_init_by_alias(self) -> None:
        callback_core._save_monitor_state(
            {
                "managed_nodes": {},
                "cloud_init_options": {
                    "bootstrap": {
                        "id": "bootstrap",
                        "label": "Bootstrap",
                        "description": "alias test option",
                        "fields": [],
                    }
                },
                "node_cloud_init": {
                    "lab-node-01.example.local": ["bootstrap"],
                },
            }
        )

        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "lab-node-01",
                "fqdn": "lab-node-01.example.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
        )
        self.assertEqual(callback.status_code, 200)

        summary = self.client.get("/api/monitor/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [node for node in nodes if node.get("name") == "lab-node-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("cloud_init_option_ids"), ["bootstrap"])

    def test_monitor_node_and_cloud_init_options(self) -> None:
        summary = self.client.get("/api/monitor/summary")
        self.assertEqual(summary.status_code, 200)
        summary_payload = summary.get_json()
        self.assertTrue(summary_payload["ok"])
        self.assertIn("nodes", summary_payload)
        self.assertIn("profiles", summary_payload)
        self.assertIn("cloud_init_options", summary_payload)

        save_node = self.client.post(
            "/api/monitor/nodes",
            json={
                "name": "lab-node-01",
                "group": "custom",
                "address": "192.168.50.10",
                "enabled": True,
                "notes": "manual node",
            },
        )
        self.assertEqual(save_node.status_code, 200)
        self.assertTrue(save_node.get_json()["ok"])

        save_option = self.client.post(
            "/api/monitor/cloud-init/options",
            json={
                "id": "bootstrap",
                "label": "Bootstrap",
                "description": "base cloud-init option",
                "fields": [
                    {
                        "id": "package_set",
                        "label": "Package Set",
                        "type": "text",
                        "required": False,
                    }
                ],
            },
        )
        self.assertEqual(save_option.status_code, 200)
        self.assertTrue(save_option.get_json()["ok"])

        assign_option = self.client.post(
            "/api/monitor/nodes/lab-node-01/cloud-init",
            json={"option_ids": ["bootstrap"]},
        )
        self.assertEqual(assign_option.status_code, 200)
        assign_payload = assign_option.get_json()
        self.assertTrue(assign_payload["ok"])
        self.assertEqual(assign_payload["option_ids"], ["bootstrap"])

        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "lab-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-SSL-Client-Subject": "CN=lab-node-01",
                "X-SSL-Client-Issuer": "CN=Rack-CA",
                "X-SSL-Client-Fingerprint": "11:22:33",
            },
        )
        self.assertEqual(callback.status_code, 200)

        cert = self.client.get("/api/monitor/nodes/lab-node-01/certificate")
        self.assertEqual(cert.status_code, 200)
        cert_payload = cert.get_json()
        self.assertTrue(cert_payload["ok"])
        self.assertTrue(cert_payload["certificate"]["cert_present"])
        self.assertEqual(cert_payload["certificate"]["cert_subject"], "CN=lab-node-01")

        delete_option = self.client.delete("/api/monitor/cloud-init/options/bootstrap")
        self.assertEqual(delete_option.status_code, 200)
        self.assertTrue(delete_option.get_json()["ok"])

        delete_node = self.client.delete("/api/monitor/nodes/lab-node-01")
        self.assertEqual(delete_node.status_code, 200)
        self.assertTrue(delete_node.get_json()["ok"])

    def test_monitor_cloud_init_option_delete_clears_legacy_mixed_case_assignments(self) -> None:
        callback_core._save_monitor_state(
            {
                "managed_nodes": {},
                "cloud_init_options": {
                    "bootstrap": {
                        "id": "bootstrap",
                        "label": "Bootstrap",
                        "description": "legacy delete cleanup",
                        "fields": [],
                    }
                },
                "node_cloud_init": {
                    "lab-node-01": ["Bootstrap", "Other"],
                },
            }
        )

        delete_option = self.client.delete("/api/monitor/cloud-init/options/bootstrap")
        self.assertEqual(delete_option.status_code, 200)
        self.assertTrue(delete_option.get_json()["ok"])

        state = callback_core._load_monitor_state()
        self.assertEqual(state.get("node_cloud_init", {}).get("lab-node-01"), ["Other"])

    def test_monitor_empty_by_default_then_callback_discovery(self) -> None:
        clear_resp = self.client.post("/api/cloud-init/events/clear", json={})
        self.assertEqual(clear_resp.status_code, 200)
        callback_core._save_monitor_state(callback_core._default_monitor_state())

        initial = self.client.get("/api/monitor/summary")
        self.assertEqual(initial.status_code, 200)
        initial_payload = initial.get_json()
        self.assertTrue(initial_payload["ok"])
        self.assertEqual(initial_payload.get("nodes"), [])

        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "fresh-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "203.0.113.55",
                "X-SSL-Client-Subject": "CN=fresh-node-01",
                "X-SSL-Client-Issuer": "CN=Rack-CA",
                "X-SSL-Client-Fingerprint": "AA:11:BB:22",
            },
        )
        self.assertEqual(callback.status_code, 200)

        after = self.client.get("/api/monitor/summary")
        self.assertEqual(after.status_code, 200)
        after_payload = after.get_json()
        nodes = after_payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "fresh-node-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("source"), "callback")
        self.assertTrue(match[0].get("last_callback"))
        self.assertTrue(match[0].get("cert", {}).get("cert_present"))

        cert = self.client.get("/api/monitor/nodes/fresh-node-01/certificate")
        self.assertEqual(cert.status_code, 200)
        cert_payload = cert.get_json()
        self.assertTrue(cert_payload["ok"])
        self.assertEqual(cert_payload["certificate"]["cert_subject"], "CN=fresh-node-01")

    def test_source_of_truth_preserves_callback_source_for_discovered_node(self) -> None:
        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "source-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={"X-Forwarded-For": "203.0.113.61"},
        )
        self.assertEqual(callback.status_code, 200)

        response = self.client.get("/api/state/source-of-truth")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])

        nodes = payload.get("state", {}).get("nodes", [])
        node = next((item for item in nodes if item.get("name") == "source-node-01"), None)
        self.assertIsNotNone(node)
        self.assertTrue(bool(node.get("in_node_ops_view")))
        self.assertTrue(bool(node.get("in_monitor_view")))
        self.assertEqual(node.get("source"), "callback")

    def test_monitor_node_simple_ping_endpoint(self) -> None:
        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "ping-node-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={"X-Forwarded-For": "203.0.113.88"},
        )
        self.assertEqual(callback.status_code, 200)

        with patch("app._ping_status", return_value={"reachable": True, "latency_ms": 2.4, "error": ""}) as ping_status:
            ping = self.client.get("/api/monitor/nodes/ping-node-01/ping")

        self.assertEqual(ping.status_code, 200)
        payload = ping.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["node"], "ping-node-01")
        self.assertEqual(payload["address"], "203.0.113.88")
        self.assertTrue(payload["reachable"])
        self.assertEqual(payload["latency_ms"], 2.4)
        ping_status.assert_called_once_with("203.0.113.88")

    def test_monitor_node_simple_ping_endpoint_missing_node(self) -> None:
        response = self.client.get("/api/monitor/nodes/not-there/ping")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload["ok"])

    def test_catalog_availability_endpoint(self) -> None:
        response = self.client.get("/api/sd/catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        entries = payload.get("entries") or []
        self.assertTrue(entries)
        first = entries[0]
        self.assertIn("local_available", first)
        self.assertIn("cache_path", first)

    def test_image_catalog_endpoint_uses_yaml_source_of_truth(self) -> None:
        response = self.client.get("/api/sd/image-catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        ids = {str(entry.get("id", "")) for entry in (payload.get("entries") or [])}
        self.assertIn("k3s-ubuntu-24.04-pi-arm64", ids)
        self.assertIn("ubuntu-24.04-pi-arm64", ids)

    def test_k3s_catalog_entry_is_presented_as_k3s_only(self) -> None:
        response = self.client.get("/api/sd/image-catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        entries = payload.get("entries") or []
        k3s_entry = next((entry for entry in entries if entry.get("id") == "k3s-ubuntu-24.04-pi-arm64"), None)
        self.assertIsNotNone(k3s_entry)
        self.assertEqual(str(k3s_entry.get("label", "")), "K3s")
        self.assertEqual(str(k3s_entry.get("distro", "")), "K3s")

    def test_image_state_summary_prefers_recent_available_state_entry(self) -> None:
        history = [
            {
                "recorded_at": "2026-07-10T20:00:00+00:00",
                "label": "newest-no-path",
                "job_status": "completed",
                "local_available": False,
                "download_path": "",
                "cache_path": "",
            },
            {
                "recorded_at": "2026-07-10T19:59:00+00:00",
                "label": "older-with-cache",
                "job_status": "completed",
                "local_available": True,
                "download_path": "/tmp/cache/image.img.xz",
            },
        ]

        with patch.object(callback_core, "_read_image_history", return_value=history), patch.object(
            callback_core,
            "_available_image_urls_from_history",
            return_value=set(),
        ), patch.object(
            callback_core,
            "_catalog_entry_with_availability",
            return_value={"local_available": False},
        ):
            summary = callback_core._image_state_summary()

        self.assertEqual(summary.get("latest_label"), "newest-no-path")
        self.assertTrue(summary.get("latest_available"))
        self.assertEqual(summary.get("latest_cache_path"), "/tmp/cache/image.img.xz")
        self.assertEqual(summary.get("latest_state_emoji"), "🎉")

    def test_ssh_key_generate_and_kload_modes(self) -> None:
        key_name = "test-mode-key"
        private_key_path = callback_core.KEYS_DIR / key_name
        public_key_path = callback_core.KEYS_DIR / f"{key_name}.pub"

        generated = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "generate"},
        )
        self.assertEqual(generated.status_code, 200)
        generated_payload = generated.get_json()
        self.assertTrue(generated_payload["ok"])
        self.assertTrue(generated_payload["created"])
        self.assertEqual(generated_payload["mode"], "generate")
        self.assertEqual(oct(private_key_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(public_key_path.stat().st_mode & 0o777), "0o644")

        generate_again = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "generate"},
        )
        self.assertEqual(generate_again.status_code, 409)
        self.assertFalse(generate_again.get_json()["ok"])

        loaded = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "kload"},
        )
        self.assertEqual(loaded.status_code, 200)
        loaded_payload = loaded.get_json()
        self.assertTrue(loaded_payload["ok"])
        self.assertFalse(loaded_payload["created"])
        self.assertEqual(loaded_payload["mode"], "kload")

    def test_ssh_key_list_endpoint(self) -> None:
        key_name = "test-list-key"
        created = self.client.post(
            "/api/ssh/generate",
            json={"key_name": key_name, "comment": "test", "mode": "generate"},
        )
        self.assertEqual(created.status_code, 200)

        response = self.client.get("/api/ssh/keys")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        names = [item.get("key_name") for item in payload.get("keys", [])]
        self.assertIn(key_name, names)

    def test_ssh_key_mode_validation(self) -> None:
        response = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "bad-mode-key", "mode": "load"},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])

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

        with patch("app.threading.Thread") as thread_cls, patch("app._seed_known_host"):
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

        with callback_app.TEST_TERMINAL_LOCK:
            job = callback_app.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertTrue(command)
            self.assertEqual(job.get("check_id"), "identity")
            self.assertIn("hostname && whoami && uname -a", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn(f"UserKnownHostsFile={callback_core.KNOWN_HOSTS_FILE}", command)

        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))

        with callback_app.TEST_TERMINAL_LOCK:
            callback_app.TEST_TERMINAL_JOBS.pop(job_id, None)

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

        with patch("app.threading.Thread"):
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

        with callback_app.TEST_TERMINAL_LOCK:
            job = callback_app.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertTrue(command)
            self.assertIn("jdizzle@192.168.174.199", command)
            self.assertEqual(job.get("requested_host"), "he-test-node-1-aug-7")
            self.assertEqual(job.get("resolved_host"), "192.168.174.199")

        with callback_app.TEST_TERMINAL_LOCK:
            callback_app.TEST_TERMINAL_JOBS.pop(job_id, None)

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

        with patch("app.threading.Thread"):
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

        with callback_app.TEST_TERMINAL_LOCK:
            job = callback_app.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@192.168.174.210", command)
            self.assertEqual(job.get("requested_host"), "he-test-node-2.lab.local")
            self.assertEqual(job.get("resolved_host"), "192.168.174.210")
            callback_app.TEST_TERMINAL_JOBS.pop(job_id, None)

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

        with patch("app.threading.Thread"):
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

        with callback_app.TEST_TERMINAL_LOCK:
            job = callback_app.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@192.168.174.240", command)
            self.assertEqual(job.get("resolved_host"), "192.168.174.240")
            callback_app.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_falls_back_to_raw_host_when_unmapped(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        with patch("app.threading.Thread"):
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

        with callback_app.TEST_TERMINAL_LOCK:
            job = callback_app.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@custom-lab-host", command)
            self.assertEqual(job.get("requested_host"), "custom-lab-host")
            self.assertEqual(job.get("resolved_host"), "custom-lab-host")
            callback_app.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_defaults_to_jdizzle_username(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        with patch("app.threading.Thread"):
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

        with callback_app.TEST_TERMINAL_LOCK:
            job = callback_app.TEST_TERMINAL_JOBS.get(job_id)
            self.assertIsNotNone(job)
            command = list(job.get("command") or []) if job else []
            self.assertIn("jdizzle@10.0.0.10", command)
            callback_app.TEST_TERMINAL_JOBS.pop(job_id, None)

    def test_test_terminal_start_returns_400_when_private_key_is_unreadable(self) -> None:
        create_key = self.client.post(
            "/api/ssh/generate",
            json={"key_name": "rack-bootstrap-ed25519", "comment": "test", "mode": "generate"},
        )
        self.assertIn(create_key.status_code, (200, 409))

        with patch("app._ssh_private_key_preflight", return_value="SSH private key is not readable"):
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

    def test_sd_option_profiles_crud(self) -> None:
        initial = self.client.get("/api/sd/options-profiles")
        self.assertEqual(initial.status_code, 200)
        self.assertTrue(initial.get_json()["ok"])

        bad = self.client.post(
            "/api/sd/options-profiles",
            json={"name": "", "options": {}},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.get_json()["ok"])

        create = self.client.post(
            "/api/sd/options-profiles",
            json={
                "name": "pi5-baseline",
                "description": "base bootstrap",
                "set_preferred": True,
                "options": {
                    "timezone": "UTC",
                    "packages": ["git", "curl"],
                    "disable_ssh_password_auth": True,
                },
            },
        )
        self.assertEqual(create.status_code, 200)
        create_payload = create.get_json()
        self.assertTrue(create_payload["ok"])
        self.assertEqual(create_payload["profile"]["name"], "pi5-baseline")

        listed = self.client.get("/api/sd/options-profiles")
        self.assertEqual(listed.status_code, 200)
        listed_payload = listed.get_json()
        names = [p.get("name") for p in listed_payload.get("profiles", [])]
        self.assertIn("pi5-baseline", names)
        self.assertEqual(names[0], "pi5-baseline")

        deleted = self.client.delete("/api/sd/options-profiles/pi5-baseline")
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["ok"])

        missing_delete = self.client.delete("/api/sd/options-profiles/pi5-baseline")
        self.assertEqual(missing_delete.status_code, 404)
        self.assertFalse(missing_delete.get_json()["ok"])

    def test_sd_prepare_auto_generates_sd_options_profile_when_missing(self) -> None:
        with patch.object(callback_app, "_ensure_prereqs", return_value=[]), patch("app.threading.Thread"):
            response = self.client.post(
                "/api/sd/prepare",
                json={
                    "profile": "pi5",
                    "hostname": "rack-node-auto",
                    "device": "/dev/sdb",
                    "ssh_public_key": "ssh-ed25519 AAAA test",
                    "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                    "distribution": "k3s",
                    "pi_only": True,
                    "cloud_init_options": {
                        "timezone": "UTC",
                        "enable_fail2ban": True,
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with callback_app.JOBS_LOCK:
            job = callback_app.JOBS.get(job_id)
            self.assertIsNotNone(job)
            request_payload = dict(job.get("request", {})) if job else {}

        generated_name = str(request_payload.get("sd_options_profile", ""))
        self.assertTrue(generated_name.startswith("generated-rack-node-auto-"))

        listing = self.client.get("/api/sd/options-profiles")
        self.assertEqual(listing.status_code, 200)
        profiles = listing.get_json().get("profiles", [])
        self.assertTrue(profiles)
        self.assertEqual(profiles[0].get("name"), generated_name)
        self.assertTrue(profiles[0].get("pi_only"))

        with callback_app.JOBS_LOCK:
            callback_app.JOBS.pop(job_id, None)

    def test_sanitize_sd_prepare_payload_includes_cloud_init_options(self) -> None:
        cleaned = callback_app._sanitize_sd_prepare_payload(
            {
                "profile": "pi5",
                "hostname": "rack-node-01",
                "device": "/dev/sdb",
                "ssh_public_key": "ssh-ed25519 AAAA",
                "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                "cloud_init_options": {"timezone": "UTC", "packages": ["git"]},
                "sd_options_profile": "base-template",
                "pi_only": True,
                "ignored": "value",
            }
        )
        self.assertNotIn("ignored", cleaned)
        self.assertEqual(cleaned["sd_options_profile"], "base-template")
        self.assertEqual(cleaned["cloud_init_options"]["timezone"], "UTC")
        self.assertTrue(cleaned["pi_only"])


    def test_nodes_summary_prefers_callback_ip_over_placeholder_inventory_address(self) -> None:
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-01",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.45",
            },
        )

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "rpi5-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("address"), "198.51.100.45")
        self.assertEqual(match[0].get("address_source"), "callback")
        self.assertTrue(match[0].get("last_callback"))

    def test_manual_node_crud_surfaces_in_nodes_summary(self) -> None:
        create = self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "manual-node-01",
                "group": "manual",
                "address": "192.168.174.238",
                "username": "jdizzle",
                "password": "not-empty",
                "default_shell": "/bin/bash",
                "certificate": "rack-bootstrap-ed25519",
            },
        )
        self.assertEqual(create.status_code, 200)
        create_payload = create.get_json()
        self.assertTrue(create_payload["ok"])
        self.assertTrue(create_payload["node"].get("password_set"))

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "manual-node-01"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("group"), "manual")
        self.assertEqual(match[0].get("address"), "192.168.174.238")
        self.assertTrue(match[0].get("manual_node"))

        delete = self.client.delete("/api/nodes/manual-nodes/manual-node-01")
        self.assertEqual(delete.status_code, 200)
        self.assertTrue(delete.get_json()["ok"])

    def test_manual_node_requires_certificate(self) -> None:
        create = self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "manual-node-no-cert",
                "group": "manual",
                "address": "192.168.174.239",
                "username": "jdizzle",
                "password": "not-empty",
                "default_shell": "/bin/bash",
                "certificate": "",
            },
        )
        self.assertEqual(create.status_code, 400)
        payload = create.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "certificate is required")

    def test_manual_node_password_persists_to_provisioning_state(self) -> None:
        response = self.client.post(
            "/api/nodes/manual-nodes",
            json={
                "name": "manual-node-password-01",
                "group": "manual",
                "address": "192.168.174.239",
                "username": "jdizzle",
                "password": "expected-password-value",
                "certificate": "rack-bootstrap-ed25519",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["node"].get("password_set"))

        state = callback_core._load_node_provisioning()
        manual_nodes = state.get("manual_nodes", {}) if isinstance(state.get("manual_nodes"), dict) else {}
        self.assertIn("manual-node-password-01", manual_nodes)
        saved_node = manual_nodes["manual-node-password-01"]
        self.assertEqual(str(saved_node.get("password", "")), "expected-password-value")

    def test_manual_node_delete_missing_returns_404(self) -> None:
        response = self.client.delete("/api/nodes/manual-nodes/does-not-exist")
        self.assertEqual(response.status_code, 404)
        payload = response.get_json()
        self.assertFalse(payload["ok"])

    def test_node_ops_inventory_view_hides_default_untouched_inventory_nodes(self) -> None:
        with patch.object(
            callback_core,
            "_inventory_nodes",
            return_value=[{"name": "rpi5-default-01", "group": "rpi5_active", "address": "192.168.1.44"}],
        ), patch.object(
            callback_core,
            "_latest_callback_details_by_host",
            return_value={},
        ), patch.object(
            callback_core,
            "_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}},
        ):
            view = callback_app._node_ops_inventory_view()

        self.assertEqual(view, [])

    def test_node_ops_inventory_view_includes_state_only_nodes(self) -> None:
        with patch.object(
            callback_core,
            "_inventory_nodes",
            return_value=[],
        ), patch.object(
            callback_core,
            "_latest_callback_details_by_host",
            return_value={},
        ), patch.object(
            callback_core,
            "_load_node_provisioning",
            return_value={
                "node_configs": {"node-from-state": ["identity"]},
                "node_inputs": {},
                "results": {},
            },
        ):
            view = callback_app._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("name"), "node-from-state")
        self.assertEqual(view[0].get("group"), "provisioned")

    def test_node_ops_inventory_view_includes_callback_only_nodes(self) -> None:
        with patch.object(
            callback_core,
            "_inventory_nodes",
            return_value=[],
        ), patch.object(
            callback_core,
            "_latest_callback_details_by_host",
            return_value={
                "fresh-callback-01": {
                    "last_callback": "2026-07-10T00:00:00+00:00",
                    "source_ip": "192.168.174.88",
                    "profile": "pi5",
                }
            },
        ), patch.object(
            callback_core,
            "_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}, "manual_nodes": {}},
        ):
            view = callback_app._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("name"), "fresh-callback-01")
        self.assertEqual(view[0].get("group"), "rpi5_active")
        self.assertEqual(view[0].get("address"), "192.168.174.88")
        self.assertEqual(view[0].get("address_source"), "callback")

    def test_node_ops_inventory_view_preserves_callback_address_source(self) -> None:
        with patch.object(
            callback_core,
            "_inventory_nodes",
            return_value=[],
        ), patch.object(
            callback_core,
            "_latest_callback_details_by_host",
            return_value={
                "seeded-node": {
                    "last_callback": "2026-07-10T00:00:00+00:00",
                    "source_ip": "10.255.45.207",
                    "profile": "pi5",
                }
            },
        ), patch.object(
            callback_core,
            "_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}, "manual_nodes": {}},
        ):
            view = callback_app._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("address_source"), "callback")

    def test_node_ops_inventory_view_uses_connectable_callback_ip_from_history(self) -> None:
        with patch.object(
            callback_core,
            "_inventory_nodes",
            return_value=[],
        ), patch.object(
            callback_core,
            "_latest_callback_details_by_host",
            return_value={
                "seeded-node": {
                    "last_callback": "2026-07-10T00:00:00+00:00",
                    "source_ip": "10.255.45.207",
                    "source_ips": ["10.255.45.207", "192.168.174.88"],
                    "source_ip_source": "remote_addr_transit",
                    "profile": "pi5",
                }
            },
        ), patch.object(
            callback_core,
            "_load_node_provisioning",
            return_value={"node_configs": {}, "node_inputs": {}, "results": {}, "manual_nodes": {}},
        ):
            view = callback_app._node_ops_inventory_view()

        self.assertEqual(len(view), 1)
        self.assertEqual(view[0].get("callback_source_ip"), "10.255.45.207")
        self.assertEqual(view[0].get("callback_connect_ip"), "192.168.174.88")
        self.assertEqual(view[0].get("address"), "192.168.174.88")
        self.assertEqual(view[0].get("address_source"), "callback")

    def test_preferred_connect_ip_from_history_falls_back_to_usable_private_ip(self) -> None:
        selected = callback_core._preferred_connect_ip_from_history(
            "10.255.45.207",
            ["10.255.45.207", "192.168.174.88"],
        )
        self.assertEqual(selected, "192.168.174.88")

    def test_nodes_monitor_uses_enriched_address_source(self) -> None:
        with patch.object(
            callback_app,
            "_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-live-01",
                    "group": "rpi5_active",
                    "address": "203.0.113.21",
                    "address_source": "callback",
                    "last_callback": "2026-06-29T00:00:00+00:00",
                }
            ],
        ), patch.object(
            callback_app,
            "_ping_status",
            return_value={"reachable": True, "latency_ms": 1.25, "error": ""},
        ):
            monitor = self.client.post("/api/nodes/monitor", json={"send_webhook": False})

        self.assertEqual(monitor.status_code, 200)
        payload = monitor.get_json()
        self.assertTrue(payload["ok"])
        statuses = payload.get("statuses", [])
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].get("address"), "203.0.113.21")
        self.assertEqual(statuses[0].get("address_source"), "callback")
        self.assertEqual(statuses[0].get("last_callback"), "2026-06-29T00:00:00+00:00")

    def test_nodes_summary_matches_callback_aliases(self) -> None:
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-02.lab.local",
                "fqdn": "rpi5-02.lab.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.52",
            },
        )

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "rpi5-02"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("address"), "198.51.100.52")
        self.assertEqual(match[0].get("address_source"), "callback")

    def test_nodes_summary_includes_latest_callback_metadata(self) -> None:
        callback = self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-01",
                "instance_id": "iid-meta-01",
                "profile": "pi5",
                "stage": "runcmd",
                "node_report": {"source": "unit-test"},
            },
            headers={
                "X-Forwarded-For": "198.51.100.45",
                "X-SSL-Client-Subject": "CN=rpi5-01",
                "X-SSL-Client-Fingerprint": "AA:BB:CC:DD",
            },
        )
        self.assertEqual(callback.status_code, 200)

        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        payload = summary.get_json()
        self.assertTrue(payload["ok"])
        nodes = payload.get("nodes", [])
        match = [n for n in nodes if n.get("name") == "rpi5-01"]
        self.assertTrue(match)
        row = match[0]
        self.assertEqual(row.get("callback_profile"), "pi5")
        self.assertEqual(row.get("callback_stage"), "runcmd")
        self.assertEqual(row.get("callback_instance_id"), "iid-meta-01")
        self.assertEqual(row.get("callback_fqdn"), "")
        self.assertEqual(row.get("callback_source_ip"), "198.51.100.45")
        self.assertEqual(row.get("callback_source_ips"), ["198.51.100.45"])
        self.assertEqual(row.get("callback_source_ip_source"), "x-forwarded-for")
        self.assertTrue(row.get("callback_cert_present"))
        self.assertEqual(row.get("callback_cert_subject"), "CN=rpi5-01")
        self.assertEqual(row.get("callback_cert_fingerprint"), "AA:BB:CC:DD")
        self.assertTrue(row.get("callback_node_report_present"))
        self.assertTrue(str(row.get("callback_node_report_id", "")).strip())

    def test_latest_callback_details_uses_most_recent_source_ip_and_tracks_history(self) -> None:
        events = [
            {
                "received_at": "2026-01-01T00:00:02+00:00",
                "hostname": "node-01",
                "fqdn": "node-01.lab.local",
                "source_ip": "10.255.45.243",
                "source_ip_source": "remote_addr_transit",
                "profile": "pi5",
                "raw_profile": "pi5",
                "profile_source": "payload",
                "stage": "power_restore",
                "instance_id": "iid-2",
                "cert_present": False,
                "node_report_present": False,
            },
            {
                "received_at": "2026-01-01T00:00:01+00:00",
                "hostname": "node-01",
                "fqdn": "node-01.lab.local",
                "source_ip": "192.168.174.100",
                "source_ip_source": "payload:source_ip",
                "profile": "pi5",
                "raw_profile": "pi5",
                "profile_source": "payload",
                "stage": "hardware-check",
                "instance_id": "iid-1",
                "cert_present": False,
                "node_report_present": True,
            },
        ]
        with patch.object(callback_core, "_read_events", return_value=events):
            details = callback_core._latest_callback_details_by_host(limit=10)

        self.assertEqual(details["node-01"]["source_ip"], "10.255.45.243")
        self.assertEqual(details["node-01"]["source_ip_source"], "remote_addr_transit")
        self.assertEqual(details["node-01"]["fqdn"], "node-01.lab.local")
        self.assertEqual(details["node-01"]["source_ips"], ["10.255.45.243", "192.168.174.100"])

    def test_nodes_monitor_surfaces_callback_last_seen(self) -> None:
        self.client.post(
            "/api/cloud-init/callback",
            json={
                "hostname": "rpi5-02.lab.local",
                "fqdn": "rpi5-02.lab.local",
                "profile": "pi5",
                "stage": "phone_home",
            },
            headers={
                "X-Forwarded-For": "198.51.100.52",
            },
        )

        monitor = self.client.post("/api/nodes/monitor", json={"send_webhook": False})
        self.assertEqual(monitor.status_code, 200)
        payload = monitor.get_json()
        self.assertTrue(payload["ok"])
        statuses = payload.get("statuses", [])
        match = [n for n in statuses if n.get("name") == "rpi5-02"]
        self.assertTrue(match)
        self.assertEqual(match[0].get("address_source"), "callback")
        self.assertTrue(match[0].get("last_callback"))

    def test_prune_stale_nodes_rejects_invalid_hours(self) -> None:
        response = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": "abc", "dry_run": True},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])

    def test_prune_stale_nodes_parses_string_dry_run_false(self) -> None:
        stale_ts = "2020-01-01T00:00:00+00:00"
        callback_core._append_event(
            {
                "received_at": stale_ts,
                "hostname": "string-dryrun-node",
                "fqdn": "string-dryrun-node.lab.local",
                "instance_id": "iid-string-dryrun",
                "profile": "pi5",
                "stage": "phone_home",
                "source_ip": "198.51.100.77",
                "source_ip_source": "x-forwarded-for",
            }
        )

        execute = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": 24, "dry_run": "false"},
        )
        self.assertEqual(execute.status_code, 200)
        payload = execute.get_json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dry_run"])
        names = {row.get("name") for row in payload.get("stale_nodes", [])}
        self.assertIn("string-dryrun-node", names)

    def test_prune_stale_nodes_propagates_across_all_state(self) -> None:
        stale_ts = "2020-01-01T00:00:00+00:00"
        fresh_ts = datetime.now(timezone.utc).isoformat()

        callback_core._append_event(
            {
                "received_at": stale_ts,
                "hostname": "stale-node-01",
                "fqdn": "stale-node-01.lab.local",
                "instance_id": "iid-stale-01",
                "profile": "pi5",
                "stage": "phone_home",
                "source_ip": "198.51.100.11",
                "source_ip_source": "x-forwarded-for",
            }
        )
        callback_core._append_event(
            {
                "received_at": fresh_ts,
                "hostname": "fresh-node-01",
                "fqdn": "fresh-node-01.lab.local",
                "instance_id": "iid-fresh-01",
                "profile": "pi5",
                "stage": "phone_home",
                "source_ip": "198.51.100.12",
                "source_ip_source": "x-forwarded-for",
            }
        )

        monitor_state = callback_core._load_monitor_state()
        monitor_state["managed_nodes"] = {
            "stale-node-01": {
                "name": "stale-node-01",
                "group": "rpi5_active",
                "address": "198.51.100.11",
                "notes": "stale",
                "enabled": True,
            },
            "fresh-node-01": {
                "name": "fresh-node-01",
                "group": "rpi5_active",
                "address": "198.51.100.12",
                "notes": "fresh",
                "enabled": True,
            },
        }
        monitor_state["node_cloud_init"] = {
            "stale-node-01.lab.local": ["base"],
            "fresh-node-01": ["base"],
        }
        callback_core._save_monitor_state(monitor_state)

        provision_state = callback_core._load_node_provisioning()
        provision_state["node_configs"] = {
            "stale-node-01.lab.local": ["baseline"],
            "fresh-node-01": ["baseline"],
        }
        provision_state["node_inputs"] = {
            "stale-node-01.lab.local": {"baseline": {"timezone": "UTC"}},
            "fresh-node-01": {"baseline": {"timezone": "UTC"}},
        }
        provision_state["results"] = {
            "stale-node-01.lab.local": {"checked_at": stale_ts},
            "fresh-node-01": {"checked_at": fresh_ts},
        }
        provision_state["manual_nodes"] = {
            "stale-node-01.lab.local": {
                "name": "stale-node-01",
                "group": "manual",
                "address": "198.51.100.11",
            },
            "fresh-node-01": {"name": "fresh-node-01", "group": "manual", "address": "198.51.100.12"},
        }
        callback_core._save_node_provisioning(provision_state)

        preview = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": 24, "dry_run": True},
        )
        self.assertEqual(preview.status_code, 200)
        preview_payload = preview.get_json()
        self.assertTrue(preview_payload["ok"])
        self.assertTrue(preview_payload["dry_run"])
        preview_names = {row.get("name") for row in preview_payload.get("stale_nodes", [])}
        self.assertIn("stale-node-01", preview_names)

        dry_run_summary = self.client.get("/api/nodes/summary").get_json()
        dry_run_names = {row.get("name") for row in dry_run_summary.get("nodes", [])}
        self.assertIn("stale-node-01", dry_run_names)

        execute = self.client.post(
            "/api/nodes/stale/prune",
            json={"max_age_hours": 24, "dry_run": False},
        )
        self.assertEqual(execute.status_code, 200)
        execute_payload = execute.get_json()
        self.assertTrue(execute_payload["ok"])
        self.assertFalse(execute_payload["dry_run"])
        self.assertGreaterEqual(int(execute_payload.get("removed", {}).get("callbacks", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_cloud_init", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_configs", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_inputs", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("node_results", 0) or 0), 1)
        self.assertEqual(int(execute_payload.get("removed", {}).get("manual_nodes", 0) or 0), 1)

        nodes_summary = self.client.get("/api/nodes/summary")
        self.assertEqual(nodes_summary.status_code, 200)
        nodes_payload = nodes_summary.get_json()
        nodes_names = {row.get("name") for row in nodes_payload.get("nodes", [])}
        self.assertNotIn("stale-node-01", nodes_names)
        self.assertIn("fresh-node-01", nodes_names)

        monitor_summary = self.client.get("/api/monitor/summary")
        self.assertEqual(monitor_summary.status_code, 200)
        monitor_payload = monitor_summary.get_json()
        monitor_names = {row.get("name") for row in monitor_payload.get("nodes", [])}
        self.assertNotIn("stale-node-01", monitor_names)
        self.assertIn("fresh-node-01", monitor_names)

        monitor_state_after = callback_core._load_monitor_state()
        self.assertNotIn("stale-node-01.lab.local", monitor_state_after.get("node_cloud_init", {}))

        provision_after = callback_core._load_node_provisioning()
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("node_configs", {}))
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("node_inputs", {}))
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("results", {}))
        self.assertNotIn("stale-node-01.lab.local", provision_after.get("manual_nodes", {}))

    def test_sd_prepare_queues_cloud_init_options_metadata(self) -> None:
        with patch.object(callback_app, "_ensure_prereqs", return_value=[]), patch("app.threading.Thread") as thread_cls:
            response = self.client.post(
                "/api/sd/prepare",
                json={
                    "profile": "pi5",
                    "hostname": "rack-node-02",
                    "device": "/dev/sdb",
                    "ssh_public_key": "ssh-ed25519 AAAA test",
                    "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
                    "distribution": "k3s",
                    "sd_options_profile": "base-profile",
                    "cloud_init_options": {
                        "timezone": "UTC",
                        "packages": ["git", "curl"],
                        "k3s_config": "# pragma: k3s-config-v1\nserver: https://control-plane.example:6443\n",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        job_id = payload.get("job_id")
        self.assertTrue(job_id)

        with callback_app.JOBS_LOCK:
            job = callback_app.JOBS.get(job_id)
            self.assertIsNotNone(job)
            request_payload = dict(job.get("request", {})) if job else {}

        self.assertEqual(request_payload.get("sd_options_profile"), "base-profile")
        self.assertEqual(request_payload.get("distribution"), "k3s")
        self.assertEqual(request_payload.get("cloud_init_options", {}).get("timezone"), "UTC")
        self.assertEqual(request_payload.get("cloud_init_options", {}).get("packages"), ["git", "curl"])
        self.assertIn("pragma: k3s-config-v1", request_payload.get("cloud_init_options", {}).get("k3s_config", ""))

        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertTrue(kwargs.get("daemon"))
        args_tuple = kwargs.get("args") or ()
        self.assertTrue(len(args_tuple) >= 2)
        self.assertEqual(args_tuple[0], job_id)

        with callback_app.JOBS_LOCK:
            callback_app.JOBS.pop(job_id, None)

    def test_run_prepare_job_maps_security_cloud_init_options_to_env(self) -> None:
        class FakePopen:
            def __init__(self, *args, **kwargs) -> None:
                self.returncode = 0
                self.stdout = iter(())

            def wait(self) -> int:
                return self.returncode

        job_id = "job-security-env"
        form = {
            "profile": "pi5",
            "hostname": "rack-sec-01",
            "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAABAQC test",
            "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
            "distribution": "standard",
            "cloud_init_options": {
                "disable_ssh_password_auth": True,
                "disable_root_ssh": True,
                "enable_fail2ban": True,
                "enable_unattended_upgrades": True,
                "enable_power_restore_callback": True,
                "reboot_after_bootstrap": True,
                "usb_max_current_enable": True,
                "pciex1_enable": True,
                "pciex1_gen3": True,
                "ssh_port": "2222",
                "ssh_allow_groups": ["sudo", "wheel"],
                "extra_ssh_keys": [
                    "ssh-ed25519 AAAA111 extra1",
                    "ssh-ed25519 AAAA222 extra2",
                ],
            },
        }

        with callback_app.JOBS_LOCK:
            callback_app.JOBS[job_id] = {
                "status": "queued",
                "created_at": "2026-01-01T00:00:00+00:00",
                "request": dict(form),
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }

        popen_call = {}

        def fake_popen(*args, **kwargs):
            popen_call.update(kwargs)
            return FakePopen()

        with patch("core.os.geteuid", return_value=0), patch(
            "core._archive_ssh_public_key", return_value={"archive_path": "test-archive"}
        ), patch("core.subprocess.Popen", side_effect=fake_popen), patch("core._record_image_history"):
            callback_core._run_prepare_job(job_id, form)

        env = popen_call.get("env") or {}
        self.assertEqual(env.get("CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH"), "true")
        self.assertEqual(env.get("CLOUD_INIT_DISABLE_ROOT_SSH"), "true")
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_FAIL2BAN"), "true")
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES"), "true")
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK"), "true")
        self.assertEqual(env.get("CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP"), "true")
        self.assertEqual(env.get("CLOUD_INIT_USB_MAX_CURRENT_ENABLE"), "true")
        self.assertEqual(env.get("CLOUD_INIT_PCIEX1_ENABLE"), "true")
        self.assertEqual(env.get("CLOUD_INIT_PCIEX1_GEN3"), "true")
        self.assertEqual(env.get("CLOUD_INIT_SSH_PORT"), "2222")
        self.assertEqual(env.get("CLOUD_INIT_SSH_ALLOW_GROUPS"), "sudo,wheel")
        self.assertEqual(
            env.get("CLOUD_INIT_EXTRA_SSH_KEYS"),
            "ssh-ed25519 AAAA111 extra1\nssh-ed25519 AAAA222 extra2",
        )

        with callback_app.JOBS_LOCK:
            self.assertEqual(callback_app.JOBS[job_id]["status"], "completed")
            callback_app.JOBS.pop(job_id, None)

    def test_run_prepare_job_respects_disabled_power_restore_callback(self) -> None:
        class FakePopen:
            def __init__(self, *args, **kwargs) -> None:
                self.returncode = 0
                self.stdout = iter(())

            def wait(self) -> int:
                return self.returncode

        job_id = "job-power-restore-disabled"
        form = {
            "profile": "pi5",
            "hostname": "rack-sec-02",
            "device": "/dev/sdb",
            "ssh_public_key": "ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQABAAABAQC test",
            "callback_url": "http://127.0.0.1:8088/api/cloud-init/callback",
            "distribution": "standard",
            "cloud_init_options": {
                "enable_power_restore_callback": False,
            },
        }

        with callback_app.JOBS_LOCK:
            callback_app.JOBS[job_id] = {
                "status": "queued",
                "created_at": "2026-01-01T00:00:00+00:00",
                "request": dict(form),
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }

        popen_call = {}

        def fake_popen(*args, **kwargs):
            popen_call.update(kwargs)
            return FakePopen()

        with patch("core.os.geteuid", return_value=0), patch(
            "core._archive_ssh_public_key", return_value={"archive_path": "test-archive"}
        ), patch("core.subprocess.Popen", side_effect=fake_popen), patch("core._record_image_history"):
            callback_core._run_prepare_job(job_id, form)

        env = popen_call.get("env") or {}
        self.assertEqual(env.get("CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK"), "false")

        with callback_app.JOBS_LOCK:
            self.assertEqual(callback_app.JOBS[job_id]["status"], "completed")
            callback_app.JOBS.pop(job_id, None)

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

    def test_settings_rack_name_roundtrip(self) -> None:
        current = self.client.get("/api/settings")
        self.assertEqual(current.status_code, 200)
        current_payload = current.get_json()
        self.assertTrue(current_payload["ok"])
        self.assertIn("rack_name", current_payload.get("settings", {}))

        updated = self.client.post("/api/settings", json={"rack_name": "Rack Alpha 01"})
        self.assertEqual(updated.status_code, 200)
        updated_payload = updated.get_json()
        self.assertTrue(updated_payload["ok"])
        self.assertEqual(updated_payload.get("settings", {}).get("rack_name"), "rack-alpha-01")

        reloaded = self.client.get("/api/settings")
        self.assertEqual(reloaded.status_code, 200)
        self.assertEqual(reloaded.get_json().get("settings", {}).get("rack_name"), "rack-alpha-01")

    def test_settings_tailscale_framework_roundtrip_and_redaction(self) -> None:
        updated = self.client.post(
            "/api/settings",
            json={
                "tailscale_tailnet": "LabTailNet.ts.net",
                "tailscale_api_key": "tskey-api-example",
            },
        )
        self.assertEqual(updated.status_code, 200)
        updated_payload = updated.get_json()
        self.assertTrue(updated_payload["ok"])
        self.assertEqual(updated_payload.get("settings", {}).get("tailscale_tailnet"), "labtailnet.ts.net")
        self.assertEqual(updated_payload.get("settings", {}).get("tailscale_api_key"), "")
        self.assertTrue(updated_payload.get("settings", {}).get("tailscale_api_key_set"))

        reloaded = self.client.get("/api/settings")
        self.assertEqual(reloaded.status_code, 200)
        reloaded_payload = reloaded.get_json()
        self.assertEqual(reloaded_payload.get("settings", {}).get("tailscale_tailnet"), "labtailnet.ts.net")
        self.assertEqual(reloaded_payload.get("settings", {}).get("tailscale_api_key"), "")
        self.assertTrue(reloaded_payload.get("settings", {}).get("tailscale_api_key_set"))

    def test_tailscale_framework_status_endpoint(self) -> None:
        response = self.client.get("/api/tailscale/framework-status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        status = payload.get("status", {})
        self.assertIn("library_available", status)
        self.assertIn("tailnet", status)
        self.assertIn("api_key_set", status)
        self.assertIn("client_ready", status)

    def test_logging_tail_works_with_default_lines(self) -> None:
        response = self.client.get("/api/logging/tail")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("lines", payload)

    def test_logging_tail_rejects_invalid_lines_value(self) -> None:
        response = self.client.get("/api/logging/tail?lines=not-a-number")
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("lines must be an integer", payload.get("error", ""))

    def test_fragment_endpoints_render(self) -> None:
        for path in (
            "/fragments/cloud-init/event-rows",
            "/fragments/sd/image-catalog-rows",
            "/fragments/sd/images-rows",
            "/fragments/tailscale-acl/overview",
            "/fragments/tailscale-acl/inputs",
            "/fragments/tailscale-acl/output",
            "/fragments/tailscale-acl/apply-guide",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, msg=f"expected 200 for {path}")

    def test_tailscale_acl_page_lazy_sections(self) -> None:
        response = self.client.get("/tailscale-acl")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('hx-get="/fragments/tailscale-acl/overview"', text)
        self.assertIn('hx-get="/fragments/tailscale-acl/inputs"', text)
        self.assertIn('hx-get="/fragments/tailscale-acl/output"', text)
        self.assertIn('hx-get="/fragments/tailscale-acl/apply-guide"', text)

    def test_tailscale_acl_inputs_use_dashboard_settings_defaults(self) -> None:
        updated = self.client.post(
            "/api/settings",
            json={"rack_name": "Rack Prime", "tailscale_tailnet": "LabTailNet.ts.net"},
        )
        self.assertEqual(updated.status_code, 200)

        response = self.client.get("/fragments/tailscale-acl/inputs")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('id="rack_name" value="rack-prime"', text)
        self.assertIn('id="tailnet" value="labtailnet.ts.net"', text)

    def test_tailscale_acl_inputs_show_last_used_draft_hint(self) -> None:
        response = self.client.get("/fragments/tailscale-acl/inputs")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("acl-draft-status", text)
        self.assertIn("Using the default ACL suggestions", text)

    def test_image_catalog_is_yaml_backed_and_has_intended_urls(self) -> None:
        entries = {entry.get("id"): entry for entry in callback_core.IMAGE_CATALOG}
        expected_files = {
            "raspios-trixie-arm64": "2026-06-18-raspios-trixie-arm64.img.xz",
            "raspios-trixie-armhf": "2026-06-18-raspios-trixie-armhf.img.xz",
            "raspios-bookworm-arm64-lite": "2026-06-18-raspios-bookworm-arm64-lite.img.xz",
            "raspios-bookworm-armhf-lite": "2026-06-18-raspios-bookworm-armhf-lite.img.xz",
            "ubuntu-24.04-pi-arm64": "ubuntu-24.04.2-preinstalled-server-arm64+raspi.img.xz",
            "ubuntu-22.04-pi-arm64": "ubuntu-22.04.4-preinstalled-server-arm64+raspi.img.xz",
            "k3s-ubuntu-24.04-pi-arm64": "ubuntu-24.04.2-preinstalled-server-arm64+raspi.img.xz",
        }

        for entry_id, expected_suffix in expected_files.items():
            self.assertIn(entry_id, entries)
            entry = entries[entry_id]
            self.assertTrue(entry.get("image_url"), msg=f"missing image_url for {entry_id}")
            self.assertTrue(entry["image_url"].endswith(expected_suffix))
            self.assertEqual(
                callback_core._safe_catalog_filename(entry["image_url"], entry_id),
                f"{entry_id}-{expected_suffix}",
            )

    def test_images_page_has_cache_path_color_hooks(self) -> None:
        with self.client.get("/images") as response:
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
        with self.client.get("/static/images.js") as assets:
            self.assertEqual(assets.status_code, 200)
            script = assets.get_data(as_text=True)
        self.assertIn("cache-path-ok", script)
        self.assertIn("cache-path-missing", script)
        self.assertIn("source-panel", text)
        self.assertIn("Distro image presets", text)
        self.assertIn("download-state available", script)
        self.assertIn("download-state missing", script)

    def test_image_crud_and_fragment_rows(self) -> None:
        created = self.client.post(
            "/api/sd/images",
            json={
                "label": "Ubuntu 24.04 LTS",
                "profile": "pi5",
                "source": "image_url",
                "source_value": "https://example.invalid/image.img.xz",
                "download_path": "/tmp/image.img.xz",
                "hash_status": "verified",
                "expected_sha256": "abc123",
                "observed_sha256": "abc123",
                "job_status": "completed",
                "exit_code": 0,
                "notes": "seed record",
            },
        )
        self.assertEqual(created.status_code, 201)
        created_payload = created.get_json()
        self.assertTrue(created_payload["ok"])
        record_id = created_payload["record"]["id"]

        listing = self.client.get("/api/sd/images")
        self.assertEqual(listing.status_code, 200)
        listed_entries = listing.get_json()["entries"]
        self.assertTrue(any(entry["id"] == record_id for entry in listed_entries))

        updated = self.client.put(
            f"/api/sd/images/{record_id}",
            json={"label": "Ubuntu 24.04 LTS Updated", "notes": "edited record"},
        )
        self.assertEqual(updated.status_code, 200)
        updated_payload = updated.get_json()
        self.assertEqual(updated_payload["record"]["label"], "Ubuntu 24.04 LTS Updated")
        self.assertEqual(updated_payload["record"]["notes"], "edited record")

        rows = self.client.get("/fragments/sd/images-rows")
        self.assertEqual(rows.status_code, 200)
        self.assertIn("Ubuntu 24.04 LTS Updated", rows.get_data(as_text=True))

        deleted = self.client.delete(f"/api/sd/images/{record_id}")
        self.assertEqual(deleted.status_code, 200)
        after_delete = self.client.get("/api/sd/images")
        self.assertFalse(any(entry["id"] == record_id for entry in after_delete.get_json()["entries"]))

    def test_image_relative_download_path_availability(self) -> None:
        rel_dir = TEST_DATA_DIR / "cache" / "images"
        rel_dir.mkdir(parents=True, exist_ok=True)
        rel_file = rel_dir / "example.img.xz"
        rel_file.write_text("dummy", encoding="utf-8")

        created = self.client.post(
            "/api/sd/images",
            json={
                "label": "Relative Path Image",
                "profile": "pi5",
                "source": "catalog_pull",
                "source_value": "https://example.invalid/example.img.xz",
                "download_path": "cache/images/example.img.xz",
                "hash_status": "verified",
                "job_status": "completed",
                "exit_code": 0,
            },
        )
        self.assertEqual(created.status_code, 201)
        record_id = created.get_json()["record"]["id"]

        listing = self.client.get("/api/sd/images")
        self.assertEqual(listing.status_code, 200)
        rows = listing.get_json().get("entries", [])
        match = [row for row in rows if row.get("id") == record_id]
        self.assertTrue(match)
        self.assertTrue(match[0].get("local_available"))

    def test_catalog_pull_api_validation(self) -> None:
        empty = self.client.post("/api/sd/catalog-pull", json={"catalog_ids": []})
        self.assertEqual(empty.status_code, 400)
        self.assertFalse(empty.get_json()["ok"])

        no_url = self.client.post("/api/sd/catalog-pull", json={"catalog_ids": ["custom-arm64"]})
        self.assertEqual(no_url.status_code, 400)
        no_url_payload = no_url.get_json()
        self.assertFalse(no_url_payload["ok"])
        self.assertIn("custom-arm64", no_url_payload.get("missing_url_ids", []))

        not_found = self.client.get("/api/sd/catalog-pull/jobs/does-not-exist")
        self.assertEqual(not_found.status_code, 404)
        self.assertFalse(not_found.get_json()["ok"])

    def test_catalog_pull_worker_completes_and_records_progress(self) -> None:
        job_id = "job-test-catalog"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }

        def fake_download(url: str, target: Path, on_progress=None) -> str:
            if callable(on_progress):
                on_progress(4, 8)
            target.write_text("payload", encoding="utf-8")
            return "abc123"

        with callback_core.CATALOG_PULL_JOBS_LOCK:
            callback_core.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        with patch.object(callback_core, "_download_catalog_image", side_effect=fake_download), patch.object(
            callback_core,
            "_append_image_history",
            return_value=None,
        ):
            callback_core._run_catalog_pull_job(job_id, [entry], skip_existing=False)

        with callback_core.CATALOG_PULL_JOBS_LOCK:
            job = callback_core.CATALOG_PULL_JOBS[job_id]

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["summary"]["ok"], 1)
        self.assertEqual(job["summary"]["running"], 0)
        self.assertEqual(job["summary"]["total"], 1)
        self.assertEqual(job["results"][0]["status"], "ok")

    def test_catalog_pull_worker_skips_stale_unwritable_part_file(self) -> None:
        job_id = "job-test-catalog-stale-part"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }
        target = callback_core.IMAGE_CACHE_DIR / callback_core._safe_catalog_filename(str(entry["image_url"]), str(entry["id"]))
        stale_tmp = target.with_suffix(target.suffix + ".part")
        stale_tmp.parent.mkdir(parents=True, exist_ok=True)
        stale_tmp.write_text("stale", encoding="utf-8")
        stale_tmp.chmod(0o444)

        def fake_download(url: str, target_path: Path, on_progress=None) -> str:
            if callable(on_progress):
                on_progress(4, 8)
            target_path.write_text("payload", encoding="utf-8")
            return "abc123"

        with callback_core.CATALOG_PULL_JOBS_LOCK:
            callback_core.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        try:
            with patch.object(callback_core, "_download_catalog_image", side_effect=fake_download), patch.object(
                callback_core,
                "_append_image_history",
                return_value=None,
            ):
                callback_core._run_catalog_pull_job(job_id, [entry], skip_existing=False)

            with callback_core.CATALOG_PULL_JOBS_LOCK:
                job = callback_core.CATALOG_PULL_JOBS[job_id]

            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["summary"]["ok"], 1)
            self.assertEqual(job["results"][0]["status"], "ok")
            self.assertTrue(target.exists())
        finally:
            if target.exists():
                target.unlink()
            if stale_tmp.exists():
                stale_tmp.chmod(0o644)
                stale_tmp.unlink()

    def test_catalog_pull_worker_removes_stale_part_files_before_download(self) -> None:
        job_id = "job-test-catalog-stale-part-cleanup"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }
        target = callback_core.IMAGE_CACHE_DIR / callback_core._safe_catalog_filename(str(entry["image_url"]), str(entry["id"]))
        stale_tmp = target.with_suffix(target.suffix + ".part")
        stale_tmp.parent.mkdir(parents=True, exist_ok=True)
        stale_tmp.write_text("stale", encoding="utf-8")

        def fake_download(url: str, target_path: Path, on_progress=None) -> str:
            if callable(on_progress):
                on_progress(4, 8)
            target_path.write_text("payload", encoding="utf-8")
            return "abc123"

        with callback_core.CATALOG_PULL_JOBS_LOCK:
            callback_core.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        try:
            with patch.object(callback_core, "_download_catalog_image", side_effect=fake_download), patch.object(
                callback_core,
                "_append_image_history",
                return_value=None,
            ):
                callback_core._run_catalog_pull_job(job_id, [entry], skip_existing=False)

            self.assertFalse(stale_tmp.exists())
            self.assertTrue(target.exists())
        finally:
            if target.exists():
                target.unlink()
            if stale_tmp.exists():
                stale_tmp.unlink()

    def test_catalog_pull_worker_initializes_running_summary_before_download(self) -> None:
        job_id = "job-test-catalog-running-summary"
        entry = {
            "id": "ubuntu-22.04-pi-arm64",
            "label": "Ubuntu Server 22.04 LTS",
            "image_url": "https://example.invalid/ubuntu-22.04.img.xz",
            "image_sha256": "",
            "profiles": ["pi5"],
        }
        observed_summary: dict[str, int] = {}

        def fake_download(url: str, target_path: Path, on_progress=None) -> str:
            with callback_core.CATALOG_PULL_JOBS_LOCK:
                job = callback_core.CATALOG_PULL_JOBS[job_id]
                observed_summary.update(job.get("summary", {}))
            if callable(on_progress):
                on_progress(4, 8)
            target_path.write_text("payload", encoding="utf-8")
            return "abc123"

        with callback_core.CATALOG_PULL_JOBS_LOCK:
            callback_core.CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": "2026-06-29T00:00:00+00:00",
                "storage_dir": str(TEST_DATA_DIR / "image_cache"),
                "requested_ids": [entry["id"]],
                "accepted_ids": [entry["id"]],
                "missing_ids": [],
                "missing_url_ids": [],
                "skip_existing": False,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0},
            }

        try:
            with patch.object(callback_core, "_download_catalog_image", side_effect=fake_download), patch.object(
                callback_core,
                "_append_image_history",
                return_value=None,
            ):
                callback_core._run_catalog_pull_job(job_id, [entry], skip_existing=False)

            self.assertEqual(observed_summary.get("running"), 1)
            self.assertEqual(observed_summary.get("total"), 1)
        finally:
            target = callback_core.IMAGE_CACHE_DIR / callback_core._safe_catalog_filename(str(entry["image_url"]), str(entry["id"]))
            if target.exists():
                target.unlink()

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
        text = page.get_data(as_text=True)
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
        with patch("core._apply_node_provisioning", return_value={"node": "auto-check-node", "results": []}) as apply_mock:
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
        self.assertEqual(apply_mock.call_args.kwargs["mode"], "check")
        self.assertTrue(apply_mock.call_args.kwargs["run_ansible"])

    def test_initial_callback_triggers_auto_closer_check_for_runcmd(self) -> None:
        with patch("core._apply_node_provisioning", return_value={"node": "auto-check-node", "results": []}) as apply_mock:
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
        self.assertEqual(apply_mock.call_args.kwargs["mode"], "check")
        self.assertTrue(apply_mock.call_args.kwargs["run_ansible"])

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
        with patch("app._store_node_report", side_effect=PermissionError("readonly node reports")):
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

        state = callback_core._load_node_provisioning()
        node_results = state.get("results", {}).get(node_name, {})
        self.assertTrue(node_results)
        self.assertTrue(all(item["status"] == "planned" for item in node_results.values()))

    def test_group_vars_all_vault_file_is_plain_yaml(self) -> None:
        vault_path = Path(__file__).resolve().parents[2] / "group_vars" / "all" / "vault.yml"
        self.assertTrue(vault_path.exists())
        vault_text = vault_path.read_text(encoding="utf-8")
        self.assertNotIn("$ANSIBLE_VAULT", vault_text)
        self.assertIn("vault_maas_api_key", vault_text)

    def test_run_node_profile_uses_address_based_temp_inventory(self) -> None:
        with patch.object(callback_core.logger, "info"), patch.object(callback_core.shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            callback_core.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch.object(
            callback_core,
            "_ssh_private_key_preflight",
            return_value="",
        ):
            result = callback_core._run_node_profile(
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
        self.assertNotEqual(inventory_path, str(callback_core.INVENTORY_FILE))
        self.assertTrue(Path(inventory_path).exists())
        inventory_text = Path(inventory_path).read_text(encoding="utf-8")
        self.assertIn("192.0.2.10", inventory_text)
        self.assertIn("he-test-node-1-aug-7", inventory_text)
        self.assertIn("ansible_user: jdizzle", inventory_text)
        self.assertIn("ansible_ssh_private_key_file", inventory_text)
        self.assertIn("rack-bootstrap-ed25519", inventory_text)
        self.assertIn("PreferredAuthentications=publickey", inventory_text)

    def test_run_node_profile_nests_active_group_under_family_group(self) -> None:
        with patch.object(callback_core.logger, "info"), patch.object(callback_core.shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            callback_core.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch.object(
            callback_core,
            "_ssh_private_key_preflight",
            return_value="",
        ):
            callback_core._run_node_profile(
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

    def test_pi_roles_tag_tasks_for_profile_scoped_runs(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        for role in ("raspberry_pi5", "raspberry_pi_3bplus"):
            tasks_text = (repo_root / "roles" / role / "tasks" / "main.yml").read_text(encoding="utf-8")
            self.assertIn("- tailscale", tasks_text, msg=f"{role} must tag tailscale tasks")
            self.assertIn("- baseline", tasks_text, msg=f"{role} must tag baseline tasks")
            self.assertIn("Resolve Tailscale provisioning inputs", tasks_text, msg=f"{role} must consume provision_inputs")

    def test_run_node_profile_uses_appdata_backed_ssh_key_path(self) -> None:
        with patch.object(callback_core.logger, "info"), patch.object(callback_core.shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            callback_core.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch.object(
            callback_core,
            "_ssh_private_key_preflight",
            return_value="",
        ):
            callback_core._run_node_profile(
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
        expected_key_path = str(callback_core.KEYS_DIR / "rack-bootstrap-ed25519")
        self.assertIn(expected_key_path, inventory_text)
        self.assertNotIn("/home/jdizzle/Playbook/callback_site/data/ssh_keys/rack-bootstrap-ed25519", inventory_text)

    def test_run_node_profile_logs_start_and_completion(self) -> None:
        with patch.object(callback_core.logger, "info") as log_info, patch.object(callback_core.shutil, "which", return_value="/tmp/fake-ansible"), patch.object(
            callback_core.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="ok\n", stderr=""),
        ) as run_mock, patch.object(
            callback_core,
            "_ssh_private_key_preflight",
            return_value="",
        ):
            result = callback_core._run_node_profile(
                "node-01",
                {"id": "docker", "tags": ["docker"]},
                mode="apply",
                run_ansible=True,
                profile_inputs={},
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(run_mock.call_args.kwargs["stdin"], callback_core.subprocess.DEVNULL)
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
        with patch.object(callback_core, "_profile_map", return_value={"baseline": {"id": "baseline", "tags": ["baseline"]}}), patch.object(
            callback_core,
            "_load_node_provisioning",
            return_value=fake_state,
        ), patch.object(callback_core, "_node_profile_inputs", return_value={}), patch.object(
            callback_core,
            "_node_ops_inventory_view",
            return_value=[{"name": "node-01", "group": "rpi5_active", "address": "192.0.2.10"}],
        ), patch.object(
            callback_core,
            "_run_node_profile",
            return_value={
                "profile_id": "baseline",
                "status": "ok",
                "mode": "check",
                "checked_at": "2026-01-01T00:00:00+00:00",
                "output": "ok",
            },
        ), patch.object(
            callback_core,
            "_run_hardware_check_callback",
            return_value={
                "profile_id": "hardware_check_trigger",
                "status": "ok",
                "mode": "check",
                "checked_at": "2026-01-01T00:00:01+00:00",
                "output": "triggered",
            },
        ) as trigger_mock, patch.object(callback_core, "_save_node_provisioning"):
            result = callback_core._apply_node_provisioning("node-01", ["baseline"], mode="check", run_ansible=True)

        self.assertEqual(result["node"], "node-01")
        self.assertEqual(result["mode"], "check")
        self.assertTrue(any(item.get("profile_id") == "hardware_check_trigger" for item in result["results"]))
        trigger_mock.assert_called_once()

    def test_run_hardware_check_callback_fails_fast_when_private_key_unreadable(self) -> None:
        with patch.object(
            callback_core,
            "_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-01",
                    "group": "rpi5_active",
                    "address": "192.0.2.10",
                    "node_username": "jdizzle",
                    "certificate": "rack-bootstrap-ed25519",
                }
            ],
        ), patch.object(callback_core, "_ssh_private_key_preflight", return_value="SSH private key is not readable"):
            result = callback_core._run_hardware_check_callback("node-01", node_group="rpi5_active", node_address="192.0.2.10")

        self.assertEqual(result.get("profile_id"), "hardware_check_trigger")
        self.assertEqual(result.get("status"), "failed")
        self.assertIn("not readable", str(result.get("output", "")))

    def test_run_hardware_check_callback_uses_ansible_setup_facts(self) -> None:
        with patch.object(
            callback_core,
            "_node_ops_inventory_view",
            return_value=[
                {
                    "name": "node-01",
                    "group": "rpi5_active",
                    "address": "192.0.2.10",
                    "node_username": "jdizzle",
                    "certificate": "rack-bootstrap-ed25519",
                }
            ],
        ), patch.object(callback_core, "_ssh_private_key_preflight", return_value=""), patch.object(
            callback_core,
            "_seed_known_host",
        ), patch.object(
            callback_core,
            "_default_callback_url",
            return_value="http://10.255.45.164:8088/api/cloud-init/callback",
        ), patch.object(
            callback_core.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout='{"ansible_facts": {"ansible_memtotal_mb": 2048, "ansible_product_name": "RPi 5"}}', stderr=""),
        ) as run_mock:
            result = callback_core._run_hardware_check_callback("node-01", node_group="rpi5_active", node_address="192.0.2.10")

        self.assertEqual(result.get("status"), "ok")
        command_texts = [" ".join(str(part) for part in call.args[0]) for call in run_mock.call_args_list]
        setup_cmd = next((text for text in command_texts if "ansible.builtin.setup" in text), "")
        self.assertTrue(setup_cmd, f"Expected ansible setup command in calls: {command_texts}")
        self.assertNotIn("ansible.builtin.shell", setup_cmd)
        self.assertNotIn("python3 - <<'PY'", setup_cmd)

    def test_preferred_node_connect_target_avoids_transit_inventory_when_callback_is_usable(self) -> None:
        target, source = callback_core._preferred_node_connect_target(
            "node-01",
            "10.255.45.243",
            "192.168.2.20",
        )
        self.assertEqual(target, "192.168.2.20")
        self.assertEqual(source, "callback")

    def test_preferred_node_connect_target_uses_recent_callback_over_inventory(self) -> None:
        target, source = callback_core._preferred_node_connect_target(
            "node-01",
            "192.168.2.99",
            "192.168.2.20",
        )
        self.assertEqual(target, "192.168.2.20")
        self.assertEqual(source, "callback")

    def test_preferred_node_connect_target_prefers_inventory_when_callback_not_usable(self) -> None:
        target, source = callback_core._preferred_node_connect_target(
            "node-01",
            "192.168.2.99",
            "8.8.8.8",
        )
        self.assertEqual(target, "192.168.2.99")
        self.assertEqual(source, "inventory")

    def test_run_hardware_check_callback_failed_output_includes_target_source_and_auth_hint(self) -> None:
        failure_output = "".join(
            [
                "Pseudo-terminal will not be allocated because stdin is not a terminal.\n",
                "Warning: Connection is not using a post-quantum key exchange algorithm.\n",
                'node-01 | UNREACHABLE! => {"changed": false, "msg": "Failed to connect to the host via ssh: Permission denied (publickey,password)", "unreachable": true}',
            ]
        )
        with patch.object(
            callback_core,
            "_node_ops_inventory_view",
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
        ), patch.object(callback_core, "_ssh_private_key_preflight", return_value=""), patch.object(
            callback_core,
            "_seed_known_host",
        ), patch.object(
            callback_core.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=4, stdout="", stderr=failure_output),
        ):
            result = callback_core._run_hardware_check_callback("node-01", node_group="rpi5_active", node_address="10.255.45.243")

        self.assertEqual(result.get("status"), "failed")
        text = str(result.get("output", ""))
        self.assertIn("SSH authentication failed", text)
        self.assertIn("post-quantum warning is informational", text)
        self.assertIn("Connect target source: callback", text)

    def test_latest_hardware_by_host_keeps_most_recent_event(self) -> None:
        older = {
            "stage": "hardware-check",
            "received_at": "2025-01-01T00:00:00+00:00",
            "hostname": "node-01",
            "fqdn": "node-01.example",
            "raw": {"hardware": {"model": "old", "ram_mb": 512, "nvme_capacity_gb": 0}},
        }
        newer = {
            "stage": "hardware-check",
            "received_at": "2025-01-01T00:05:00+00:00",
            "hostname": "node-01",
            "fqdn": "node-01.example",
            "raw": {"hardware": {"model": "new", "ram_mb": 2048, "nvme_capacity_gb": 953}},
        }
        with patch.object(callback_core, "_read_events", return_value=[older, newer]):
            result = callback_core._latest_hardware_by_host(limit=10)
        self.assertEqual(result["node-01"]["model"], "new")
        self.assertEqual(result["node-01"]["ram_mb"], 2048)
        self.assertEqual(result["node-01"]["nvme_capacity_gb"], 953)

    def test_nodes_telegraf_test_uses_current_profile_inputs(self) -> None:
        summary = self.client.get("/api/nodes/summary")
        self.assertEqual(summary.status_code, 200)
        nodes = summary.get_json().get("nodes") or []
        if not nodes:
            self.skipTest("No active nodes in inventory for telegraf test endpoint")

        node_name = nodes[0]["name"]

        with patch.object(
            callback_app,
            "_telegraf_influx_dummy_check",
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

    def test_prepare_script_escapes_embedded_runtime_vars(self) -> None:
        script_text = (APP_DIR.parent / "scripts" / "prepare_sd_cloud_init.sh").read_text(encoding="utf-8")
        self.assertIn('\\$PI_MODEL', script_text)
        self.assertIn('\\$RAM_MB', script_text)
        self.assertIn('PAYLOAD_JSON="\\$(PI_MODEL=\\"\\$PI_MODEL\\"', script_text)
        self.assertIn("python3 <<'PY'", script_text)
        self.assertIn('print(json.dumps(payload, separators=(",", ":")))', script_text)
        self.assertIn('--data-binary "\\$PAYLOAD_JSON"', script_text)
        self.assertIn('SOURCE_IP="\\$(/usr/local/bin/rack-local-ip', script_text)
        self.assertIn('PAYLOAD_JSON="\\$(INSTANCE_ID="\\${INSTANCE_ID:-\\$(hostname)}"', script_text)
        self.assertIn('if [[ -n "\\${CALLBACK_URL:-}" ]]; then', script_text)
        self.assertIn('-d "\\${PAYLOAD_JSON}" "\\${CALLBACK_URL}" || true', script_text)
        self.assertIn('INFLUX_URL="\\${CLOUD_INIT_TELEGRAF_INFLUX_URL:-}"', script_text)
        self.assertIn('CLOUD_INIT_USERNAME="${CLOUD_INIT_USERNAME:-jdizzle}"', script_text)
        self.assertIn('CLOUD_INIT_USER_PASSWORD="${CLOUD_INIT_USER_PASSWORD:-}"', script_text)
        self.assertIn('CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH="${CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH:-false}"', script_text)
        self.assertIn('CLOUD_INIT_DISABLE_ROOT_SSH="${CLOUD_INIT_DISABLE_ROOT_SSH:-false}"', script_text)
        self.assertIn('CLOUD_INIT_BOOT_SUMMARY="${CLOUD_INIT_BOOT_SUMMARY:-true}"', script_text)
        self.assertIn('CLOUD_INIT_ENABLE_FAIL2BAN="${CLOUD_INIT_ENABLE_FAIL2BAN:-false}"', script_text)
        self.assertIn('CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES="${CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES:-false}"', script_text)
        self.assertIn('CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK="${CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK:-true}"', script_text)
        self.assertIn('validate_http_url "--callback-url" "$CALLBACK_URL"', script_text)
        self.assertIn('validate_ssh_public_key_lines "--ssh-key-file" "$SSH_KEY_CONTENT"', script_text)
        self.assertIn('Invalid CLOUD_INIT_USERNAME value', script_text)
        self.assertIn('Invalid CLOUD_INIT_SSH_PORT value', script_text)
        self.assertIn('Invalid CLOUD_INIT_SSH_ALLOW_GROUPS value', script_text)
        self.assertIn('validate_ssh_public_key_lines "CLOUD_INIT_EXTRA_SSH_KEYS" "$CLOUD_INIT_EXTRA_SSH_KEYS"', script_text)
        self.assertIn('role=worker', script_text)
        self.assertIn('/etc/fail2ban/jail.d/rack-sshd.local', script_text)
        self.assertIn('systemctl enable --now fail2ban || true', script_text)
        self.assertIn('PermitRootLogin no', script_text)
        self.assertIn('PasswordAuthentication no', script_text)
        self.assertIn("if [ -f /home/${CLOUD_INIT_USERNAME}/.ssh/authorized_keys.d/rack-extra.keys ]", script_text)
        self.assertIn('require_cmd openssl', script_text)
        self.assertIn("openssl passwd -6 -stdin", script_text)
        self.assertIn('--retry 5 --retry-delay 2 --retry-connrefused --retry-all-errors --connect-timeout 5 --max-time 20', script_text)
        self.assertIn('rack callback POST failed after retries to ${CALLBACK_URL}', script_text)
        self.assertIn('if [[ "$CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK" == "true" ]]; then', script_text)
        self.assertIn('if [[ "\\${CLOUD_INIT_BOOT_SUMMARY}" != "true" ]]; then', script_text)
        self.assertIn('printf \'%s+--------------------------------------------------------------+%s\\n\' "\\$cyan" "\\$reset"', script_text)
        self.assertIn('print_row "\\$red" "Hostname" "\\$(hostname)"', script_text)
        self.assertIn('/usr/local/bin/rack-bootstrap-summary', script_text)
        self.assertIn('VALIDATE_CLOUD_INIT_SCHEMA="${CLOUD_INIT_VALIDATE_SCHEMA:-true}"', script_text)
        self.assertIn('cloud-init schema --config-file "$USER_DATA_OUT"', script_text)
        self.assertIn("cloud-init devel schema --config-file \"$USER_DATA_OUT\"", script_text)
        self.assertIn('usb_max_current_enable=1', script_text)
        self.assertIn('cat >> "$USER_DATA_OUT" <<RUNCMD_EOF', script_text)
        self.assertIn('final_message: "Cloud-init finished for ${HOSTNAME}"', script_text)


if __name__ == "__main__":
    unittest.main()
