from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from k3s_lightweight import node_ops_specs as lightweight_node_ops_specs
from k3s_lightweight import node_role_spec_parts
from tailscale_policy import policy_defaults
from network_toolkit import GROUP as TOOLKIT_GROUP, INPUT_SPECS as TOOLKIT_INPUT_SPECS
from playbook_server import GROUP as SERVER_GROUP, INPUT_SPECS as SERVER_INPUT_SPECS

BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_IMAGE_CATALOG_FILE = BASE_DIR / "catalogs" / "image_catalog.yml"
IMAGE_CATALOG_FILE = Path(os.getenv("CALLBACK_IMAGE_CATALOG_FILE", str(_DEFAULT_IMAGE_CATALOG_FILE))).expanduser()

TAILSCALE_POLICY_DEFAULTS: Dict[str, Any] = policy_defaults()


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

# NucBox over Tailscale; etcd peer TLS is IP-sensitive, so never the NATed LAN address.
K3S_API_ENDPOINT_DEFAULT = "https://100.65.24.76:6443"

NODE_PROVISION_PROFILES: List[Dict[str, Any]] = [
    {
        "id": "cgroups",
        "label": "Pi Cgroups",
        "description": "Ensure cpuset and memory boot flags in the existing Pi cmdline. Reboot manually to activate; no automatic reboot.",
        "tags": ["cgroups"],
        "groups": ["rpi5_active", "rpi4_active", "rpi_3bplus_active", TOOLKIT_GROUP],
        "input_specs": [],
    },
    {
        "id": "systemd",
        "label": "Systemd Service",
        "description": "Manage one existing service. Stopping services can disconnect access; check the selected unit before applying.",
        "tags": ["systemd"],
        "groups": ["rpi5_active", "rpi4_active", "rpi_3bplus_active", TOOLKIT_GROUP, SERVER_GROUP],
        "input_specs": [
            {"id": "service_name", "label": "Service unit", "placeholder": "docker.service",
             "input_type": "text", "required": True},
            {"id": "service_state", "label": "Runtime state", "input_type": "select",
             "options": ["started", "stopped"], "default": "started", "required": True},
            {"id": "service_enabled", "label": "Start at boot", "input_type": "select",
             "options": ["true", "false"], "default": "true", "required": True},
        ],
    },
    {
        "id": "network-toolkit",
        "label": "Network Toolkit (Pi 3B+)",
        "description": "Serial/Ethernet tools, a restricted browser terminal and opt-in authenticated Tailscale Funnel. Wi-Fi uplink; no forwarding.",
        "tags": ["network-toolkit"],
        "groups": [TOOLKIT_GROUP],
        "input_specs": TOOLKIT_INPUT_SPECS,
    },
    {
        "id": "playbook-server",
        "label": "Playbook Server (Ubuntu)",
        "description": "Plain Ubuntu Server running this dashboard under Docker from a git ref, with the host SD/USB flasher. Re-run to update.",
        "tags": ["playbook-server"],
        "groups": [SERVER_GROUP],
        "input_specs": SERVER_INPUT_SPECS,
    },
    {
        "id": "baseline",
        "label": "Baseline",
        "description": "Core system baseline and host-level defaults.",
        "tags": ["baseline"],
        "groups": ["rpi5_active", "rpi4_active", "rpi_3bplus_active", TOOLKIT_GROUP],
        "input_specs": [
            {
                "id": "timezone",
                "label": "Timezone",
                "placeholder": "UTC",
                "input_type": "text",
                "required": False,
            },
            {
                "id": "ntp_servers",
                "label": "NTP Servers (comma-separated)",
                "placeholder": "pool.ntp.org",
                "input_type": "csv",
                "required": False,
            },
            {
                "id": "packages",
                "label": "Extra Packages (comma-separated)",
                "placeholder": "git,curl,htop",
                "input_type": "csv",
                "required": False,
            },
            {
                "id": "enable_unattended_upgrades",
                "label": "Enable unattended upgrades",
                "input_type": "checkbox",
                "required": False,
            },
            {
                "id": "enable_fail2ban",
                "label": "Install and enable fail2ban",
                "input_type": "checkbox",
                "required": False,
            },
            {
                "id": "enable_hardware_checks",
                "label": "Enable hardware detection",
                "input_type": "checkbox",
                "default": True,
                "required": False,
            },
        ],
    },
    {
        "id": "tailscale",
        "label": "Tailscale",
        "description": "Tailnet agent and related ACL tag setup.",
        "tags": ["tailscale"],
        "groups": ["rpi5_active", "rpi4_active", "rpi_3bplus_active", TOOLKIT_GROUP],
        "input_specs": [
            {
                "id": "auth_key",
                "label": "Auth Key",
                "placeholder": "tskey-auth-...",
                "input_type": "text",
                "required": True,
                "sensitive": True,
            },
            {
                "id": "tailnet",
                "label": "Tailnet",
                "placeholder": "tail00c2ce.ts.net",
                "input_type": "text",
                "required": True,
            },
            {
                "id": "hostname",
                "label": "Tailscale Hostname",
                "placeholder": "rack-node-01",
                "input_type": "text",
                "required": False,
            },
            {
                "id": "node_ip",
                "label": "Tailscale IPv4 Address (for Kubernetes node IP)",
                "placeholder": "100.x.y.z",
                "input_type": "text",
                "required": False,
            },
            {
                "id": "advertise_tags",
                "label": "Advertise Tags (comma-separated)",
                "placeholder": "tag:rpi5,tag:homelab",
                "input_type": "csv",
                "required": False,
            },
            {
                "id": "advertise_exit_node",
                "label": "Advertise as exit node",
                "input_type": "checkbox",
                "required": False,
            },
            {
                "id": "enable_ssh",
                "label": "Enable Tailscale SSH",
                "input_type": "checkbox",
                "required": False,
            },
        ],
    },
    {
        "id": "docker",
        "label": "Docker",
        "description": "Container runtime provisioning and Docker defaults.",
        "tags": ["docker"],
        "groups": ["rpi5_active", "rpi4_active"],
        "input_specs": [
            {
                "id": "docker_data_root",
                "label": "Docker Data Root",
                "placeholder": "/var/lib/docker",
                "input_type": "text",
                "required": False,
            },
            {
                "id": "registry_mirror",
                "label": "Registry Mirror",
                "placeholder": "https://mirror.gcr.io",
                "input_type": "text",
                "required": False,
            },
        ],
    },
    {
        "id": "kubernetes",
        "label": "Kubernetes",
        "description": "Kubernetes role and join settings. Pi 5 and Pi 4: worker, control-plane or utility (no Kubernetes). Pi 3B+: light-worker, utility, or (not recommended) control-plane.",
        "tags": ["kubernetes"],
        "groups": ["rpi5_active", "rpi4_active", "rpi_3bplus_active"],
        "input_specs": [
            {
                "id": "node_role",
                "label": "Node Role",
                "input_type": "select",
                "options": ["worker"],
                **node_role_spec_parts(),
                "default": "worker",
                "hint": "Pi 5 and Pi 4: worker, control-plane or utility. Pi 3B+: light-worker (tainted agent), utility, or control-plane (not recommended). A Pi 4 control-plane needs an SSD: K3s says SD cards cannot handle etcd. Keep the number of K3s servers odd: the NucBox plus two control-plane Pis makes three.",
                "required": True,
            },
            {
                "id": "server_endpoint",
                "label": "K3s Server Endpoint",
                "placeholder": K3S_API_ENDPOINT_DEFAULT,
                "input_type": "text",
                "default": K3S_API_ENDPOINT_DEFAULT,
                "required": True,
                "optional_for_roles": ["utility"],
            },
            {
                "id": "server_token",
                "label": "K3s Server Token",
                "placeholder": "K10...::server:...",
                "input_type": "text",
                "sensitive": True,
                "required": True,
                "optional_for_roles": ["utility"],
            },
            {
                "id": "node_ip",
                "label": "Node Advertise IP (blank = read from Tailscale on the node)",
                "placeholder": "100.x.y.z",
                "input_type": "text",
                "required": False,
            },
            {
                "id": "k3s_config",
                "label": "Advanced K3s Config Override (replaces generated config)",
                "placeholder": "# pragma: k3s-config-v1",
                "input_type": "textarea",
                "check": "k3s_config",
                "required": False,
            },
            {
                "id": "k3s_security_baseline",
                "label": "Enable cluster secrets encryption (control-plane only)",
                "warning": "Keep off for now: the current cluster has encryption disabled, so a node joining with this on will fail.",
                "input_type": "checkbox",
                "required": False,
            },
            # Role options, each shown only for the roles it applies to (only_roles) on both boards.
            *lightweight_node_ops_specs(),
        ],
    },
    {
        "id": "longhorn",
        "label": "Longhorn",
        "description": "Optional Longhorn install and node prerequisites for Pi 5 Kubernetes nodes.",
        "tags": ["longhorn"],
        "groups": ["rpi5_active"],
        "input_specs": [
            {
                "id": "storage_path",
                "label": "Longhorn Storage Path",
                "placeholder": "/var/lib/longhorn",
                "input_type": "text",
                "required": False,
            },
            {
                "id": "storage_disk",
                "label": "Storage Disk (auto = the one blank disk, a device like /dev/nvme0n1, or none for a plain directory)",
                "placeholder": "auto",
                "input_type": "text",
                "default": "auto",
                "warning": "A blank disk is partitioned, formatted and mounted at the storage path. Disks that already hold data are never touched.",
                "required": False,
            },
            {
                "id": "default_replica_count",
                "label": "Default Replica Count",
                "placeholder": "2",
                "input_type": "text",
                "required": False,
            },
            {
                "id": "set_default_storage_class",
                "label": "Set Longhorn as default StorageClass",
                "input_type": "checkbox",
                "default": True,
                "required": False,
            },
            {
                "id": "install_chart",
                "label": "Install Longhorn chart on k3s server nodes",
                "input_type": "checkbox",
                "default": True,
                "required": False,
            },
        ],
    },
    {
        "id": "telegraf",
        "label": "Telegraf",
        "description": "Telegraf provisioning with InfluxDB bucket credentials.",
        "tags": ["monitoring", "telegraf"],
        "groups": ["rpi5_active", "rpi4_active", "rpi_3bplus_active"],
        "input_specs": [
            {
                "id": "influx_endpoint",
                "label": "Influx Endpoint",
                "placeholder": "https://influx.example:8086",
                "input_type": "text",
                "required": True,
            },
            {
                "id": "influx_bucket",
                "label": "Influx Bucket",
                "placeholder": "telegraf",
                "input_type": "text",
                "required": True,
            },
            {
                "id": "influx_org",
                "label": "Influx Org",
                "placeholder": "homelab",
                "input_type": "text",
                "required": True,
            },
            {
                "id": "influx_access_key",
                "label": "Influx Access Key",
                "placeholder": "token or key",
                "input_type": "text",
                "required": True,
                "sensitive": True,
            },
            {
                "id": "influx_secret_key",
                "label": "Influx Secret Key",
                "placeholder": "only for the access_key:secret_key token form",
                "input_type": "text",
                "required": False,
                "sensitive": True,
            },
            {
                "id": "influx_token_mode",
                "label": "Influx Token Form",
                "input_type": "select",
                "options": ["access_key", "access_key:secret_key"],
                "default": "access_key",
                "required": False,
                "hint": "What Telegraf sends as the Influx token: the access key alone, or access_key:secret_key. Test Influx connection finds out which one your server accepts.",
            },
            {
                "id": "telegraf_config_url",
                "label": "Telegraf Config URL",
                "placeholder": "https://influx.example/api/v2/telegrafs/<id>",
                "input_type": "text",
                "required": False,
            },
        ],
    },
]

IMAGE_CATALOG = _load_image_catalog()
