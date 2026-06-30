from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

BASE_DIR = Path(__file__).resolve().parent
IMAGE_CATALOG_FILE = BASE_DIR / "catalogs" / "image_catalog.yml"

TAILSCALE_POLICY_DEFAULTS: Dict[str, Any] = {
    "tailnet": "tail00c2ce.ts.net",
    "admin_group": "group:admins",
    "rpi5_tag": "tag:rpi5",
    "rpi_bplus_tag": "tag:rpi-bplus",
    "homelab_tag": "tag:homelab",
    "pi5_subnets": ["192.168.174.0/24", "192.168.175.0/24"],
}


def _load_image_catalog() -> List[Dict[str, Any]]:
    if not IMAGE_CATALOG_FILE.exists():
        return []

    try:
        loaded = yaml.safe_load(IMAGE_CATALOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(loaded, dict):
        return []

    catalog = loaded.get("image_catalog", [])
    if not isinstance(catalog, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for item in catalog:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        entry["id"] = str(entry.get("id", "")).strip()
        if not entry["id"]:
            continue
        entry["label"] = str(entry.get("label", "")).strip()
        entry["distro"] = str(entry.get("distro", "")).strip()
        entry["release"] = str(entry.get("release", "")).strip()
        entry["arch"] = str(entry.get("arch", "")).strip()
        profiles = entry.get("profiles", [])
        entry["profiles"] = [str(v).strip() for v in profiles if str(v).strip()] if isinstance(profiles, list) else []
        entry["recommended"] = bool(entry.get("recommended", False))
        entry["image_url"] = str(entry.get("image_url", "")).strip()
        entry["image_sha256"] = str(entry.get("image_sha256", "")).strip()
        entry["docs_url"] = str(entry.get("docs_url", "")).strip()
        entry["notes"] = str(entry.get("notes", "")).strip()
        normalized.append(entry)

    return normalized

NODE_PROVISION_PROFILES: List[Dict[str, Any]] = [
    {
        "id": "baseline",
        "label": "Baseline",
        "description": "Core system baseline and host-level defaults.",
        "tags": ["baseline"],
        "groups": ["rpi5_active", "rpi_bplus_active"],
        "input_specs": [
            {
                "id": "timezone",
                "label": "Timezone",
                "placeholder": "UTC",
                "required": False,
            },
            {
                "id": "ntp_servers",
                "label": "NTP Servers (comma-separated)",
                "placeholder": "pool.ntp.org",
                "required": False,
            },
        ],
    },
    {
        "id": "tailscale",
        "label": "Tailscale",
        "description": "Tailnet agent and related ACL tag setup.",
        "tags": ["tailscale"],
        "groups": ["rpi5_active", "rpi_bplus_active"],
        "input_specs": [
            {
                "id": "auth_key",
                "label": "Auth Key",
                "placeholder": "tskey-auth-...",
                "required": True,
                "sensitive": True,
            },
            {
                "id": "tailnet",
                "label": "Tailnet",
                "placeholder": "tail00c2ce.ts.net",
                "required": True,
            },
            {
                "id": "advertise_tags",
                "label": "Advertise Tags (comma-separated)",
                "placeholder": "tag:rpi5,tag:homelab",
                "required": False,
            },
        ],
    },
    {
        "id": "docker",
        "label": "Docker",
        "description": "Container runtime provisioning and Docker defaults.",
        "tags": ["docker"],
        "groups": ["rpi5_active"],
        "input_specs": [
            {
                "id": "docker_data_root",
                "label": "Docker Data Root",
                "placeholder": "/var/lib/docker",
                "required": False,
            },
            {
                "id": "registry_mirror",
                "label": "Registry Mirror",
                "placeholder": "https://mirror.gcr.io",
                "required": False,
            },
        ],
    },
    {
        "id": "kubernetes",
        "label": "Kubernetes",
        "description": "Kubernetes bootstrap defaults for control-plane or worker nodes.",
        "tags": ["kubernetes"],
        "groups": ["rpi5_active"],
        "input_specs": [
            {
                "id": "cluster_name",
                "label": "Cluster Name",
                "placeholder": "rack-cluster",
                "required": True,
            },
            {
                "id": "node_role",
                "label": "Node Role",
                "placeholder": "control-plane or worker",
                "required": True,
            },
            {
                "id": "pod_cidr",
                "label": "Pod CIDR",
                "placeholder": "10.244.0.0/16",
                "required": False,
            },
        ],
    },
    {
        "id": "telegraf",
        "label": "Telegraf",
        "description": "Telegraf provisioning with InfluxDB bucket credentials.",
        "tags": ["monitoring", "telegraf"],
        "groups": ["rpi5_active", "rpi_bplus_active"],
        "input_specs": [
            {
                "id": "influx_endpoint",
                "label": "Influx Endpoint",
                "placeholder": "https://influx.example:8086",
                "required": True,
            },
            {
                "id": "influx_bucket",
                "label": "Influx Bucket",
                "placeholder": "telegraf",
                "required": True,
            },
            {
                "id": "influx_org",
                "label": "Influx Org",
                "placeholder": "homelab",
                "required": True,
            },
            {
                "id": "influx_access_key",
                "label": "Influx Access Key",
                "placeholder": "token or key",
                "required": True,
                "sensitive": True,
            },
            {
                "id": "influx_secret_key",
                "label": "Influx Secret Key",
                "placeholder": "secret",
                "required": True,
                "sensitive": True,
            },
        ],
    },
]

IMAGE_CATALOG = _load_image_catalog()
