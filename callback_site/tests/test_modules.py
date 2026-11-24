from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]

PROBE = """
import sys
from app import MODULES, app
rules = {rule.rule for rule in app.url_map.iter_rules()}
with app.test_request_context("/settings"):
    sidebar = app.jinja_env.get_template("_sidebar.html").render(modules=MODULES)
print(any(r.startswith(("/kubernetes", "/api/kubernetes", "/api/helm")) for r in rules))
print("services.kubernetes" in sys.modules or "services.helm" in sys.modules)
print('href="/kubernetes"' in sidebar)
"""


def run_portal(modules: str | None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(APP_DIR), str(APP_DIR.parent)])}
    env.pop("PLAYBOOK_MODULES", None)
    if modules is not None:
        env["PLAYBOOK_MODULES"] = modules
    with tempfile.TemporaryDirectory(prefix="modules-test-") as data_dir:
        env["CALLBACK_APPDATA_DIR"] = data_dir
        return subprocess.run([sys.executable, "-c", PROBE], cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=60)


class OptionalModuleTests(unittest.TestCase):
    def test_kubernetes_is_absent_unless_enabled(self) -> None:
        result = run_portal(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["False", "False", "False"])

    def test_enabled_kubernetes_registers_routes(self) -> None:
        result = run_portal(" Kubernetes ")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["True", "True", "True"])

    def test_unknown_module_fails_at_startup(self) -> None:
        result = run_portal("kubernetes,provisoner")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown PLAYBOOK_MODULES entries: provisoner", result.stderr)


if __name__ == "__main__":
    unittest.main()
