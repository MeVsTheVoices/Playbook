#!/usr/bin/env python3
"""Apply opt-in Tailscale exposure to a Helm-rendered Service."""

import json
import re
import sys

import yaml


def render(text, config):
    chunks = re.split(r"^---[ \t]*(?:\#.*)?\r?$", text, flags=re.MULTILINE)
    documents = [yaml.safe_load(chunk) for chunk in chunks]
    namespace = config["namespace"]
    services = [
        doc for doc in documents
        if isinstance(doc, dict) and doc.get("apiVersion") == "v1" and doc.get("kind") == "Service"
        and (doc.get("metadata") or {}).get("name") == config["service"]
        and (doc.get("metadata") or {}).get("namespace", namespace) == namespace
    ]
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
        print(f"Tailscale post-render failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
