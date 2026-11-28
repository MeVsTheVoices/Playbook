#!/usr/bin/env python3
"""Apply opt-in Servarr VPN and Tailscale Service exposure to Helm output."""

import json
import re
import sys
from pathlib import Path

import yaml


def _servarr(documents, chunks, config):
    from services.helm import _servarr_vpn

    vpn = _servarr_vpn(config["servarr_vpn"])
    name = f"{config['release']}-qbittorrent"
    matches = [
        doc for doc in documents if isinstance(doc, dict) and doc.get("apiVersion") == "apps/v1"
        and doc.get("kind") == "Deployment" and (doc.get("metadata") or {}).get("name") == name
        and (doc.get("metadata") or {}).get("namespace", config["namespace"]) == config["namespace"]
    ]
    if len(matches) != 1:
        raise ValueError(f"VPN requires exactly one Deployment named {name}.")
    deployment = matches[0]
    pod = deployment["spec"]["template"]["spec"]
    containers = pod.get("containers", [])
    if len(containers) != 1 or containers[0].get("name") != "servarr" or pod.get("initContainers"):
        raise ValueError("VPN requires the unmodified upstream qBittorrent container and no other containers.")
    app = containers[0]
    image = app.get("image", "").split(":")[0]
    env = {item.get("name"): item for item in app.get("env", [])}
    security = app.get("securityContext") or {}
    if (image not in {"lscr.io/linuxserver/qbittorrent", "linuxserver/qbittorrent"}
            or env.get("PUID", {}).get("value") != vpn["puid"]
            or pod.get("hostNetwork") or pod.get("shareProcessNamespace")
            or app.get("command") or app.get("args")
            or security.get("privileged") or "NET_ADMIN" in (security.get("capabilities") or {}).get("add", [])):
        raise ValueError("VPN requires the LinuxServer image, matching PUID, and an unprivileged pod without host networking.")
    assets = Path(config["chart"]) / "playbook-vpn"
    sidecar = yaml.safe_load((assets / "qbittorrent-sidecar.yaml").read_text(encoding="utf-8"))["spec"]["template"]["spec"]["containers"][0]
    patch = yaml.safe_load((assets / "qbittorrent-vpn.patch.yaml").read_text(encoding="utf-8"))["spec"]["template"]["spec"]
    sidecar.update(patch["containers"][0] | {"name": "tailscale"})
    sidecar_env = {item["name"]: item for item in sidecar["env"]}
    sidecar_env["OAUTH_SECRET"]["valueFrom"]["secretKeyRef"]["name"] = vpn["secret_name"]
    sidecar_env["TS_HOSTNAME"]["value"] = vpn["hostname"]
    sidecar_env["TS_EXTRA_ARGS"]["value"] = f"--advertise-tags={vpn['tag']} --exit-node={vpn['exit_node']}"
    guard = patch["initContainers"][0]
    script = guard["command"][-1]
    for old, new in (("10.42.0.0/16", vpn["pod_cidr"]), ("10.43.0.10", vpn["cluster_dns"]), ("1000", vpn["puid"])):
        script = script.replace(old, new)
    guard["command"][-1] = script
    pod["initContainers"] = [guard]
    containers.insert(0, sidecar)
    index = next(index for index, doc in enumerate(documents) if doc is deployment)
    chunks[index] = "\n" + yaml.safe_dump(deployment, sort_keys=False)


def render(text, config):
    chunks = re.split(r"^---[ \t]*(?:\#.*)?\r?$", text, flags=re.MULTILINE)
    documents = [yaml.safe_load(chunk) for chunk in chunks]
    namespace = config["namespace"]
    if (config.get("servarr_vpn") or {}).get("enabled"):
        _servarr(documents, chunks, config)
    if config["mode"] == "off":
        return "---".join(chunks)
    rendered_services = [
        doc for doc in documents
        if isinstance(doc, dict) and doc.get("apiVersion") == "v1" and doc.get("kind") == "Service"
        and (doc.get("metadata") or {}).get("namespace", namespace) == namespace
    ]
    services = [doc for doc in rendered_services if (doc.get("metadata") or {}).get("name") == config["service"]]
    if not services:
        names = sorted({str(doc["metadata"]["name"]) for doc in rendered_services if (doc.get("metadata") or {}).get("name")})
        available = f"Rendered Services: {', '.join(names[:10])}{', ...' if len(names) > 10 else ''}." if names else "This chart renders no Services in that namespace."
        raise ValueError(
            f"No rendered Service named {config['service']} in {namespace}. {available} "
            "Set Tailscale exposure to Off and save, then preview to find a Service name; "
            "leave exposure Off if the workload has no Service."
        )
    if len(services) != 1:
        raise ValueError(f"Expected exactly one rendered Service named {config['service']} in {namespace}; found {len(services)}.")
    service = services[0]
    spec = service.get("spec") or {}
    if spec.get("type") == "ExternalName":
        raise ValueError("ExternalName Services cannot be exposed using this configuration.")
    annotations = {}
    for field, annotation in (("proxy_class", "proxy-class"), ("proxy_group", "proxy-group"), ("tags", "tags")):
        if config[field]:
            annotations[f"tailscale.com/{annotation}"] = config[field]
    if config["mode"] == "service":
        metadata = service.setdefault("metadata", {})
        existing = metadata.setdefault("annotations", {})
        additions = {**annotations, "tailscale.com/hostname": config["hostname"]}
        if config["proxy_group"]:
            if existing.get("tailscale.com/expose") == "true":
                raise ValueError("The chart already enables standalone Tailscale exposure on this Service.")
            if spec.get("type", "ClusterIP") not in {"ClusterIP", "LoadBalancer"} or spec.get("loadBalancerClass") not in (None, "tailscale"):
                raise ValueError("ProxyGroup exposure requires a ClusterIP or Tailscale LoadBalancer Service.")
            spec["type"] = "LoadBalancer"
            spec["loadBalancerClass"] = "tailscale"
        else:
            additions["tailscale.com/expose"] = "true"
        if any(key in existing and existing[key] != value for key, value in additions.items()):
            raise ValueError("The chart already defines conflicting Tailscale Service annotations.")
        existing.update(additions)
        index = next(index for index, doc in enumerate(documents) if doc is service)
        chunks[index] = "\n" + yaml.safe_dump(service, sort_keys=False)
    else:
        port = int(config["port"])
        if not any(item.get("port") == port and item.get("protocol", "TCP") == "TCP" for item in spec.get("ports", []) if isinstance(item, dict)):
            raise ValueError(f"Service {config['service']} does not expose TCP port {port}.")
        name = f"{config['release']}-tailscale"
        if any(isinstance(doc, dict) and doc.get("kind") == "Ingress" and (doc.get("metadata") or {}).get("name") == name
               and (doc.get("metadata") or {}).get("namespace", namespace) == namespace for doc in documents):
            raise ValueError(f"The chart already defines Ingress {name}.")
        ingress = {
            "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
            "metadata": {"name": name, "namespace": namespace, "annotations": annotations},
            "spec": {
                "ingressClassName": "tailscale", "tls": [{"hosts": [config["hostname"]]}],
                "rules": [{"http": {"paths": [{"path": "/", "pathType": "Prefix", "backend": {
                    "service": {"name": config["service"], "port": {"number": port}}
                }}]}}],
            },
        }
        chunks.append("\n" + yaml.safe_dump(ingress, sort_keys=False))
    return "---".join(chunks)


def main():
    try:
        with open(sys.argv[1], encoding="utf-8") as file:
            config = json.load(file)
        sys.stdout.write(render(sys.stdin.read(), config))
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
        print(f"Helm post-render failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
