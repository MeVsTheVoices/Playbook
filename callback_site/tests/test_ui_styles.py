"""Shared page chrome and measurable desktop/mobile styling invariants."""

from __future__ import annotations

import html
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

try:
    from .test_support import APP_DIR, BaseCallbackSiteTest, patch_in
except ImportError:
    from test_support import APP_DIR, BaseCallbackSiteTest, patch_in

PAGES = ("/", "/nodes", "/monitor", "/images", "/tailscale-acl", "/settings", "/test-terminal", "/kubernetes")


class SharedStyleTests(BaseCallbackSiteTest):
    def test_sidebar_toggle_keeps_state_specific_icon_and_accessible_label(self) -> None:
        checks = self.browser_probe(
            "/tailscale-acl", ["tailscale_acl.js"],
            'localStorage.setItem("sidebar-collapsed", "0");',
            """
const toggle = document.querySelector("#sidebar-toggle");
const visible = selector => getComputedStyle(document.querySelector(selector)).display !== "none";
check(toggle.closest(".sidebar-brand") !== null, "toggle is integrated into sidebar header");
check(visible(".sidebar-collapse-label") && !visible(".sidebar-expand-label"),
  "expanded sidebar offers collapse action");
check(visible(".sidebar-collapse-icon") && !visible(".sidebar-expand-icon"),
  "expanded sidebar uses a fixed left arrow");
toggle.click();
check(document.body.classList.contains("sidebar-collapsed")
  && localStorage.getItem("sidebar-collapsed") === "1", "collapse persists");
check(visible(".sidebar-expand-label") && !visible(".sidebar-collapse-label"),
  "collapsed sidebar offers expand action");
check(visible(".sidebar-expand-icon") && !visible(".sidebar-collapse-icon"),
  "collapsed sidebar uses a fixed right arrow");
document.querySelector(".sidebar").getAnimations().forEach(animation => animation.finish());
const bounds = toggle.getBoundingClientRect(), header = document.querySelector(".sidebar-brand").getBoundingClientRect();
check(bounds.width === 42 && Math.abs((bounds.left + bounds.right) / 2 - (header.left + header.right) / 2) < 1,
  "collapsed toggle stays full-sized and centered, even with a sidebar scrollbar");
toggle.click();
check(!document.body.classList.contains("sidebar-collapsed")
  && localStorage.getItem("sidebar-collapsed") === "0", "expand persists");
check(visible(".sidebar-collapse-label") && visible(".sidebar-collapse-icon"),
  "expanded action and icon are restored");
""",
        )
        self.assertEqual(len(checks), 9)

    def test_every_page_uses_shared_styles_and_navigation(self) -> None:
        for path in PAGES:
            with self.subTest(path=path), patch_in("_detect_kubernetes_runtime", return_value={
                "available": True, "k3s_binary": "/usr/local/bin/k3s",
            }):
                with self.client.get(path) as response:
                    text = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(text.count('/static/common.css'), 1)
                self.assertIn('aria-label="Main"', text)
                self.assertIn('aria-current="page"', text)
                self.assertIn("Cloud-init, imaging, and node operations", text)
                self.assertNotIn("<style>", text)
        from app import app
        with app.test_request_context("/"):
            report = app.jinja_env.get_template("report.html").render(
                report_id="test-report", report={"hostname": "test-node", "received_at": "2026-11-08"},
            )
        self.assertIn('/static/common.css', report)
        self.assertIn('/static/report.css', report)
        self.assertNotIn("<style>", report)

    def test_shared_styles_have_one_owner(self) -> None:
        for path in (APP_DIR / "static").glob("*.css"):
            if path.name == "common.css":
                continue
            with self.subTest(stylesheet=path.name):
                text = path.read_text()
                self.assertNotRegex(text, r"(?m)^(?:body|\.sidebar|\.wrap|\.panel|\.table-wrap|:root)\s*\{")
        css = (APP_DIR / "static/common.css").read_text()
        self.assertIn(":focus-visible", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertRegex(css, r"select\[multiple\]\s*\{\s*overflow:\s*auto;")

    def test_pages_fit_viewports_and_share_control_styles_in_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for shared style layout validation")
        frames = []
        for path in PAGES:
            with patch_in("_read_image_history", ["routes.pages"], return_value=[{
                "id": "layout-image", "label": "x" * 80, "source_value": "https://example.invalid/" + "y" * 120,
                "download_path": "", "profile": "pi5", "recorded_at": "", "hash_status": "unknown",
                "job_status": "completed", "exit_code": 0, "local_available": False,
            }]), self.client.get(path) as response:
                text = response.get_data(as_text=True)
            text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.S)
            styles = "\n".join(
                (APP_DIR / "static" / name).read_text()
                for name in re.findall(r'href="/static/([^"]+\.css)"', text)
            )
            styles += "\n*, *::before, *::after { animation: none !important; transition: none !important; }"
            text = re.sub(r"<link\b[^>]*>", "", text)
            text = text.replace("</head>", f"<style>{styles}</style></head>")
            if path == "/nodes":
                scripts = "\n".join(
                    (APP_DIR / "static/nodes" / name).read_text()
                    for name in ("state.js", "module_config.js", "node_table.js")
                )
                scripts += """
nodes = [{
  name: "jd-shorty-rack-alpha", group: "rpi5_active", address: "100.82.216.81",
  address_source: "callback", callback_stage: "hardware-check", manual_node: true,
  last_callback: "2026-11-08T12:42:28Z",
  selected_profiles: ["baseline", "tailscale", "telegraf", "docker", "longhorn"],
  latest_check: {status: "ok", mode: "check", checked_at: "2026-11-08T12:42:07Z"},
  hardware: {received_at: "now", model: "Ubuntu 24.04.2 LTS", source_ip: "100.82.216.81"},
  cert: {cert_present: true, cert_subject: "long-certificate-subject-".repeat(12)}
}];
profiles = nodes[0].selected_profiles.map(id => ({id, label: id}));
statusByNode = {"jd-shorty-rack-alpha": {reachable: false}};
renderRows();
"""
                text = text.replace("</body>", f"<script>{scripts}</script></body>")
            for width in (360, 390, 768, 1024, 1440):
                frames.append(
                    f'<iframe width="{width}" height="1000" data-path="{path}" data-width="{width}" '
                    f'onload="measure(this)" srcdoc="{html.escape(text, quote=True)}"></iframe>'
                )
        probe = """
const results = [];
function measure(frame) {
  const doc = frame.contentDocument, win = frame.contentWindow;
  const sample = doc.createElement("div");
  sample.className = "actions-row";
  sample.innerHTML = '<button>Primary</button><button class="secondary">Secondary</button>'
    + '<button class="danger">Delete</button><input type="checkbox">';
  doc.querySelector(".panel").appendChild(sample);
  const taskCards = doc.querySelector("#task-cards");
  if (taskCards) {
    taskCards.innerHTML = '<button class="task-card" aria-pressed="true"><strong>Identity</strong>'
      + '<span class="muted">Read-only system information</span></button>';
  }
  const moduleDetail = doc.querySelector("#module-detail");
  if (moduleDetail) {
    moduleDetail.classList.remove("collapsed");
    moduleDetail.innerHTML = '<div class="module-head"><h4>Baseline</h4>'
      + '<div class="module-live-bar"><span class="module-live-status ok">'
      + 'Read from a-long-node-name.example.internal just now · 3 fields found</span>'
      + '<button class="secondary module-live-refresh">Read from node</button></div></div>'
      + '<div class="module-options"><div class="module-field module-toggle">'
      + '<label class="module-toggle-row"><input type="checkbox">'
      + '<span>Enable unattended upgrades</span></label></div></div>';
    doc.querySelector("#module-name-toggle").disabled = false;
  }
  const style = element => win.getComputedStyle(element);
  for (const state of ["default", "expanded"]) {
    if (state === "expanded") {
      doc.querySelectorAll("details").forEach(element => { element.open = true; });
      doc.body.classList.add("sidebar-collapsed");
      if (moduleDetail) {
        doc.querySelector("#module-name-toggle").hidden = true;
        doc.querySelector("#module-name-form").hidden = false;
      }
    }
    const nodeTable = doc.querySelector("#node-rows");
    const statCard = doc.querySelector(".stats-grid .stat-card");
    results.push({
      state,
      path: frame.dataset.path, viewport: Number(frame.dataset.width),
      width: doc.documentElement.scrollWidth, heading: style(doc.querySelector("h1")).fontSize,
      panelRadius: style(doc.querySelector(".panel")).borderRadius,
      primary: style(sample.children[0]).backgroundColor,
      secondary: style(sample.children[1]).backgroundColor,
      secondaryInk: style(sample.children[1]).color,
      danger: style(sample.children[2]).backgroundColor,
      checkboxWidth: sample.children[3].getBoundingClientRect().width,
      navigationColumns: style(doc.querySelector("nav")).gridTemplateColumns,
      nodeTableOverflow: nodeTable ? style(nodeTable.closest("table").parentElement).overflowX : null,
      statCardPosition: statCard ? style(statCard).position : null,
      statCardOverflow: statCard ? style(statCard).overflowX : null,
      taskCardBackground: taskCards ? style(taskCards.firstElementChild).backgroundColor : null,
      taskCardInk: taskCards ? style(taskCards.firstElementChild).color : null,
      taskCardLayout: taskCards ? style(taskCards.firstElementChild).display : null,
      helmSelectionHeight: doc.querySelector("#helm-selection")?.getBoundingClientRect().height ?? null,
      helmSelectionArrow: doc.querySelector("#helm-selection") ? style(doc.querySelector("#helm-selection")).backgroundImage : null,
      sidebarWidth: doc.querySelector(".sidebar").getBoundingClientRect().width,
      sidebarToggleWidth: doc.querySelector("#sidebar-toggle").getBoundingClientRect().width,
      sidebarToggleFitsHeader: (() => {
        const button = doc.querySelector("#sidebar-toggle").getBoundingClientRect();
        const header = doc.querySelector(".sidebar-brand").getBoundingClientRect();
        return !button.width || (button.left >= header.left && button.right <= header.right + 1
          && button.top >= header.top && button.bottom <= header.bottom + 1);
      })(),
      sidebarBrandVisible: style(doc.querySelector(".sidebar-brand-copy")).display !== "none",
      aclHeroBackground: doc.querySelector(".acl-hero")
        ? style(doc.querySelector(".acl-hero")).backgroundImage : null,
      aclHeroInk: doc.querySelector(".acl-hero") ? style(doc.querySelector(".acl-hero")).color : null,
      aclSmileys: doc.querySelectorAll(".acl-hero .hero-corner").length,
      navigationIcons: [...doc.querySelectorAll("nav a")].every(link =>
        link.querySelector("svg")?.getBoundingClientRect().width > 0
        && link.getAttribute("aria-label") && link.title),
      nodeActionCount: nodeTable ? nodeTable.querySelectorAll("button").length : null,
      nodeActionsFit: nodeTable ? [...nodeTable.querySelectorAll("button")].every(button => {
        const bounds = button.getBoundingClientRect(), cell = button.closest("td").getBoundingClientRect();
        return bounds.width > 0 && bounds.left >= cell.left && bounds.right <= cell.right
          && button.scrollWidth <= button.clientWidth + 1;
      }) : null,
      moduleOptionsOverflow: moduleDetail ? doc.querySelector(".module-options").scrollWidth
        - doc.querySelector(".module-options").clientWidth : null,
      nameFormDisplay: moduleDetail ? style(doc.querySelector("#module-name-form")).display : null,
      nameToggleDisplay: moduleDetail ? style(doc.querySelector("#module-name-toggle")).display : null,
      moduleActionsFit: moduleDetail ? [...doc.querySelectorAll(
        ".module-actions button, .module-name-form input, .module-live-bar button"
      )].every(element => {
        const bounds = element.getBoundingClientRect(), parent = element.parentElement.getBoundingClientRect();
        return !bounds.width || (bounds.right <= parent.right + 1 && bounds.left >= parent.left - 1);
      }) : null,
    });
  }
  if (results.length === 80) {
    const output = document.createElement("pre");
    output.id = "style-result";
    output.textContent = JSON.stringify(results);
    document.body.replaceChildren(output);
  }
}
"""
        with tempfile.TemporaryDirectory(prefix="shared-style-") as tmp:
            fixture = Path(tmp) / "styles.html"
            fixture.write_text("<!doctype html><script>" + probe + "</script>" + "\n".join(frames))
            result = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
                 f"--user-data-dir={tmp}/chrome", "--dump-dom", fixture.as_uri()],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="style-result">(.*?)</pre>', result.stdout, re.S)
        self.assertIsNotNone(match, result.stdout[-2000:])
        snapshots = json.loads(html.unescape(match.group(1)))
        self.assertEqual(len(snapshots), 80)
        for snapshot in snapshots:
            with self.subTest(path=snapshot["path"], width=snapshot["viewport"], state=snapshot["state"]):
                self.assertLessEqual(snapshot["width"], snapshot["viewport"])
                self.assertEqual(snapshot["panelRadius"], "16px")
                self.assertEqual(snapshot["primary"], "rgb(11, 139, 110)")
                self.assertEqual(snapshot["secondary"], "rgb(241, 245, 249)")
                self.assertEqual(snapshot["secondaryInk"], "rgb(51, 65, 85)")
                self.assertEqual(snapshot["danger"], "rgb(185, 28, 28)")
                self.assertLess(snapshot["checkboxWidth"], 30)
                self.assertTrue(snapshot["navigationIcons"])
                self.assertTrue(snapshot["sidebarToggleFitsHeader"])
                self.assertEqual(snapshot["sidebarBrandVisible"],
                                 snapshot["viewport"] <= 760 or snapshot["state"] == "default")
                if snapshot["path"] == "/tailscale-acl":
                    self.assertIn("linear-gradient", snapshot["aclHeroBackground"])
                    self.assertIn("11, 139, 110", snapshot["aclHeroBackground"])
                    self.assertEqual(snapshot["aclHeroInk"], "rgb(230, 254, 247)")
                    self.assertEqual(snapshot["aclSmileys"], 2)
                if snapshot["viewport"] <= 760:
                    self.assertEqual(len(snapshot["navigationColumns"].split()), 2)
                else:
                    self.assertEqual(snapshot["sidebarToggleWidth"], 42)
                    self.assertLessEqual(snapshot["sidebarWidth"], 196)
                    if snapshot["state"] == "expanded":
                        self.assertEqual(snapshot["sidebarWidth"], 60)
                if snapshot["path"] == "/nodes":
                    self.assertEqual(snapshot["nodeTableOverflow"], "auto")
                    self.assertEqual(snapshot["statCardPosition"], "relative")
                    self.assertEqual(snapshot["statCardOverflow"], "hidden")
                    self.assertEqual(snapshot["nodeActionCount"], 4)
                    self.assertTrue(snapshot["nodeActionsFit"])
                    self.assertTrue(snapshot["moduleActionsFit"])
                    self.assertLessEqual(snapshot["moduleOptionsOverflow"], 1)
                    self.assertEqual(snapshot["nameFormDisplay"], "none" if snapshot["state"] == "default" else "flex")
                    self.assertEqual(snapshot["nameToggleDisplay"] == "none", snapshot["state"] == "expanded")
                if snapshot["path"] == "/test-terminal":
                    self.assertEqual(snapshot["taskCardLayout"], "grid")
                    self.assertEqual(snapshot["taskCardBackground"], "rgb(234, 250, 243)")
                    self.assertEqual(snapshot["taskCardInk"], "rgb(22, 32, 45)")
                if snapshot["path"] == "/kubernetes":
                    self.assertGreaterEqual(snapshot["helmSelectionHeight"], 42)
                    self.assertNotEqual(snapshot["helmSelectionArrow"], "none")
        for width in (360, 390, 768, 1024, 1440):
            headings = {snapshot["heading"] for snapshot in snapshots if snapshot["viewport"] == width}
            self.assertEqual(len(headings), 1, (width, headings))
