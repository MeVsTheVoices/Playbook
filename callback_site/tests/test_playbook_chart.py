"""Render the portal chart and check its module, RBAC and storage contracts."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
HELM = shutil.which("helm")


@unittest.skipUnless(HELM, "helm is required to test the Playbook chart")
class PlaybookChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        for name in ("playbook", "flash-helper"):
            shutil.copytree(ROOT / "charts" / name, Path(cls.tmp.name) / name, ignore=shutil.ignore_patterns("charts"))
        cls.chart = Path(cls.tmp.name) / "playbook"
        subprocess.run([HELM, "dependency", "build", str(cls.chart)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def render(self, *values):
        result = subprocess.run(
            [HELM, "template", "playbook", str(self.chart), *values],
            capture_output=True, text=True,
        )
        if result.returncode:
            raise ValueError(result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_core_install_has_no_cluster_access(self):
        docs = self.render()
        kinds = {doc["kind"] for doc in docs}
        self.assertNotIn("ClusterRoleBinding", kinds)
        deployment = next(doc for doc in docs if doc["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        self.assertFalse(pod["automountServiceAccountToken"])
        env = {item["name"]: item["value"] for item in pod["containers"][0]["env"]}
        self.assertEqual(env["PLAYBOOK_MODULES"], "")
        self.assertEqual(deployment["spec"]["template"]["metadata"]["labels"]["app"], "playbook-callback-site")

    def test_kubernetes_module_gets_cluster_credentials(self):
        docs = self.render("--set", "modules={kubernetes}")
        binding = next(doc for doc in docs if doc["kind"] == "ClusterRoleBinding")
        self.assertEqual(binding["subjects"][0]["name"], "playbook")
        pod = next(doc for doc in docs if doc["kind"] == "Deployment")["spec"]["template"]["spec"]
        self.assertTrue(pod["automountServiceAccountToken"])
        self.assertIn({"name": "PLAYBOOK_MODULES", "value": "kubernetes"}, pod["containers"][0]["env"])

    def test_unknown_module_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown module "bogus"'):
            self.render("--set", "modules={bogus}")

    def test_existing_claim_skips_the_chart_claim(self):
        docs = self.render("--set", "persistence.existingClaim=portal-data")
        self.assertNotIn("PersistentVolumeClaim", {doc["kind"] for doc in docs})


if __name__ == "__main__":
    unittest.main()
