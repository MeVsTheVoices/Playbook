from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_IMAGE_CATALOG_FILE = BASE_DIR / "catalogs" / "image_catalog.yml"
IMAGE_CATALOG_FILE = Path(os.getenv("CALLBACK_IMAGE_CATALOG_FILE", str(_DEFAULT_IMAGE_CATALOG_FILE))).expanduser()

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
        text = IMAGE_CATALOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return []

    loaded: Any = None
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
    except Exception:
        try:
            loaded = json.loads(text)
        except Exception:
            loaded = _parse_image_catalog_yaml(text)

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


def _parse_image_catalog_yaml(text: str) -> Dict[str, Any]:
    def parse_scalar(raw: str) -> Any:
        value = raw.strip()
        if value in {"true", "True"}:
            return True
        if value in {"false", "False"}:
            return False
        if value in {"null", "None", "~"}:
            return None
        if len(value) >= 2 and value[0] == value[-1] == '"':
            return value[1:-1]
        if len(value) >= 2 and value[0] == value[-1] == "'":
            return value[1:-1]
        if value.isdigit():
            return int(value)
        return value

    data: Dict[str, Any] = {"version": 1, "image_catalog": []}
    current_item: Dict[str, Any] | None = None
    current_list_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            current_item = None
            current_list_key = None
            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key == "version":
                data["version"] = parse_scalar(value)
            elif key == "image_catalog":
                data["image_catalog"] = []
            continue

        if stripped.startswith("- ") and current_list_key:
            if current_item is None:
                continue
            bucket = current_item.get(current_list_key)
            if not isinstance(bucket, list):
                bucket = []
                current_item[current_list_key] = bucket
            bucket.append(parse_scalar(stripped[2:]))
            continue

        if indent == 2 and stripped.startswith("- "):
            if current_item:
                data["image_catalog"].append(current_item)
            current_item = {}
            current_list_key = None
            remainder = stripped[2:].strip()
            if remainder and ":" in remainder:
                key, value = remainder.split(":", 1)
                current_item[key.strip()] = parse_scalar(value)
            continue

        if current_item is None or ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            current_item[key] = []
            current_list_key = key
        else:
            current_item[key] = parse_scalar(value)
            current_list_key = None

    if current_item:
        data["image_catalog"].append(current_item)
    return data

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
