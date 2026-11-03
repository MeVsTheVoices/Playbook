"""Settings, logging, Tailscale page defaults and shared fragments."""

from __future__ import annotations

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest


class SettingsTests(BaseCallbackSiteTest):
    def test_health_endpoint(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

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
