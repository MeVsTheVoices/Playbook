"""Compact, locally highlighted output must preserve data and clipboard text."""

from __future__ import annotations

try:
    from .test_support import APP_DIR, BaseCallbackSiteTest
except ImportError:
    from test_support import APP_DIR, BaseCallbackSiteTest


class StructuredOutputTests(BaseCallbackSiteTest):
    def test_structured_output_pages_load_the_local_bundle(self) -> None:
        for path in ("/", "/monitor", "/images", "/kubernetes", "/tailscale-acl", "/test-terminal"):
            with self.subTest(path=path):
                text = self.client.get(path).get_data(as_text=True)
                self.assertEqual(text.count("/static/structured_output.js"), 1)
        from app import app
        with app.test_request_context("/"):
            text = app.jinja_env.get_template("report.html").render(report_id="test", report={})
        self.assertIn("/static/structured_output.js", text)
        for name in ("structured_output.js", "structured_output.LICENSE.txt"):
            self.assertTrue((APP_DIR / "static" / name).is_file())

    def test_output_formatting_and_dynamic_updates_in_browser(self) -> None:
        checks = self.browser_probe("/", ["structured_output.js"], """
const sourceValue = {
  tags: ["tag:rpi5", "tag:rpi4", "tag:homelab"],
  nested: {enabled: true, ports: ["tcp:22", "tcp:8088"]},
  escaped: 'comma, quote" slash\\\\ newline\\n<img src=x onerror="window.unsafe=true">'
};
const payload = document.querySelector("#payload-inspect-json");
const original = JSON.stringify(sourceValue, null, 2);
payload.textContent = original;
function block(language, text, compact) {
  const element = document.createElement("pre");
  if (language) element.dataset.format = language;
  if (compact === false) element.dataset.compact = "false";
  element.textContent = text;
  document.body.append(element);
  return element;
}
const policyText = '{\\n  "routes": {\\n    "10.0.0.0/24": [\\n      "tag:rpi5", "tag:rpi4"\\n    ]\\n  }\\n}';
const policy = block("json", policyText, false);
const yamlText = '# Deployment\\napiVersion: apps/v1\\nkind: Deployment\\n---\\nitems: [one, two]\\n';
const manifest = block("yaml", yamlText);
const commandText = 'printf "%s\\\\n" "$HOSTNAME"\\nuname -a';
const command = block("bash", commandText);
const rawLog = block(null, "2026-11-08 INFO  100 bytes copied\\n");
const numericText = '{"id":9007199254740993,"precise":0.1234567890123456789,"negative":-0}';
const numeric = block("json", numericText);
const invalid = block("json", '{"broken": }');
const largeText = JSON.stringify({value: "x".repeat(200001)});
const large = block("json", largeText);
""", """
check(JSON.stringify(JSON.parse(payload.textContent)) === JSON.stringify(sourceValue), "JSON values unchanged");
check(payload.textContent.split("\\n").length < original.split("\\n").length, "JSON line count reduced");
check(payload.textContent.includes('["tag:rpi5", "tag:rpi4", "tag:homelab"]'), "short arrays inline");
check(payload.querySelector(".hljs-attr") && payload.querySelector(".hljs-string"), "JSON syntax colors");
check(!payload.querySelector("img") && !window.unsafe, "untrusted output escaped");
check(policy.textContent === policyText, "ACL layout and copy text preserved");
check(manifest.textContent === yamlText && manifest.querySelector(".hljs-attr"), "YAML indentation preserved and highlighted");
check(command.textContent === commandText && command.querySelector("span"), "shell preview preserved and highlighted");
check(rawLog.textContent === "2026-11-08 INFO  100 bytes copied\\n" && !rawLog.children.length, "live logs left alone");
check(numeric.textContent === numericText, "precise numbers preserved verbatim");
check(invalid.dataset.formatStatus === "invalid" && invalid.textContent === '{"broken": }', "invalid JSON stays visible with explanation");
check(large.dataset.formatStatus === "large" && large.textContent === largeText && !large.children.length, "large outputs stay responsive");
const originalColor = getComputedStyle(payload.querySelector(".hljs-attr")).color;
check(originalColor !== getComputedStyle(payload).color, "syntax styles applied");
payload.textContent = JSON.stringify({updated: ["one", "two"]}, null, 2);
await tick();
check(payload.querySelector(".hljs-attr") && JSON.parse(payload.textContent).updated.length === 2, "async output changes reformatted");
const inserted = block("json", JSON.stringify({added: [1, 2]}, null, 2));
await tick();
check(inserted.textContent.includes("[1, 2]") && inserted.querySelector(".hljs-number"), "dynamically inserted output formatted");
const unchanged = inserted.innerHTML;
await tick();
check(inserted.innerHTML === unchanged, "observer does not reprocess its own highlighting");
""")
        self.assertEqual(len(checks), 16)
