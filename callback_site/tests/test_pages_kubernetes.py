from __future__ import annotations

from pathlib import Path
import html as html_lib
import json
import re
import shutil
import subprocess
import tempfile
from unittest.mock import patch

try:
    from .test_support import BaseCallbackSiteTest
except ImportError:
    from test_support import BaseCallbackSiteTest
import routes.monitor
import services.kubernetes
try:
    from .test_support import patch_in
except ImportError:
    from test_support import patch_in


class KubernetesPageTests(BaseCallbackSiteTest):
    def test_older_callback_rows_remain_compact_in_browser(self) -> None:
        from app import app as flask_app

        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for callback row height validation")
        root = Path(__file__).resolve().parents[1]
        events = [
            {
                "hostname": host, "profile": "network-toolkit", "stage": stage,
                "received_at": f"2026-11-07T12:00:0{3 - index}+00:00",
                "instance_id": "toolkit-instance", "fqdn": f"{host}.example.ts.net",
                "source_ip": "100.100.1.2", "source_ip_source": "payload",
                "cert_present": True, "cert_subject": "CN=toolkit.example.ts.net",
                "cert_fingerprint": "ab:" * 32,
            }
            for index, (host, stage) in enumerate(
                (("toolkit-01", "ready"), ("toolkit-02", "ready"), ("toolkit-01", "hardware-check"))
            )
        ]
        with flask_app.test_request_context("/"):
            rows = flask_app.jinja_env.get_template("fragments/callback_rows.html").render(events=events)
        script = (root / "static/home/events.js").read_text()
        css = (root / "static/home.css").read_text() + (root / "static/common.css").read_text()
        probe = """
const measurements = [];
function measure(label) {
  const rows = [...eventsBodyEl.querySelectorAll("tr.event-row-new")];
  measurements.push({
    label, heights: rows.map(row => row.getBoundingClientRect().height),
    condensed: rows.map(row => row.classList.contains("event-row-condensed")),
    noteHidden: rows[1] && rows[1].dataset.older ? getComputedStyle(rows[1].querySelector(".payload-kv")).display === "none" : null,
  });
}
renderEvents(allEvents);
eventsBodyEl.querySelector(".event-host-toggle").click();
eventsBodyEl.querySelector(".event-stage-toggle").click();
measure("initial");
renderEvents(allEvents);
measure("refresh");
eventsBodyEl.querySelector(".event-detail-toggle").click();
measure("expanded");
eventsBodyEl.querySelector(".event-detail-toggle").click();
measure("recondensed");
toggleOlderEvents();
measure("all-expanded");
toggleOlderEvents();
measure("all-condensed");
document.body.innerHTML = '<pre id="row-result"></pre>';
document.getElementById("row-result").textContent = JSON.stringify(measurements);
"""
        fixture = f"""<!doctype html><style>{css}</style>
<div class="table-wrap"><table class="events-table"><tbody id="events-body">{rows}</tbody></table></div>
<div id="payload-inspect-panel"></div><div id="payload-inspect-meta"></div>
<pre id="payload-inspect-json"></pre><button id="events-toggle-older"></button>
<script>
localStorage.removeItem("callback-older-expanded");
const eventsBodyEl = document.getElementById("events-body");
const eventSearchEl = {{value:""}}, eventProfileEl = {{value:""}}, eventStageEl = {{value:""}};
const allEvents = {json.dumps(events)};
{script}
{probe}
</script>"""
        with tempfile.TemporaryDirectory(prefix="callback-row-heights-") as tmp:
            for width in (390, 1440):
                page = Path(tmp) / "rows.html"
                page.write_text(
                    f'<!doctype html><iframe style="width:{width}px;height:1000px;border:0" '
                    'onload="document.body.appendChild(this.contentDocument.getElementById(\'row-result\').cloneNode(true))" '
                    f'srcdoc="{html_lib.escape(fixture, quote=True)}"></iframe>'
                )
                result = subprocess.run(
                    [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                     "--disable-background-networking", f"--user-data-dir={tmp}/chrome",
                     "--dump-dom", page.as_uri()],
                    text=True, capture_output=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                match = re.search(r'<pre id="row-result">(.*?)</pre>', result.stdout, re.DOTALL)
                self.assertIsNotNone(match, result.stdout)
                measurements = json.loads(html_lib.unescape(match.group(1)))
                for measured in measurements:
                    with self.subTest(width=width, phase=measured["label"]):
                        if measured["label"] == "all-condensed":
                            self.assertEqual(measured["condensed"], [False, False])
                            continue
                        compact = measured["label"] not in ("expanded", "all-expanded")
                        self.assertEqual(measured["condensed"], [False, compact, False])
                        self.assertEqual(measured["noteHidden"], compact)
                        if compact:
                            self.assertLessEqual(measured["heights"][1], 24)
                            self.assertLess(measured["heights"][1], measured["heights"][0] * 0.65)
                        else:
                            self.assertGreater(measured["heights"][1], 24)

    def test_callback_history_groups_by_host_and_type(self) -> None:
        events = [
            {"hostname": host, "stage": stage, "profile": "pi5",
             "received_at": f"2026-11-07T12:00:{20 - index:02d}+00:00",
             "instance_id": f"instance-{index}", "raw": {"marker": index}}
            for index, (host, stage) in enumerate((
                ("alpha", "ready"), ("bravo", "ready"), ("ALPHA", "boot"),
                ("alpha", "boot"), ("alpha", "hardware-check"),
                ("bravo", "boot"), ("alpha", "<unsafe>"),
            ))
        ]
        bootstrap = f"""
localStorage.removeItem("callback-older-expanded");
const eventsBodyEl = document.getElementById("events-body");
const eventSearchEl = document.getElementById("event-search");
const eventProfileEl = document.getElementById("event-profile");
const eventStageEl = document.getElementById("event-stage");
let allEvents = {json.dumps(events)};
"""
        probe = """
const rows = () => [...eventsBodyEl.querySelectorAll("tr[data-event-id]")];
const host = name => [...eventsBodyEl.querySelectorAll(".event-host-toggle")]
  .find(button => button.dataset.expansionKey === name);
const stage = (name, type) => [...eventsBodyEl.querySelectorAll(".event-stage-toggle")]
  .find(button => button.dataset.expansionKey === JSON.stringify([name, type]));
renderEvents(allEvents);
check(rows().length === 2, "one latest row per host");
check(host("alpha").textContent.includes("4 older callbacks"), "host history count");
check(eventsBodyEl.querySelectorAll("[data-archive-node]").length === 2, "archive only newest rows");
host("alpha").click();
check(host("alpha").getAttribute("aria-expanded") === "true", "host expands");
check(stage("alpha", "boot").textContent.includes("(2)"), "type groups count callbacks");
check(rows().length === 2, "type groups initially collapsed");
check(!eventsBodyEl.querySelector("unsafe") && stage("alpha", "<unsafe>").textContent.includes("<unsafe>"), "type names are escaped");
stage("alpha", "boot").click();
check(rows().length === 4 && rows()[1].classList.contains("event-row-condensed"), "type shows compact history");
rows()[1].click();
check(JSON.parse(document.getElementById("payload-inspect-json").textContent).raw.marker === 2, "older payload inspection");
const detail = eventsBodyEl.querySelector(".event-detail-toggle");
detail.click();
check(!rows()[1].classList.contains("event-row-condensed"), "details expand");
renderEvents(allEvents);
check(host("alpha").getAttribute("aria-expanded") === "true" &&
      stage("alpha", "boot").getAttribute("aria-expanded") === "true" &&
      !rows()[1].classList.contains("event-row-condensed"), "refresh preserves expansion");
check(document.activeElement.classList.contains("event-detail-toggle"), "refresh preserves toggle focus");
allEvents.unshift({hostname: "alpha", stage: "final", profile: "pi5", received_at: "2026-11-07T12:01:00+00:00"});
renderEvents(allEvents);
check(rows()[0].textContent.includes("final") && stage("alpha", "boot").getAttribute("aria-expanded") === "true",
      "new callback becomes latest without closing history");
allEvents.shift();
updateEventFilterOptions(allEvents);
eventStageEl.value = "boot";
renderEvents(filterEvents(allEvents));
check(rows().length === 3 && rows().every(row => row.textContent.includes("boot")), "stage filter groups matching events");
eventStageEl.value = "";
eventSearchEl.value = "bravo";
renderEvents(filterEvents(allEvents));
check(rows().length === 1 && host("bravo"), "host filter");
eventSearchEl.value = "";
renderEvents(filterEvents(allEvents));
check(stage("alpha", "boot").getAttribute("aria-expanded") === "true", "filter preserves expansion");
toggleOlderEvents();
check(rows().length === 7 && !eventsBodyEl.querySelector(".event-row-condensed"), "expand all");
stage("alpha", "boot").click();
check(stage("alpha", "boot").getAttribute("aria-expanded") === "false" && rows().length === 5, "individual collapse after expand all");
toggleOlderEvents();
check(rows().length === 2 && !eventsBodyEl.querySelector(".event-stage-toggle"), "collapse all");
"""
        self.browser_probe("/", ["home/events.js"], bootstrap, probe)

    def test_dashboard_and_pages_render(self) -> None:
        for path in ("/", "/nodes", "/monitor", "/images", "/tailscale-acl", "/settings", "/test-terminal", "/kubernetes"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, msg=f"expected 200 for {path}")

    def test_dashboard_contains_mobile_layout_rules_and_scrollable_callbacks(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<div class="table-wrap">\n        <table class="events-table">', html)
        with self.client.get("/static/home.css") as stylesheet:
            css = stylesheet.get_data(as_text=True)
        with self.client.get("/static/common.css") as stylesheet:
            shared_css = stylesheet.get_data(as_text=True)
        self.assertIn("minmax(min(240px, 100%), 1fr)", shared_css)
        self.assertRegex(
            css,
            r"\.key-picker-row,\s*\.key-preset-row,\s*\.key-upload-row,\s*\.inline-row\s*\{\s*"
            r"grid-template-columns: minmax\(0, 1fr\);",
        )

    def test_cloud_init_profile_matrix_in_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for the cloud-init profile matrix browser test")
        root = Path(__file__).resolve().parents[1]
        html = self.client.get("/").get_data(as_text=True)
        html = re.sub(r'<script\b[^>]*\bsrc=[^>]*>.*?</script>', "", html, flags=re.DOTALL)
        html = re.sub(r"<link\b[^>]*>", "", html)
        css = (root / "static/home.css").read_text() + (root / "static/common.css").read_text()
        html = html.replace("</head>", f"<style>{css}</style></head>")
        scripts = "\n".join(
            (root / "static/home" / name).read_text()
            for name in ("state.js", "catalog_filters.js", "k3s_roles.js", "cloud_init_options.js")
        )
        probe = r"""
const results = [];
window.fetch = async () => ({ok: true, json: async () => ({ok: true})});
function snapshot(profile, distribution) {
  form.profile.value = profile;
  form.distribution.value = distribution;
  ensureK3sConfigDefaults();
  applyK3sRoleDefaults();
  rebuildCatalogFilters();
  updateOsScopedOptionsVisibility();
  for (const section of document.querySelectorAll("[data-cloud-init-section]")) {
    if (!section.hidden && section.style.display !== "none") section.open = true;
  }
  return {
    profile, distribution: form.distribution.value,
    sections: [...document.querySelectorAll("[data-cloud-init-section]")]
      .filter(el => getComputedStyle(el).display !== "none").map(el => el.dataset.cloudInitSection),
    enabled: sdOptionsInputEls.filter(el => !el.disabled).map(el => el.dataset.optionKey),
    options: gatherCloudInitOptions(),
    requiredKey: document.getElementById("opt_tailscale_auth_key").required,
    securityStarterHidden: getComputedStyle(sdOptionsSecurityDefaultsBtn).display === "none",
    invalidHidden: sdOptionsInputEls.filter(el => !el.disabled && el.required && !el.checkValidity())
      .some(el => !el.getClientRects().length),
    viewportWidth: innerWidth, pageWidth: document.documentElement.scrollWidth,
  };
}
(async () => {
try {
  sdOptionsProfilePiOnlyEl.checked = true;
  document.getElementById("opt_wifi_ssid").value = "generic-wifi";
  document.getElementById("opt_package_list").value = "docker.io";
  document.getElementById("opt_toolkit_extra_packages").value = "iperf3, whois";
  document.getElementById("opt_install_telegraf").checked = true;
  document.getElementById("opt_disable_ssh_password").checked = true;
  document.getElementById("opt_tailscale_advertise_routes").value = "192.168.1.0/24";
  for (const profile of ["pi5", "pi4", "pi3bplus", "network-toolkit"]) {
    for (const distribution of ["standard", "k3s"]) results.push(snapshot(profile, distribution));
  }
  results.push(snapshot("pi5", "standard"));
  form.profile.value = "network-toolkit";
  ensureK3sConfigDefaults();
  applyCloudInitOptions({
    toolkit_wifi_ssid: " wifi with spaces ", toolkit_wifi_password: " password ",
    toolkit_wifi_country: "US", packages: ["docker.io"], install_telegraf: true,
    toolkit_extra_packages: ["strace", "whois"],
    tailscale_advertise_routes: ["192.168.1.0/24"], k3s_config: "server: stale",
  });
  results.push(snapshot("network-toolkit", "standard"));
  await resetCloudInitOptions();
  results.push(snapshot("network-toolkit", "standard"));
  document.body.innerHTML = '<pre id="matrix-result"></pre>';
  document.getElementById("matrix-result").textContent = JSON.stringify(results);
} catch (error) {
  document.body.innerHTML = '<pre id="matrix-error"></pre>';
  document.getElementById("matrix-error").textContent = String(error.stack);
}
})();
"""
        html = html.replace("</body>", f"<script>{scripts}\n{probe}</script></body>")
        with tempfile.TemporaryDirectory(prefix="cloud-init-matrix-") as tmp:
            page = Path(tmp) / "matrix.html"
            page.write_text(
                '<!doctype html><iframe style="width:390px;height:1000px;border:0" '
                'onload="document.body.appendChild((this.contentDocument.getElementById(\'matrix-result\') '
                '|| this.contentDocument.getElementById(\'matrix-error\')).cloneNode(true))" '
                f'srcdoc="{html_lib.escape(html, quote=True)}"></iframe>'
            )
            result = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                 "--disable-background-networking", f"--user-data-dir={tmp}/chrome",
                 "--dump-dom", page.as_uri()],
                text=True, capture_output=True, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="matrix-result">(.*?)</pre>', result.stdout, re.DOTALL)
        self.assertIsNotNone(match, result.stdout)
        snapshots = json.loads(html_lib.unescape(match.group(1)))
        general = {"identity", "network", "packages", "misc", "hardening", "access", "tailscale", "telemetry"}
        toolkit = {"toolkit", "toolkit-tools", "identity", "access", "tailscale", "telemetry"}
        for snapshot in snapshots:
            with self.subTest(profile=snapshot["profile"], distribution=snapshot["distribution"]):
                expected = set(toolkit if snapshot["profile"] == "network-toolkit" else general)
                if snapshot["profile"] == "pi5":
                    expected.add("pi")
                if snapshot["distribution"] == "k3s":
                    expected.add("k3s")
                self.assertEqual(set(snapshot["sections"]), expected)
                self.assertFalse(snapshot["invalidHidden"])
                self.assertEqual(snapshot["viewportWidth"], 390)
                self.assertLessEqual(snapshot["pageWidth"], snapshot["viewportWidth"])
                if snapshot["profile"] == "network-toolkit":
                    self.assertEqual(snapshot["distribution"], "standard")
                    self.assertTrue(snapshot["requiredKey"])
                    self.assertTrue(snapshot["securityStarterHidden"])
                    self.assertIn("toolkit_wifi_ssid", snapshot["enabled"])
                    self.assertIn("toolkit_extra_packages", snapshot["enabled"])
                    self.assertIn("username", snapshot["enabled"])
                    self.assertIn("tailscale_auth_key", snapshot["enabled"])
                    self.assertIn("enable_unattended_upgrades", snapshot["enabled"])
                    for key in ("wifi_ssid", "packages", "k3s_config", "install_telegraf",
                                "tailscale_advertise_routes", "disable_ssh_password_auth", "ssh_port"):
                        self.assertNotIn(key, snapshot["enabled"])
                        self.assertNotIn(key, snapshot["options"])
                else:
                    self.assertFalse(snapshot["requiredKey"])
                    self.assertFalse(snapshot["securityStarterHidden"])
                    self.assertNotIn("toolkit_wifi_ssid", snapshot["enabled"])
                    self.assertNotIn("toolkit_extra_packages", snapshot["enabled"])
                    self.assertNotIn("toolkit_extra_packages", snapshot["options"])
                    self.assertNotIn("toolkit_enable_funnel", snapshot["options"])
        self.assertEqual(snapshots[8]["options"]["wifi_ssid"], "generic-wifi")
        self.assertEqual(snapshots[8]["options"]["packages"], ["docker.io"])
        self.assertTrue(snapshots[8]["options"]["install_telegraf"])
        self.assertTrue(snapshots[8]["options"]["disable_ssh_password_auth"])
        self.assertEqual(snapshots[9]["options"]["toolkit_wifi_ssid"], " wifi with spaces ")
        self.assertEqual(snapshots[9]["options"]["toolkit_wifi_password"], " password ")
        self.assertEqual(snapshots[9]["options"]["toolkit_extra_packages"], ["strace", "whois"])
        self.assertEqual(snapshots[10]["options"]["toolkit_wifi_country"], "US")
        self.assertFalse(snapshots[10]["options"]["toolkit_enable_funnel"])
        self.assertNotIn("toolkit_extra_packages", snapshots[10]["options"])

    def test_sidebar_shows_kubernetes_link_when_runtime_unavailable(self) -> None:
        with patch_in("_detect_kubernetes_runtime",
            return_value={"available": False, "active_service": "", "reason": "inactive"},
        ):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('href="/kubernetes"', text)

    def test_sidebar_shows_kubernetes_link_when_runtime_available(self) -> None:
        with patch_in("_detect_kubernetes_runtime",
            return_value={"available": True, "active_service": "k3s", "reason": "active"},
        ):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('href="/kubernetes"', text)

    def test_sidebar_shows_kubernetes_link_when_installed_but_inactive(self) -> None:
        with patch_in("_detect_kubernetes_runtime",
            return_value={
                "available": False,
                "active_service": "",
                "k3s_binary": "/usr/local/bin/k3s",
                "has_kubeconfig": True,
                "reason": "k3s binary found but service is inactive",
            },
        ):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/kubernetes"', response.get_data(as_text=True))

    def test_kubernetes_summary_endpoint_returns_summary(self) -> None:
        fake_summary = {
            "ok": True,
            "available": True,
            "runtime": {"available": True, "active_service": "k3s"},
            "nodes_total": 3,
            "nodes_ready": 2,
            "control_plane_nodes": 1,
            "pods_total": 18,
            "pods_running": 16,
            "namespaces_total": 6,
            "jobs_total": 2,
            "jobs_running": 1,
            "longhorn_namespace_present": True,
            "longhorn_pods_total": 3,
            "longhorn_pods_running": 3,
            "longhorn_ready": True,
            "job_rows": [{"namespace": "default", "name": "refresh-secrets", "status": "Running", "active": 1, "succeeded": 0, "failed": 0}],
            "last_error": "",
        }
        with patch_in("_kubernetes_local_summary", return_value=fake_summary):
            response = self.client.get("/api/kubernetes/summary")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["nodes_total"], 3)
        self.assertEqual(payload["jobs_running"], 1)
        self.assertTrue(payload["longhorn_ready"])

    def test_kubernetes_page_renders_dense_dashboard_sections(self) -> None:
        response = self.client.get("/kubernetes")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("Runtime Details", text)
        self.assertIn("Cluster Snapshot", text)
        self.assertIn("Nodes", text)
        self.assertIn("Namespaces", text)
        self.assertIn("Pods", text)
        self.assertIn("Active Jobs", text)
        self.assertIn("Snapshot Notes", text)
        self.assertIn("Raw JSON snapshot", text)
        self.assertIn("Longhorn", text)

    def test_kubernetes_page_refreshes_summary_without_cache(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "static" / "kubernetes.js").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/kubernetes/summary", { cache: "no-store" })', text)
        self.assertIn('window.addEventListener("focus", loadKubernetesSummary);', text)
        self.assertIn('document.addEventListener("visibilitychange"', text)
