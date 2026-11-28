#!/usr/bin/env python3
"""Fail closed when a rendered Servarr chart loses required local settings."""

import sys
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
APPS = {"bazarr", "flaresolverr", "jellyfin", "prowlarr", "qbittorrent", "radarr", "seerr", "sonarr"}
ALLOWED_KINDS = {"ServiceAccount", "Service", "Deployment", "Ingress"}


def verify(objects):
    objects = list(objects)
    if not objects or any(not isinstance(obj, dict) for obj in objects):
        raise ValueError("rendered resources must be nonempty Kubernetes objects")
    for obj in objects:
        if obj.get("kind") not in ALLOWED_KINDS or obj.get("metadata", {}).get("namespace") != "servarr":
            raise ValueError("unexpected resource kind or namespace; review the chart update")

    deployments = [obj for obj in objects if obj["kind"] == "Deployment"]
    names = [obj["metadata"]["name"] for obj in deployments]
    if len(names) != len(APPS) or set(names) != {f"servarr-{app}" for app in APPS}:
        raise ValueError("rendered deployments do not match the existing suite")
    ingress_names = {obj["metadata"]["name"] for obj in objects if obj["kind"] == "Ingress"}
    if ingress_names != {f"servarr-{app}" for app in ("jellyfin", "sonarr", "radarr", "prowlarr", "qbittorrent", "seerr")}:
        raise ValueError("rendered ingress resources do not match the existing suite")

    claims = {
        "bazarr": {"servarr-bazarr", "servarr-jellyfin-media"},
        "jellyfin": {"servarr-jellyfin-config", "servarr-jellyfin-media"},
        "prowlarr": {"servarr-prowlarr"},
        "qbittorrent": {"servarr-qbittorrent", "servarr-jellyfin-media"},
        "radarr": {"servarr-radarr", "servarr-jellyfin-media"},
        "seerr": {"servarr-seerr"},
        "sonarr": {"servarr-sonarr", "servarr-jellyfin-media"},
    }
    for obj in deployments:
        app = obj["metadata"]["name"].removeprefix("servarr-")
        pod_spec = obj["spec"]["template"]["spec"]
        actual_claims = {
            volume["persistentVolumeClaim"]["claimName"]
            for volume in pod_spec.get("volumes", [])
            if "persistentVolumeClaim" in volume
        }
        if actual_claims != claims.get(app, set()):
            raise ValueError(f"{app} PVC bindings do not match the existing deployment")

    seerr = next(obj for obj in deployments if obj["metadata"]["name"] == "servarr-seerr")
    seerr_pod = seerr["spec"]["template"]["spec"]
    security = seerr_pod.get("securityContext", {})
    if security.get("runAsUser") != 1000 or security.get("fsGroup") != 1000 or security.get("runAsNonRoot") is not True:
        raise ValueError("Seerr must retain its non-root writable-volume security context")
    seerr_container = next((item for item in seerr_pod.get("containers", []) if item["name"] == "servarr"), {})
    if "ALL" not in seerr_container.get("securityContext", {}).get("capabilities", {}).get("drop", []):
        raise ValueError("Seerr must drop all container capabilities")

    qb = next(obj for obj in deployments if obj["metadata"]["name"] == "servarr-qbittorrent")
    pod = qb["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in pod["containers"]}
    if len(pod["containers"]) != 2 or set(containers) != {"tailscale", "servarr"}:
        raise ValueError("qBittorrent must have exactly the application and VPN sidecar")
    env = {item["name"]: item for item in containers["servarr"].get("env", [])}
    if env.get("PUID", {}).get("value") != "1000":
        raise ValueError("qBittorrent PUID must match the VPN guard")
    volumes = {volume["name"]: volume for volume in pod.get("volumes", [])}
    for name, claim in (("config", "servarr-qbittorrent"), ("media", "servarr-jellyfin-media")):
        if volumes.get(name, {}).get("persistentVolumeClaim", {}).get("claimName") != claim:
            raise ValueError(f"qBittorrent {name} PVC does not match the existing deployment")

    guard = yaml.safe_load((HERE / "qbittorrent-vpn.patch.yaml").read_text())
    guard_pod = guard["spec"]["template"]["spec"]
    init = {item["name"]: item for item in pod.get("initContainers", [])}
    expected_guard = guard_pod["initContainers"][0]
    if init.get(expected_guard["name"]) != expected_guard:
        raise ValueError("qBittorrent VPN network guard is missing or changed")
    expected_tail = guard_pod["containers"][0]
    for key in ("lifecycle", "readinessProbe"):
        if containers["tailscale"].get(key) != expected_tail[key]:
            raise ValueError(f"Tailscale {key} is missing or changed")

    sidecar = yaml.safe_load((HERE / "qbittorrent-sidecar.yaml").read_text())
    expected_sidecar = sidecar["spec"]["template"]["spec"]["containers"][0]
    for key in ("image", "securityContext"):
        if containers["tailscale"].get(key) != expected_sidecar[key]:
            raise ValueError(f"Tailscale {key} is missing or changed")
    actual_env = {item["name"]: item for item in containers["tailscale"].get("env", [])}
    if actual_env != {item["name"]: item for item in expected_sidecar["env"]}:
        raise ValueError("Tailscale exit-node or credential configuration changed")


def main():
    try:
        verify(yaml.safe_load_all(sys.stdin))
    except (ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"Servarr render validation failed: {exc}", file=sys.stderr)
        return 1
    print("Servarr render validation passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
