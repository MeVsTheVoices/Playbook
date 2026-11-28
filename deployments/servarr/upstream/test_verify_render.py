import copy
import unittest
from pathlib import Path

import yaml

from verify_render import APPS, verify


HERE = Path(__file__).resolve().parent


def example():
    guard = yaml.safe_load((HERE / "qbittorrent-vpn.patch.yaml").read_text())
    sidecar = yaml.safe_load((HERE / "qbittorrent-sidecar.yaml").read_text())
    pod = guard["spec"]["template"]["spec"]
    tailscale = sidecar["spec"]["template"]["spec"]["containers"][0]
    tailscale.update(pod["containers"][0])
    pod["containers"] = [
        tailscale,
        {"name": "servarr", "env": [{"name": "PUID", "value": "1000"}]},
    ]
    pod["volumes"] = [
        {"name": "config", "persistentVolumeClaim": {"claimName": "servarr-qbittorrent"}},
        {"name": "media", "persistentVolumeClaim": {"claimName": "servarr-jellyfin-media"}},
    ]
    qb = guard
    objects = [qb]
    claims = {
        "bazarr": ["servarr-bazarr", "servarr-jellyfin-media"],
        "jellyfin": ["servarr-jellyfin-config", "servarr-jellyfin-media"],
        "prowlarr": ["servarr-prowlarr"],
        "radarr": ["servarr-radarr", "servarr-jellyfin-media"],
        "seerr": ["servarr-seerr"],
        "sonarr": ["servarr-sonarr", "servarr-jellyfin-media"],
    }
    for app in APPS - {"qbittorrent"}:
        volumes = [{"name": str(i), "persistentVolumeClaim": {"claimName": claim}}
                   for i, claim in enumerate(claims.get(app, []))]
        pod_spec = {"volumes": volumes}
        if app == "seerr":
            pod_spec["securityContext"] = {"runAsUser": 1000, "fsGroup": 1000, "runAsNonRoot": True}
            pod_spec["containers"] = [{"name": "servarr", "securityContext": {"capabilities": {"drop": ["ALL"]}}}]
        objects.append({
            "kind": "Deployment",
            "metadata": {"name": f"servarr-{app}", "namespace": "servarr"},
            "spec": {"template": {"spec": pod_spec}},
        })
    for app in ("jellyfin", "sonarr", "radarr", "prowlarr", "qbittorrent", "seerr"):
        objects.append({"kind": "Ingress", "metadata": {"name": f"servarr-{app}", "namespace": "servarr"}})
    return objects


class VerifyRenderTests(unittest.TestCase):
    def test_accepts_required_resources(self):
        verify(example())

    def test_rejects_missing_or_weakened_guard(self):
        for change in ("missing", "changed"):
            objects = example()
            init = objects[0]["spec"]["template"]["spec"]["initContainers"]
            if change == "missing":
                init.clear()
            else:
                init[0]["command"][-1] = "true"
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify(objects)

    def test_rejects_changed_identity_storage_or_namespace(self):
        for change in ("uid", "claim", "namespace", "extra", "ingress", "seerr"):
            objects = copy.deepcopy(example())
            pod = objects[0]["spec"]["template"]["spec"]
            if change == "uid":
                pod["containers"][1]["env"][0]["value"] = "0"
            elif change == "claim":
                pod["volumes"][1]["persistentVolumeClaim"]["claimName"] = "other"
            elif change == "namespace":
                objects[0]["metadata"]["namespace"] = "default"
            elif change == "extra":
                objects.append({"kind": "Secret", "metadata": {"namespace": "servarr"}})
            elif change == "ingress":
                objects = [obj for obj in objects if obj["metadata"]["name"] != "servarr-seerr" or obj["kind"] != "Ingress"]
            else:
                seerr = next(obj for obj in objects if obj["metadata"]["name"] == "servarr-seerr")
                seerr["spec"]["template"]["spec"]["securityContext"]["runAsUser"] = 0
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify(objects)


if __name__ == "__main__":
    unittest.main()
