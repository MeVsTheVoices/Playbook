"""Render the Dockerbot chart and check its storage and secret contracts."""

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "charts" / "dockerbot"
HELM = shutil.which("helm")


@unittest.skipUnless(HELM, "helm is required to test the Dockerbot chart")
class DockerbotChartTests(unittest.TestCase):
    def render(self, *values):
        result = subprocess.run(
            [HELM, "template", "dockerbot", str(CHART), *values],
            check=True, capture_output=True, text=True,
        )
        return {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout) if doc}

    def test_default_deployment_and_claim(self):
        docs = self.render()
        deployment = docs["Deployment"]["spec"]
        pod = deployment["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(deployment["replicas"], 1)
        self.assertEqual(deployment["strategy"]["type"], "Recreate")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(container["image"], "ghcr.io/rhomelab/red-discordbot:latest-noaudio")
        token = next(env for env in container["env"] if env["name"] == "TOKEN")
        self.assertEqual(token["valueFrom"]["secretKeyRef"], {
            "name": "dockerbot-token", "key": "token",
        })
        self.assertEqual(container["volumeMounts"][0]["mountPath"], "/redbot/data")
        self.assertEqual(pod["volumes"][0]["persistentVolumeClaim"]["claimName"], "dockerbot-dockerbot")
        claim = docs["PersistentVolumeClaim"]["spec"]
        self.assertNotIn("storageClassName", claim)
        self.assertEqual(claim["accessModes"], ["ReadWriteOnce"])
        self.assertEqual(claim["resources"]["requests"]["storage"], "10Gi")

    def test_longhorn_and_existing_claim(self):
        docs = self.render("--set", "persistence.storageClassName=longhorn")
        self.assertEqual(docs["PersistentVolumeClaim"]["spec"]["storageClassName"], "longhorn")
        docs = self.render("--set", "persistence.existingClaim=red-data")
        self.assertNotIn("PersistentVolumeClaim", docs)
        volume = docs["Deployment"]["spec"]["template"]["spec"]["volumes"][0]
        self.assertEqual(volume["persistentVolumeClaim"]["claimName"], "red-data")

    def test_disposable_storage_and_custom_secret(self):
        docs = self.render(
            "--set", "persistence.enabled=false",
            "--set", "bot.existingSecret=custom-secret",
            "--set", "bot.tokenKey=discord-token",
        )
        self.assertNotIn("PersistentVolumeClaim", docs)
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(pod["volumes"][0]["emptyDir"], {})
        token = next(env for env in pod["containers"][0]["env"] if env["name"] == "TOKEN")
        self.assertEqual(token["valueFrom"]["secretKeyRef"], {
            "name": "custom-secret", "key": "discord-token",
        })

    def test_empty_secret_is_rejected(self):
        result = subprocess.run(
            [HELM, "template", "dockerbot", str(CHART), "--set", "bot.existingSecret="],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bot.existingSecret must name an existing Secret", result.stderr)
