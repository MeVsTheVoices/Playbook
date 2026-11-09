from __future__ import annotations

import atexit
import importlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import MagicMock, patch

TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="callback-site-tests-"))
os.environ["CALLBACK_APPDATA_DIR"] = str(TEST_DATA_DIR)
# Tests post callbacks constantly; a background SSH pull per node would only add noise.
os.environ["CALLBACK_AUTO_BOOT_LOGS"] = "0"

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import app as callback_app  # noqa: E402
from app import app  # noqa: E402


def _cleanup_test_data_dir() -> None:
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


atexit.register(_cleanup_test_data_dir)


class patch_in:
    """Replace one name with a single shared mock in every loaded project module that holds it.

    A from-import copies a name into each importing module, so patching only the defining module
    misses its callers. Pass `modules` to limit the patch. Usable as a context manager or via start()/stop().
    """

    def __init__(self, name: str, modules: Iterable[str] | None = None, **mock_kwargs: Any) -> None:
        self.mock = MagicMock(**mock_kwargs)
        if modules is None:
            modules = [
                key for key, mod in list(sys.modules.items())
                if mod is not None and (key == "app" or key.startswith(("services.", "routes."))) and hasattr(mod, name)
            ]
        self._targets = [(importlib.import_module(module), name) for module in modules]
        self._stack: ExitStack | None = None

    def start(self) -> MagicMock:
        self._stack = ExitStack()
        for module, name in self._targets:
            self._stack.enter_context(patch.object(module, name, self.mock))
        return self.mock

    def stop(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    def __enter__(self) -> MagicMock:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class BaseCallbackSiteTest(unittest.TestCase):
    def browser_probe(self, path: str, scripts: list[str], bootstrap: str, probe: str) -> Any:
        """Run real page markup and selected scripts with stubbed requests in Chrome."""
        chrome = shutil.which("google-chrome") or shutil.which("chromium")
        if not chrome:
            self.skipTest("Chrome is required for browser interaction validation")
        text = self.client.get(path).get_data(as_text=True)
        text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.S)
        styles = "\n".join(
            (APP_DIR / "static" / name).read_text()
            for name in re.findall(r'href="/static/([^"]+\.css)"', text)
        )
        text = re.sub(r"<link\b[^>]*>", "", text)
        text = text.replace("</head>", f"<style>{styles}</style></head>")
        source = "\n".join((APP_DIR / "static" / name).read_text() for name in scripts)
        runner = """
const checks = [];
function check(condition, label) {
  if (!condition) throw new Error(label);
  checks.push(label);
}
const tick = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
async function runProbe() {
""" + probe + """
  return checks;
}
runProbe().then(checks => finish({checks})).catch(error => finish({error: error.message, checks}));
function finish(result) {
  document.body.innerHTML = '<pre id="browser-result"></pre>';
  document.querySelector("pre").textContent = JSON.stringify(result);
}
"""
        text = text.replace("</body>", f"<script>{bootstrap}\n{source}\n{runner}</script></body>")
        with tempfile.TemporaryDirectory(prefix="callback-browser-") as tmp:
            fixture = Path(tmp) / "probe.html"
            fixture.write_text(text)
            result = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
                 f"--user-data-dir={tmp}/chrome", "--dump-dom", fixture.as_uri()],
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        match = re.search(r'<pre id="browser-result">(.*?)</pre>', result.stdout, re.S)
        self.assertIsNotNone(match, result.stdout[-2000:])
        output = json.loads(html.unescape(match.group(1)))
        self.assertNotIn("error", output, output)
        return output["checks"]

    def page_text(self, response) -> str:
        """The page HTML plus the local stylesheets and scripts it links, for assertions on inline-era content."""
        html = response.get_data(as_text=True)
        parts = [html]
        for path in re.findall(r'(?:src|href)="(/static/[^"?#]+\.(?:js|css))', html):
            with self.client.get(path) as asset:
                parts.append(asset.get_data(as_text=True))
        return "\n".join(parts).replace("\r\n", "\n")

    def setUp(self) -> None:
        self.client = app.test_client()
        # Callback tests must not launch Ansible against inventory or example IPs.
        automatic_jobs = patch_in("_start_node_provision_job", ["services.node_views"], return_value="test-auto-job")
        automatic_jobs.start()
        self.addCleanup(automatic_jobs.stop)
        # The Node Ops hardware check POSTs here; port 9 is closed, so tests can never reach the live site.
        guard = patch_in(
            "_default_callback_url",
            ["services.node_runs"],
            return_value="http://127.0.0.1:9/api/cloud-init/callback",
        )
        guard.start()
        self.addCleanup(guard.stop)
        # The real value comes from the K3s on this machine, which would make env assertions machine-dependent.
        version_guard = patch_in("_local_k3s_version", return_value="")
        version_guard.start()
        self.addCleanup(version_guard.stop)
        # This machine's tailnet would otherwise change which address a node resolves to.
        tailnet_guard = patch_in("_tailscale_peer_ip", return_value="")
        tailnet_guard.start()
        self.addCleanup(tailnet_guard.stop)
        fqdn_guard = patch_in("_tailscale_peer_fqdn", return_value="")
        fqdn_guard.start()
        self.addCleanup(fqdn_guard.stop)
