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

    def test_pages_fit_viewports_and_share_control_styles_in_browser(self) -> None:
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for shared style layout validation")
        frames = []
        for path in PAGES:
            with self.client.get(path) as response:
                text = response.get_data(as_text=True)
            text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.S)
            styles = "\n".join(
                (APP_DIR / "static" / name).read_text()
                for name in re.findall(r'href="/static/([^"]+\.css)"', text)
            )
            text = re.sub(r"<link\b[^>]*>", "", text)
            text = text.replace("</head>", f"<style>{styles}</style></head>")
            for width in (360, 390, 768, 1440):
                frames.append(
                    f'<iframe width="{width}" height="1000" data-path="{path}" data-width="{width}" '
                    f'onload="measure(this)" srcdoc="{html.escape(text, quote=True)}"></iframe>'
                )
        probe = """
const results = [];
function measure(frame) {
  const doc = frame.contentDocument, win = frame.contentWindow;
  const sample = doc.createElement("div");
  sample.innerHTML = '<button>Primary</button><button class="secondary">Secondary</button>'
    + '<button class="danger">Delete</button><input type="checkbox">';
  doc.querySelector(".panel").appendChild(sample);
  const style = element => win.getComputedStyle(element);
  for (const state of ["default", "expanded"]) {
    if (state === "expanded") {
      doc.querySelectorAll("details").forEach(element => { element.open = true; });
      doc.body.classList.add("sidebar-collapsed");
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
    });
  }
  if (results.length === 64) {
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
        self.assertEqual(len(snapshots), 64)
        for snapshot in snapshots:
            with self.subTest(path=snapshot["path"], width=snapshot["viewport"], state=snapshot["state"]):
                self.assertLessEqual(snapshot["width"], snapshot["viewport"])
                self.assertEqual(snapshot["panelRadius"], "16px")
                self.assertEqual(snapshot["primary"], "rgb(11, 139, 110)")
                self.assertEqual(snapshot["secondary"], "rgb(241, 245, 249)")
                self.assertEqual(snapshot["secondaryInk"], "rgb(51, 65, 85)")
                self.assertEqual(snapshot["danger"], "rgb(185, 28, 28)")
                self.assertLess(snapshot["checkboxWidth"], 30)
                if snapshot["viewport"] <= 760:
                    self.assertEqual(len(snapshot["navigationColumns"].split()), 2)
                if snapshot["path"] == "/nodes":
                    self.assertEqual(snapshot["nodeTableOverflow"], "auto")
                    self.assertEqual(snapshot["statCardPosition"], "relative")
                    self.assertEqual(snapshot["statCardOverflow"], "hidden")
        for width in (360, 390, 768, 1440):
            headings = {snapshot["heading"] for snapshot in snapshots if snapshot["viewport"] == width}
            self.assertEqual(len(headings), 1, (width, headings))
