"""Settings, logging, Tailscale page defaults and shared fragments."""

from __future__ import annotations

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest


class SettingsTests(BaseCallbackSiteTest):
    def test_combined_maintenance_preserves_separate_actions_in_browser(self) -> None:
        self.browser_probe("/settings", ["settings.js"], """
let confirmed = false, fail = false, release;
const requests = [], confirmations = [];
window.confirm = message => { confirmations.push(message); return confirmed; };
window.fetch = async (url, options = {}) => {
  if (!options.method) return {ok: true, status: 200, json: async () => ({
    ok: true, settings: {}, nodes: [{name: "retired-node", archived_at: "2026-11-08T12:00:00Z"}],
    events: [], entries: [], profiles: [], state: {}, summary: {}
  })};
  requests.push({url, method: options.method, body: JSON.parse(options.body)});
  await new Promise(resolve => { release = resolve; });
  if (fail) throw new Error("network interrupted");
  return {ok: true, json: async () => ({ok: true, reset: {cleared: true}, cleared: 3})};
};
""", """
await tick();
const panel = document.querySelector("#node-state-maintenance");
const danger = document.querySelector("#destructive-maintenance");
check(panel && panel.contains(resetNodeOpsBtn) && panel.contains(clearCallbackBtn)
  && panel.contains(document.querySelector("#archived-node-list")), "all three sections share one panel");
check(!danger.open && danger.contains(resetNodeOpsBtn) && danger.contains(clearCallbackBtn),
  "destructive actions collapsed by default");
check(!danger.contains(document.querySelector("#archived-node-list")), "archive stays outside destructive controls");
check(document.querySelectorAll("#archived-nodes").length === 1, "archive anchor retained");
resetNodeOpsBtn.click();
clearCallbackBtn.click();
await tick();
check(requests.length === 0 && confirmations.length === 2, "each cancelled action sends no request");
danger.open = true;
confirmed = true;
resetNodeOpsBtn.click();
resetNodeOpsBtn.click();
check(resetNodeOpsBtn.disabled && !clearCallbackBtn.disabled && requests.length === 1,
  "reset disables only its own button and prevents duplicates");
check(requests[0].url === "/api/settings/reset" && requests[0].body.scope === "node_ops",
  "reset scope unchanged");
release();
await tick();
check(!resetNodeOpsBtn.disabled && statusBox.textContent.startsWith("Reset complete:")
  && callbackClearStatus.textContent === "Idle.", "reset completion uses its own status");
clearCallbackBtn.click();
check(requests[1].url === "/api/cloud-init/events/clear" && clearCallbackBtn.disabled,
  "backlog clearing keeps its endpoint");
release();
await tick();
check(callbackClearStatus.textContent === "Cleared events: 3" && !clearCallbackBtn.disabled,
  "callback completion separate");
fail = true;
resetNodeOpsBtn.click();
release();
await tick();
check(statusBox.textContent.includes("network interrupted") && !resetNodeOpsBtn.disabled,
  "reset network errors visible and retryable");
clearCallbackBtn.click();
release();
await tick();
check(callbackClearStatus.textContent.includes("network interrupted") && !clearCallbackBtn.disabled,
  "clear network errors visible and retryable");
fail = false;
document.querySelector("#archived-node-list button").click();
check(requests.at(-1).url === "/api/nodes/retired-node/archive"
  && requests.at(-1).method === "PUT" && requests.at(-1).body.archived === false,
  "restore remains non-destructive");
release();
await tick();
check(!requests.some(r => r.method === "DELETE"), "no permanent node deletion introduced");
""")

    def test_catalog_health_and_source_share_one_panel(self) -> None:
        text = self.client.get("/settings").get_data(as_text=True)
        start = text.index("<summary>Image Catalog Health</summary>")
        end = text.index('<details class="settings-utility">', start)
        self.assertIn("<summary>Catalog Source Of Truth</summary>", text[start:end])
        self.assertNotIn("<h2>Catalog Source Of Truth</h2>", text)

    def test_utilities_share_one_panel_and_expand_independently(self) -> None:
        self.browser_probe("/settings", ["settings.js"], """
window.fetch = async () => ({ok: true, json: async () => ({
  ok: true, settings: {rack_name: "test-rack"}, nodes: [], events: [],
  entries: [], profiles: [], state: {}, summary: {}
})});
""", """
await tick();
const panel = document.querySelector("#settings-utilities");
const sections = Array.from(panel.querySelectorAll(":scope > details"));
check(sections.length === 3 && sections.every(section => !section.open),
  "three utilities collapsed inside one panel");
check(panel.getBoundingClientRect().height < 260, "collapsed utility panel stays below 260px");
for (const id of ["rack-name", "save-rack-name", "rack-status", "check-catalog-health",
  "catalog-health-status", "enable-verbose-logging", "disable-verbose-logging",
  "view-recent-logs", "logging-status", "logging-output"]) {
  check(panel.contains(document.getElementById(id)), `existing control retained: ${id}`);
  check(document.querySelectorAll(`#${id}`).length === 1, `unique control: ${id}`);
}
sections[0].querySelector("summary").click();
check(sections[0].open && !sections[1].open && !sections[2].open, "rack expands independently");
sections[1].querySelector("summary").click();
check(sections[0].open && sections[1].open, "opening catalog preserves rack form");
check(sections[1].querySelector(":scope > details > summary").textContent === "Catalog Source Of Truth",
  "catalog source remains a nested disclosure");
check(document.querySelector("#rack-name").value === "test-rack", "rack settings still populate");
""")

    def test_image_cache_actions_require_confirmation_and_report_errors(self) -> None:
        self.browser_probe("/settings", ["settings.js"], """
let confirmed = false, fail = false, release;
const requests = [];
window.confirm = () => confirmed;
window.fetch = async (url, options = {}) => {
  if (!options.method) return {ok: true, json: async () => ({
    ok: true, settings: {}, nodes: [], events: [], entries: [], profiles: [], state: {}, summary: {}
  })};
  requests.push({url, body: JSON.parse(options.body)});
  await new Promise(resolve => { release = resolve; });
  return {ok: !fail, json: async () => fail
    ? {ok: false, error: "Image preparation is active"}
    : {ok: true, deleted_files: 2, freed_bytes: 1048576}};
};
""", """
await tick();
const danger = document.querySelector("#destructive-maintenance");
check(danger.contains(clearImageCacheBtn) && danger.contains(clearImagePartialsBtn),
  "cache actions are in destructive maintenance");
clearImageCacheBtn.click();
check(requests.length === 0, "cancel leaves cache untouched");
confirmed = true;
clearImageCacheBtn.click();
clearImagePartialsBtn.click();
check(requests.length === 1 && clearImageCacheBtn.disabled && clearImagePartialsBtn.disabled,
  "duplicate cache requests prevented");
check(requests[0].url === "/api/sd/image-cache/clear" && requests[0].body.confirm === true
  && requests[0].body.mode === "all", "explicit full-cache request");
release();
await tick();
check(imageCacheClearStatus.textContent.includes("Deleted 2") && !clearImageCacheBtn.disabled,
  "success reports freed space and enables retry");
fail = true;
clearImagePartialsBtn.click();
check(requests[1].body.mode === "partials", "partial cleanup has a separate mode");
release();
await tick();
check(imageCacheClearStatus.textContent.includes("Image preparation is active")
  && !clearImagePartialsBtn.disabled, "server refusal visible and retryable");
""")

    def test_health_endpoint(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True})

    def test_settings_page_exposes_archive_restore_controls(self) -> None:
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Archived Nodes", text)
        self.assertIn('id="archived-node-list"', text)
        self.assertIn('id="refresh-archived-nodes"', text)
        self.assertNotIn('id="run-stale-prune"', text)
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
