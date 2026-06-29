from __future__ import annotations

import json
import os
import hashlib
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from urllib import parse as urlparse
from urllib import error as urlerror
from urllib import request as urlrequest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

BASE_DIR = Path(__file__).resolve().parent
APPDATA_DIR = Path(os.getenv("CALLBACK_APPDATA_DIR", str(BASE_DIR / "data"))).expanduser()
DATA_DIR = APPDATA_DIR
DATA_FILE = DATA_DIR / "callbacks.jsonl"
IMAGE_HISTORY_FILE = DATA_DIR / "image_download_catalog.jsonl"
IMAGE_CACHE_DIR = DATA_DIR / "image_cache"
KEYS_DIR = DATA_DIR / "ssh_keys"
KEY_ARCHIVE_DIR = DATA_DIR / "ssh_key_archive"
KEY_ARCHIVE_INDEX_FILE = DATA_DIR / "ssh_key_archive.jsonl"
NODE_CONFIG_FILE = DATA_DIR / "node_monitor_config.json"
NODE_PROVISION_FILE = DATA_DIR / "node_provisioning.json"
MONITOR_STATE_FILE = DATA_DIR / "node_monitor_state.json"
APP_SETTINGS_FILE = DATA_DIR / "app_settings.json"
INVENTORY_FILE = BASE_DIR.parent / "inventory.yml"
SCRIPT_PATH = BASE_DIR.parent / "scripts" / "prepare_sd_cloud_init.sh"
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
CATALOG_PULL_JOBS: Dict[str, Dict[str, Any]] = {}
CATALOG_PULL_JOBS_LOCK = threading.Lock()
DEVICE_SUBSCRIBERS: List[queue.Queue[str]] = []
DEVICE_SUBSCRIBERS_LOCK = threading.Lock()
DEVICE_MONITOR_STARTED = False
DEVICE_MONITOR_LOCK = threading.Lock()
TAILSCALE_POLICY_DEFAULTS: Dict[str, Any] = {
    "tailnet": "tail00c2ce.ts.net",
    "admin_group": "group:admins",
    "rpi5_tag": "tag:rpi5",
    "rpi_bplus_tag": "tag:rpi-bplus",
    "homelab_tag": "tag:homelab",
    "pi5_subnets": ["192.168.174.0/24", "192.168.175.0/24"],
}

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

IMAGE_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "ubuntu-24.04-pi-arm64",
        "label": "Ubuntu Server 24.04 LTS (Raspberry Pi ARM64)",
        "distro": "Ubuntu",
        "release": "24.04 LTS",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": True,
        "image_url": "https://old-releases.ubuntu.com/releases/24.04/ubuntu-24.04.2-preinstalled-server-arm64+raspi.img.xz",
        "image_sha256": "07fe39e530381cee7095c16f54d05fd99db2fe8233e05ebf8948437e40b5feaa",
        "docs_url": "https://ubuntu.com/download/raspberry-pi",
        "notes": "Pinned to old-releases for reliable direct download with curl.",
    },
    {
        "id": "ubuntu-22.04-pi-arm64",
        "label": "Ubuntu Server 22.04 LTS (Raspberry Pi ARM64)",
        "distro": "Ubuntu",
        "release": "22.04 LTS",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "https://old-releases.ubuntu.com/releases/22.04/ubuntu-22.04.4-preinstalled-server-arm64+raspi.img.xz",
        "image_sha256": "8985915e3840d1e3971780aa160791e0665f64a2ee7e52ef8285c0701a8f6d6b",
        "docs_url": "https://ubuntu.com/download/raspberry-pi",
        "notes": "Useful for older automation or package compatibility.",
    },
    {
        "id": "debian-bookworm-arm64",
        "label": "Debian 12 Bookworm (ARM64)",
        "distro": "Debian",
        "release": "12 Bookworm",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://raspi.debian.net/tested-images/",
        "notes": "Provide image URL manually from Debian Raspberry Pi tested images.",
    },
    {
        "id": "debian-bookworm-armhf",
        "label": "Debian 12 Bookworm (ARMHF)",
        "distro": "Debian",
        "release": "12 Bookworm",
        "arch": "armhf",
        "profiles": ["pibplus"],
        "recommended": True,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://raspi.debian.net/tested-images/",
        "notes": "Good baseline for Pi B+ 32-bit class hardware.",
    },
    {
        "id": "raspios-bookworm-arm64-lite",
        "label": "Raspberry Pi OS Lite (Bookworm, ARM64)",
        "distro": "Raspberry Pi OS",
        "release": "Bookworm",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://www.raspberrypi.com/software/operating-systems/",
        "notes": "Provide a direct Lite image URL from official Raspberry Pi downloads.",
    },
    {
        "id": "raspios-bookworm-armhf-lite",
        "label": "Raspberry Pi OS Lite (Bookworm, 32-bit)",
        "distro": "Raspberry Pi OS",
        "release": "Bookworm",
        "arch": "armhf",
        "profiles": ["pibplus"],
        "recommended": True,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://www.raspberrypi.com/software/operating-systems/",
        "notes": "Recommended family for Pi B+ when Ubuntu ARM64 is not suitable.",
    },
    {
        "id": "fedora-iot-arm64",
        "label": "Fedora IoT (ARM64)",
        "distro": "Fedora",
        "release": "IoT Stable",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://fedoraproject.org/iot/download",
        "notes": "Provide image URL manually from Fedora IoT download page.",
    },
    {
        "id": "fedora-server-arm64",
        "label": "Fedora Server (ARM64)",
        "distro": "Fedora",
        "release": "Server Stable",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://fedoraproject.org/server/download",
        "notes": "General-purpose Fedora Server option for rack nodes.",
    },
    {
        "id": "opensuse-microos-arm64",
        "label": "openSUSE MicroOS (AArch64)",
        "distro": "openSUSE",
        "release": "MicroOS Stable",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://get.opensuse.org/microos/",
        "notes": "Immutable distro option for appliance-like cluster nodes.",
    },
    {
        "id": "ubuntu-core-arm64",
        "label": "Ubuntu Core (ARM64)",
        "distro": "Ubuntu",
        "release": "Core Stable",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://ubuntu.com/core/docs",
        "notes": "Snap-based immutable Ubuntu Core image for constrained use cases.",
    },
    {
        "id": "dietpi-armhf",
        "label": "DietPi (ARMHF)",
        "distro": "DietPi",
        "release": "Stable",
        "arch": "armhf",
        "profiles": ["pibplus"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://dietpi.com/#download",
        "notes": "Lightweight OS option for Raspberry Pi B+ class hardware.",
    },
    {
        "id": "raspios-legacy-bullseye-armhf-lite",
        "label": "Raspberry Pi OS Lite (Bullseye, 32-bit)",
        "distro": "Raspberry Pi OS",
        "release": "Bullseye Legacy",
        "arch": "armhf",
        "profiles": ["pibplus"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://www.raspberrypi.com/software/operating-systems/",
        "notes": "Legacy fallback image for older Pi B+ compatibility paths.",
    },
    {
        "id": "alpine-aarch64",
        "label": "Alpine Linux (AArch64)",
        "distro": "Alpine",
        "release": "Stable",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://www.alpinelinux.org/downloads/",
        "notes": "Great for lightweight containers and appliance-style nodes.",
    },
    {
        "id": "kali-arm64-rpi",
        "label": "Kali Linux Raspberry Pi (ARM64)",
        "distro": "Kali",
        "release": "Rolling",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://www.kali.org/get-kali/#kali-arm",
        "notes": "Security testing distro option for lab nodes.",
    },
    {
        "id": "archlinuxarm-rpi",
        "label": "Arch Linux ARM (Raspberry Pi)",
        "distro": "Arch Linux ARM",
        "release": "Rolling",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "https://archlinuxarm.org/platforms/armv8/broadcom/raspberry-pi-4",
        "notes": "Use with care in production due to rolling updates.",
    },
    {
        "id": "custom-arm64",
        "label": "Custom URL Or Local Image (ARM64)",
        "distro": "Custom",
        "release": "Manual",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "",
        "notes": "Bring your own trusted image URL or local file path.",
    },
    {
        "id": "custom-armhf",
        "label": "Custom URL Or Local Image (ARMHF)",
        "distro": "Custom",
        "release": "Manual",
        "arch": "armhf",
        "profiles": ["pibplus"],
        "recommended": False,
        "image_url": "",
        "image_sha256": "",
        "docs_url": "",
        "notes": "Bring your own trusted image URL or local file path.",
    },
]

app = Flask(__name__)


def _ensure_data_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.touch()


def _ensure_image_history_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not IMAGE_HISTORY_FILE.exists():
        IMAGE_HISTORY_FILE.touch()


def _ensure_image_cache_dir() -> None:
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _default_app_settings() -> Dict[str, Any]:
    return {
        "rack_name": "homelab",
    }


def _sanitize_rack_name(value: str) -> str:
    raw = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    return cleaned[:40] or "homelab"


def _load_app_settings() -> Dict[str, Any]:
    _ensure_data_file()
    defaults = _default_app_settings()
    if not APP_SETTINGS_FILE.exists():
        return defaults

    try:
        loaded = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults

    if not isinstance(loaded, dict):
        return defaults

    rack_name = _sanitize_rack_name(str(loaded.get("rack_name", defaults["rack_name"])))
    return {"rack_name": rack_name}


def _save_app_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = _load_app_settings()
    if "rack_name" in payload:
        settings["rack_name"] = _sanitize_rack_name(str(payload.get("rack_name", "")))
    _ensure_data_file()
    APP_SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return settings


def _tailscale_policy_defaults() -> Dict[str, Any]:
    settings = _load_app_settings()
    rack_name = _sanitize_rack_name(str(settings.get("rack_name", "homelab")))
    defaults = dict(TAILSCALE_POLICY_DEFAULTS)
    defaults["rack_name"] = rack_name
    defaults["homelab_tag"] = f"tag:{rack_name}"
    return defaults


def _resolve_download_path(download_path: str) -> Path | None:
    path_raw = str(download_path or "").strip()
    if not path_raw:
        return None

    candidate = Path(path_raw)
    candidates: List[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.extend(
            [
                (BASE_DIR.parent / candidate),
                (DATA_DIR / candidate),
                (BASE_DIR / candidate),
                candidate,
            ]
        )

    for resolved in candidates:
        if resolved.exists():
            return resolved
    return None


def _ensure_keys_dir() -> None:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(KEYS_DIR, 0o700)
    except PermissionError:
        # Best effort; keep running on filesystems that ignore chmod semantics.
        pass


def _ensure_key_archive_dir() -> None:
    KEY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(KEY_ARCHIVE_DIR, 0o700)
    except PermissionError:
        pass


def _archive_ssh_public_key(job_id: str, form: Dict[str, Any], public_key: str) -> Dict[str, str]:
    _ensure_key_archive_dir()
    if not KEY_ARCHIVE_INDEX_FILE.exists():
        KEY_ARCHIVE_INDEX_FILE.touch()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    profile = str(form.get("profile", "unknown")).strip() or "unknown"
    hostname = str(form.get("hostname", "host")).strip() or "host"
    safe_host = re.sub(r"[^A-Za-z0-9._-]+", "-", hostname)
    archive_name = f"{ts}-{safe_host}-{profile}-{job_id[:8]}.pub"
    archive_path = KEY_ARCHIVE_DIR / archive_name
    archive_path.write_text(public_key.strip() + "\n", encoding="utf-8")

    fingerprint = ""
    try:
        fp_proc = subprocess.run(
            ["ssh-keygen", "-lf", str(archive_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if fp_proc.returncode == 0 and fp_proc.stdout:
            # Output format example: "256 SHA256:abcd... comment (ED25519)"
            parts = fp_proc.stdout.strip().split()
            if len(parts) >= 2:
                fingerprint = parts[1]
    except FileNotFoundError:
        fingerprint = ""

    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "hostname": hostname,
        "profile": profile,
        "device": str(form.get("device", "")).strip(),
        "archive_path": str(archive_path),
        "fingerprint": fingerprint,
    }
    with KEY_ARCHIVE_INDEX_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")

    return {"archive_path": str(archive_path), "fingerprint": fingerprint}


def _append_event(event: Dict[str, Any]) -> None:
    _ensure_data_file()
    with DATA_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def _clear_events() -> int:
    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    DATA_FILE.write_text("", encoding="utf-8")
    return len(lines)


def _read_events(limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    events: List[Dict[str, Any]] = []
    for line in selected:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    events.reverse()
    return events


def _append_image_history(entry: Dict[str, Any]) -> None:
    _ensure_image_history_file()
    with IMAGE_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _image_record_id(entry: Dict[str, Any], raw_line: str = "") -> str:
    seed = raw_line.strip() or json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _normalize_image_record(entry: Dict[str, Any], raw_line: str = "") -> Dict[str, Any]:
    record = dict(entry)
    record.setdefault("id", _image_record_id(record, raw_line=raw_line))
    record.setdefault("label", record.get("source_value") or record.get("download_path") or record.get("hostname") or "Retrieved image")
    record.setdefault("notes", "")
    record.setdefault("updated_at", record.get("recorded_at", ""))
    resolved_path = _resolve_download_path(str(record.get("download_path", "")))
    is_available = bool(resolved_path)
    if resolved_path:
        record["download_path"] = str(resolved_path)
    record["local_available"] = is_available
    record["availability"] = "available" if is_available else "missing"
    return record


def _write_image_history(entries: List[Dict[str, Any]]) -> None:
    _ensure_image_history_file()
    with IMAGE_HISTORY_FILE.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _list_image_records(limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_image_history_file()
    lines = IMAGE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    entries: List[Dict[str, Any]] = []
    for line in selected:
        try:
            entries.append(_normalize_image_record(json.loads(line), raw_line=line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries


def _find_image_record(record_id: str) -> Dict[str, Any] | None:
    for record in _list_image_records(limit=1000):
        if record.get("id") == record_id:
                        return record
    return None


def _upsert_image_record(payload: Dict[str, Any], record_id: str | None = None) -> Dict[str, Any]:
    entries = _list_image_records(limit=5000)
    now = datetime.now(timezone.utc).isoformat()
    normalized = {
        "id": record_id or payload.get("id") or uuid.uuid4().hex[:12],
        "recorded_at": str(payload.get("recorded_at", "")).strip() or now,
        "updated_at": now,
        "profile": str(payload.get("profile", "")).strip(),
        "hostname": str(payload.get("hostname", "")).strip(),
        "source": str(payload.get("source", "")).strip(),
        "source_value": str(payload.get("source_value", "")).strip(),
        "downloaded": bool(payload.get("downloaded", False)),
        "download_path": str(payload.get("download_path", "")).strip(),
        "hash_status": str(payload.get("hash_status", "")).strip(),
        "expected_sha256": str(payload.get("expected_sha256", "")).strip().lower(),
        "observed_sha256": str(payload.get("observed_sha256", "")).strip().lower(),
        "job_status": str(payload.get("job_status", "")).strip(),
        "exit_code": payload.get("exit_code"),
        "label": str(payload.get("label", "")).strip() or "Retrieved image",
        "notes": str(payload.get("notes", "")).strip(),
    }

    replaced = False
    for idx, entry in enumerate(entries):
        if entry.get("id") == normalized["id"]:
            existing = dict(entry)
            existing.update(normalized)
            entries[idx] = existing
            replaced = True
            normalized = existing
            break

    if not replaced:
        entries.append(normalized)

    _write_image_history(entries)
    return normalized


def _delete_image_record(record_id: str) -> bool:
    entries = _list_image_records(limit=5000)
    kept = [entry for entry in entries if entry.get("id") != record_id]
    if len(kept) == len(entries):
        return False
    _write_image_history(kept)
    return True


def _read_image_history(limit: int = 200) -> List[Dict[str, Any]]:
    return _list_image_records(limit=limit)


def _image_state_summary() -> Dict[str, Any]:
    history = _read_image_history(limit=500)
    available_urls = _available_image_urls_from_history(limit=5000)
    catalog_available = sum(
        1
        for entry in IMAGE_CATALOG
        if _catalog_entry_with_availability(entry, available_urls=available_urls).get("local_available")
    )
    latest = history[0] if history else {}
    latest_label = str(latest.get("label") or latest.get("source_value") or latest.get("download_path") or "-")
    latest_status = str(latest.get("job_status") or latest.get("hash_status") or "-")
    latest_recorded_at = str(latest.get("recorded_at") or "")
    latest_available = bool(latest.get("local_available", False))
    cache_path = str(latest.get("download_path") or latest.get("cache_path") or "")
    return {
        "total_records": len(history),
        "latest_label": latest_label,
        "latest_status": latest_status,
        "latest_recorded_at": latest_recorded_at,
        "latest_available": latest_available,
        "latest_cache_path": cache_path,
        "available_catalog_entries": catalog_available,
        "catalog_entries": len(IMAGE_CATALOG),
    }


def _record_image_history(form: Dict[str, Any], stdout: str, exit_code: int) -> None:
    image_url = str(form.get("image_url", "")).strip()
    image_file = str(form.get("image_file", "")).strip()
    expected_sha = str(form.get("image_sha256", "")).strip().lower()

    download_match = re.search(r"Download complete:\s*(.+)", stdout)
    verified_match = re.search(r"Image hash verified:\s*([A-Fa-f0-9]{64})", stdout)
    fail_expected_match = re.search(r"Expected:\s*([A-Fa-f0-9]{64})", stdout)
    fail_actual_match = re.search(r"Actual:\s*([A-Fa-f0-9]{64})", stdout)

    if verified_match:
        hash_status = "verified"
    elif "Image hash verification failed" in stdout:
        hash_status = "failed"
    elif "Hash verification skipped" in stdout:
        hash_status = "skipped"
    elif expected_sha:
        hash_status = "provided"
    else:
        hash_status = "unknown"

    source = "image_url" if image_url else "image_file" if image_file else "default"
    source_value = image_url or image_file
    record = {
        "id": uuid.uuid4().hex[:12],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "profile": str(form.get("profile", "")).strip(),
        "hostname": str(form.get("hostname", "")).strip(),
        "source": source,
        "source_value": source_value,
        "downloaded": bool(download_match),
        "download_path": download_match.group(1).strip() if download_match else "",
        "hash_status": hash_status,
        "expected_sha256": (fail_expected_match.group(1).lower() if fail_expected_match else expected_sha),
        "observed_sha256": (
            verified_match.group(1).lower()
            if verified_match
            else fail_actual_match.group(1).lower()
            if fail_actual_match
            else ""
        ),
        "job_status": "completed" if exit_code == 0 else "failed",
        "exit_code": exit_code,
    }
    _append_image_history(record)


def _safe_catalog_filename(url: str, entry_id: str) -> str:
    parsed = urlparse.urlparse(url)
    name = Path(parsed.path).name or f"{entry_id}.img"
    safe = re.sub(r"[^A-Za-z0-9._+-]+", "-", name).strip(".-")
    if not safe:
        safe = f"{entry_id}.img"
    return f"{entry_id}-{safe}"


def _download_catalog_image(url: str, target: Path) -> str:
    req = urlrequest.Request(url, headers={"User-Agent": "callback-site-catalog-pull/1.0"})
    digest = hashlib.sha256()
    with urlrequest.urlopen(req, timeout=60) as resp, target.open("wb") as dst:  # nosec B310
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _catalog_entry_with_availability(entry: Dict[str, Any], available_urls: set[str] | None = None) -> Dict[str, Any]:
    enriched = dict(entry)
    image_url = str(entry.get("image_url", "")).strip()
    if not image_url:
        enriched["local_available"] = False
        enriched["cache_path"] = ""
        return enriched

    _ensure_image_cache_dir()
    entry_id = str(entry.get("id", "entry"))
    cache_path = IMAGE_CACHE_DIR / _safe_catalog_filename(image_url, entry_id)
    has_cache_file = cache_path.exists()
    has_history_file = bool(available_urls and image_url in available_urls)
    
    # Also check managed images for any record with this URL that has local_available=True
    has_managed_file = False
    if not has_cache_file and not has_history_file:
        for record in _list_image_records(limit=500):
            if record.get("source_value") == image_url and record.get("local_available"):
                has_managed_file = True
                break
    
    enriched["local_available"] = has_cache_file or has_history_file or has_managed_file
    enriched["cache_path"] = str(cache_path)
    return enriched


def _available_image_urls_from_history(limit: int = 5000) -> set[str]:
    available: set[str] = set()
    for record in _list_image_records(limit=limit):
        if not bool(record.get("local_available", False)):
            continue
        source_value = str(record.get("source_value", "")).strip()
        if source_value.startswith("http://") or source_value.startswith("https://"):
            available.add(source_value)
    return available


def _run_catalog_pull_job(job_id: str, entries: List[Dict[str, Any]], skip_existing: bool) -> None:
    _ensure_image_cache_dir()
    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = datetime.now(timezone.utc).isoformat()

    for entry in entries:
        entry_id = str(entry.get("id", ""))
        label = str(entry.get("label", entry_id))
        image_url = str(entry.get("image_url", "")).strip()
        expected_sha = str(entry.get("image_sha256", "")).strip().lower()
        profile_value = ",".join(entry.get("profiles", [])) if isinstance(entry.get("profiles"), list) else ""

        item_result: Dict[str, Any] = {
            "id": entry_id,
            "label": label,
            "url": image_url,
            "status": "pending",
            "download_path": "",
            "observed_sha256": "",
            "expected_sha256": expected_sha,
            "message": "",
        }

        filename = _safe_catalog_filename(image_url, entry_id)
        target = IMAGE_CACHE_DIR / filename

        try:
            if target.exists() and skip_existing:
                item_result["status"] = "skipped"
                item_result["download_path"] = str(target)
                item_result["message"] = "already exists"
                with CATALOG_PULL_JOBS_LOCK:
                    job = CATALOG_PULL_JOBS.get(job_id)
                    if job:
                        job.setdefault("results", []).append(item_result)
                continue

            tmp = target.with_suffix(target.suffix + ".part")
            observed_sha = _download_catalog_image(image_url, tmp)
            tmp.replace(target)

            hash_status = "verified" if expected_sha and observed_sha == expected_sha else "downloaded"
            if expected_sha and observed_sha != expected_sha:
                hash_status = "failed"

            item_result["status"] = "ok" if hash_status != "failed" else "failed"
            item_result["download_path"] = str(target)
            item_result["observed_sha256"] = observed_sha
            item_result["message"] = hash_status

            _append_image_history(
                {
                    "id": uuid.uuid4().hex[:12],
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "profile": profile_value,
                    "hostname": "catalog-pull",
                    "source": "catalog_pull",
                    "source_value": image_url,
                    "downloaded": True,
                    "download_path": str(target),
                    "hash_status": hash_status,
                    "expected_sha256": expected_sha,
                    "observed_sha256": observed_sha,
                    "job_status": "completed" if hash_status != "failed" else "failed",
                    "exit_code": 0 if hash_status != "failed" else 1,
                    "label": label,
                    "notes": "Manual catalog pull",
                }
            )
        except Exception as exc:
            item_result["status"] = "failed"
            item_result["message"] = str(exc)
        finally:
            with CATALOG_PULL_JOBS_LOCK:
                job = CATALOG_PULL_JOBS.get(job_id)
                if job:
                    job.setdefault("results", []).append(item_result)

    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            return
        results = job.get("results", [])
        failed = sum(1 for r in results if r.get("status") == "failed")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        ok = sum(1 for r in results if r.get("status") == "ok")
        job["summary"] = {"ok": ok, "failed": failed, "skipped": skipped, "total": len(results)}
        job["status"] = "completed_with_errors" if failed else "completed"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()


def _ensure_prereqs() -> List[str]:
    errors: List[str] = []
    if not SCRIPT_PATH.exists():
        errors.append(f"Missing script: {SCRIPT_PATH}")
    if not INVENTORY_FILE.exists():
        errors.append(f"Missing inventory file: {INVENTORY_FILE}")
    return errors


def _default_node_config() -> Dict[str, Any]:
    return {
        "username": "",
        "password": "",
        "webhook_url": "",
        "verify_tls": True,
    }


def _default_node_provisioning() -> Dict[str, Any]:
    return {"node_configs": {}, "results": {}, "node_inputs": {}}


def _default_monitor_state() -> Dict[str, Any]:
    return {
        "managed_nodes": {},
        "cloud_init_options": {},
        "node_cloud_init": {},
    }


def _load_monitor_state() -> Dict[str, Any]:
    _ensure_data_file()
    default = _default_monitor_state()
    if not MONITOR_STATE_FILE.exists():
        return default

    try:
        loaded = json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default

    if not isinstance(loaded, dict):
        return default

    managed_nodes = loaded.get("managed_nodes", {})
    cloud_init_options = loaded.get("cloud_init_options", {})
    node_cloud_init = loaded.get("node_cloud_init", {})
    if not isinstance(managed_nodes, dict):
        managed_nodes = {}
    if not isinstance(cloud_init_options, dict):
        cloud_init_options = {}
    if not isinstance(node_cloud_init, dict):
        node_cloud_init = {}

    return {
        "managed_nodes": managed_nodes,
        "cloud_init_options": cloud_init_options,
        "node_cloud_init": node_cloud_init,
    }


def _save_monitor_state(state: Dict[str, Any]) -> None:
    _ensure_data_file()
    MONITOR_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _sanitize_managed_node(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_name = str(payload.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", raw_name):
        raise ValueError("node name must use only letters, numbers, dot, dash, or underscore")

    raw_group = str(payload.get("group", "")).strip()
    group = raw_group or "custom"
    address = str(payload.get("address", "")).strip() or raw_name
    notes = str(payload.get("notes", "")).strip()
    return {
        "name": raw_name,
        "group": group,
        "address": address,
        "notes": notes,
        "enabled": bool(payload.get("enabled", True)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _sanitize_cloud_init_option(payload: Dict[str, Any]) -> Dict[str, Any]:
    option_id = str(payload.get("id", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]+", option_id):
        raise ValueError("option id must use only lowercase letters, numbers, dash, or underscore")

    label = str(payload.get("label", "")).strip() or option_id
    description = str(payload.get("description", "")).strip()
    raw_fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    allowed_types = {"text", "number", "bool", "select", "secret"}
    fields: List[Dict[str, Any]] = []

    for raw in raw_fields:
        if not isinstance(raw, dict):
            continue
        field_id = str(raw.get("id", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", field_id):
            continue
        field_type = str(raw.get("type", "text")).strip().lower()
        if field_type not in allowed_types:
            field_type = "text"
        field = {
            "id": field_id,
            "label": str(raw.get("label", "")).strip() or field_id,
            "type": field_type,
            "placeholder": str(raw.get("placeholder", "")).strip(),
            "default": str(raw.get("default", "")).strip(),
            "required": bool(raw.get("required", False)),
        }
        fields.append(field)

    return {
        "id": option_id,
        "label": label,
        "description": description,
        "fields": fields,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _node_aliases(name: str) -> set[str]:
    raw = str(name or "").strip().lower()
    if not raw:
        return set()
    aliases = {raw}
    if "." in raw:
        aliases.add(raw.split(".", 1)[0])
    return aliases


def _latest_node_cert(node_name: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    aliases = _node_aliases(node_name)
    for event in events:
        host_aliases = _node_aliases(str(event.get("hostname", "")))
        fqdn_aliases = _node_aliases(str(event.get("fqdn", "")))
        if not aliases.intersection(host_aliases.union(fqdn_aliases)):
            continue
        return {
            "received_at": str(event.get("received_at", "")),
            "cert_present": bool(event.get("cert_present", False)),
            "cert_subject": str(event.get("cert_subject", "")),
            "cert_issuer": str(event.get("cert_issuer", "")),
            "cert_fingerprint": str(event.get("cert_fingerprint", "")),
            "source_ip": str(event.get("source_ip", "")),
        }

    return {
        "received_at": "",
        "cert_present": False,
        "cert_subject": "",
        "cert_issuer": "",
        "cert_fingerprint": "",
        "source_ip": "",
    }


def _monitor_nodes_summary() -> Dict[str, Any]:
    state = _load_monitor_state()
    provision = _load_node_provisioning()
    inventory_nodes = _inventory_nodes()
    managed_nodes = state.get("managed_nodes", {}) if isinstance(state.get("managed_nodes", {}), dict) else {}
    node_cloud_init = state.get("node_cloud_init", {}) if isinstance(state.get("node_cloud_init", {}), dict) else {}
    events = _read_events(limit=2000)
    callbacks = _last_callback_by_host()

    combined: Dict[str, Dict[str, Any]] = {}
    for node in inventory_nodes:
        node_name = str(node.get("name", "")).strip()
        if not node_name:
            continue
        combined[node_name] = {
            "name": node_name,
            "group": str(node.get("group", "")).strip() or "custom",
            "address": str(node.get("address", "")).strip() or node_name,
            "notes": "",
            "enabled": True,
            "source": "inventory",
        }

    for node_name, raw in managed_nodes.items():
        if not isinstance(raw, dict):
            continue
        name = str(node_name).strip()
        if not name:
            continue
        managed = {
            "name": name,
            "group": str(raw.get("group", "custom")).strip() or "custom",
            "address": str(raw.get("address", name)).strip() or name,
            "notes": str(raw.get("notes", "")).strip(),
            "enabled": bool(raw.get("enabled", True)),
            "source": "managed",
        }
        if name in combined:
            combined[name].update({
                "group": managed["group"],
                "address": managed["address"],
                "notes": managed["notes"],
                "enabled": managed["enabled"],
                "source": "inventory+managed",
            })
        else:
            combined[name] = managed

    nodes: List[Dict[str, Any]] = []
    for node in sorted(combined.values(), key=lambda n: str(n.get("name", "")).lower()):
        node_name = str(node.get("name", ""))
        cert = _latest_node_cert(node_name, events)
        cloud_ids_raw = node_cloud_init.get(node_name, []) if isinstance(node_cloud_init, dict) else []
        cloud_ids = [str(v) for v in cloud_ids_raw] if isinstance(cloud_ids_raw, list) else []
        enriched = dict(node)
        enriched["last_callback"] = callbacks.get(node_name, "")
        enriched["cert"] = cert
        enriched["cloud_init_option_ids"] = cloud_ids
        enriched["selected_profiles"] = _effective_node_profiles(enriched, provision)
        nodes.append(enriched)

    raw_options = state.get("cloud_init_options", {}) if isinstance(state.get("cloud_init_options", {}), dict) else {}
    cloud_init_options = [v for v in raw_options.values() if isinstance(v, dict)]
    cloud_init_options.sort(key=lambda item: str(item.get("label", "")).lower())

    return {
        "nodes": nodes,
        "cloud_init_options": cloud_init_options,
        "node_cloud_init": node_cloud_init,
    }


def _load_node_config() -> Dict[str, Any]:
    _ensure_data_file()
    default = _default_node_config()
    if not NODE_CONFIG_FILE.exists():
        return default
    try:
        loaded = json.loads(NODE_CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
    default.update({k: loaded.get(k, default[k]) for k in default})
    return default


def _save_node_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = _load_node_config()
    for key in ("username", "password", "webhook_url", "verify_tls"):
        if key in payload:
            config[key] = payload[key]
    _ensure_data_file()
    NODE_CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def _default_profiles_for_group(group: str) -> List[str]:
    defaults = ["baseline", "tailscale", "telegraf"]
    if group == "rpi5_active":
        defaults.append("docker")
    return defaults


def _load_node_provisioning() -> Dict[str, Any]:
    _ensure_data_file()
    default = _default_node_provisioning()
    if not NODE_PROVISION_FILE.exists():
        return default
    try:
        loaded = json.loads(NODE_PROVISION_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default

    if not isinstance(loaded, dict):
        return default
    node_configs = loaded.get("node_configs", {})
    results = loaded.get("results", {})
    node_inputs = loaded.get("node_inputs", {})
    if not isinstance(node_configs, dict):
        node_configs = {}
    if not isinstance(results, dict):
        results = {}
    if not isinstance(node_inputs, dict):
        node_inputs = {}
    return {"node_configs": node_configs, "results": results, "node_inputs": node_inputs}


def _save_node_provisioning(state: Dict[str, Any]) -> None:
    _ensure_data_file()
    NODE_PROVISION_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _reset_node_ops_settings() -> Dict[str, Any]:
    _ensure_data_file()
    node_cfg = _default_node_config()
    node_prov = _default_node_provisioning()
    NODE_CONFIG_FILE.write_text(json.dumps(node_cfg, indent=2), encoding="utf-8")
    NODE_PROVISION_FILE.write_text(json.dumps(node_prov, indent=2), encoding="utf-8")
    return {
        "node_config": True,
        "node_provisioning": True,
    }


def _profile_map() -> Dict[str, Dict[str, Any]]:
    return {p["id"]: p for p in NODE_PROVISION_PROFILES}


def _profile_input_specs(profile_id: str) -> List[Dict[str, Any]]:
    profile = _profile_map().get(profile_id, {})
    specs = profile.get("input_specs")
    if isinstance(specs, list):
        return [s for s in specs if isinstance(s, dict)]
    return []


def _sanitize_node_profile_inputs(payload: Dict[str, Any], profile_ids: List[str]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for profile_id in profile_ids:
        raw_inputs = payload.get(profile_id, {})
        if not isinstance(raw_inputs, dict):
            continue
        specs = _profile_input_specs(profile_id)
        allowed = {str(spec.get("id", "")) for spec in specs}
        cleaned: Dict[str, str] = {}
        for key, value in raw_inputs.items():
            k = str(key)
            if k not in allowed:
                continue
            cleaned[k] = str(value).strip()
        out[profile_id] = cleaned
    return out


def _node_profile_inputs(node_name: str, state: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    node_inputs = state.get("node_inputs", {})
    raw = node_inputs.get(node_name, {}) if isinstance(node_inputs, dict) else {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for profile_id, values in raw.items():
        if isinstance(values, dict):
            out[str(profile_id)] = {str(k): str(v) for k, v in values.items()}
    return out


def _missing_required_inputs(node_name: str, profile_ids: List[str], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    saved_inputs = _node_profile_inputs(node_name, state)
    profile_by_id = _profile_map()
    missing: List[Dict[str, Any]] = []

    for profile_id in profile_ids:
        specs = _profile_input_specs(profile_id)
        required_specs = [s for s in specs if bool(s.get("required", False))]
        if not required_specs:
            continue

        profile_inputs = saved_inputs.get(profile_id, {})
        missing_fields = []
        for spec in required_specs:
            field_id = str(spec.get("id", ""))
            value = str(profile_inputs.get(field_id, "")).strip()
            if not value:
                missing_fields.append(
                    {
                        "id": field_id,
                        "label": str(spec.get("label", field_id)),
                        "placeholder": str(spec.get("placeholder", "")),
                        "sensitive": bool(spec.get("sensitive", False)),
                    }
                )

        if missing_fields:
            missing.append(
                {
                    "profile_id": profile_id,
                    "profile_label": str(profile_by_id.get(profile_id, {}).get("label", profile_id)),
                    "fields": missing_fields,
                }
            )
    return missing


def _redact_profile_inputs(profile_id: str, inputs: Dict[str, str]) -> Dict[str, str]:
    specs = {str(s.get("id", "")): s for s in _profile_input_specs(profile_id)}
    out: Dict[str, str] = {}
    for key, value in inputs.items():
        spec = specs.get(key, {})
        if bool(spec.get("sensitive", False)):
            out[key] = "***"
        else:
            out[key] = value
    return out


def _effective_node_profiles(node: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    node_configs = state.get("node_configs", {})
    selected = node_configs.get(node["name"])
    if isinstance(selected, list) and selected:
        return [str(v) for v in selected]
    return _default_profiles_for_group(node.get("group", ""))


def _latest_node_check(node_name: str, state: Dict[str, Any]) -> Dict[str, Any]:
    node_results = state.get("results", {}).get(node_name, {})
    if not isinstance(node_results, dict):
        return {"status": "not-run", "checked_at": "", "mode": ""}

    latest: Dict[str, Any] = {"status": "not-run", "checked_at": "", "mode": ""}
    for result in node_results.values():
        if not isinstance(result, dict):
            continue
        checked_at = str(result.get("checked_at", ""))
        if checked_at and checked_at >= str(latest.get("checked_at", "")):
            latest = {
                "status": str(result.get("status", "not-run")),
                "checked_at": checked_at,
                "mode": str(result.get("mode", "")),
            }
    return latest


def _run_node_profile(node_name: str, profile: Dict[str, Any], mode: str, run_ansible: bool, profile_inputs: Dict[str, str]) -> Dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    profile_id = str(profile.get("id", ""))
    tags = profile.get("tags") or []

    if not run_ansible:
        return {
            "profile_id": profile_id,
            "status": "planned",
            "mode": mode,
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": "Ansible execution disabled for this run (plan-only).",
        }

    cmd = [
        "ansible-playbook",
        "-i",
        str(INVENTORY_FILE),
        "site.yml",
        "--limit",
        node_name,
    ]
    if tags:
        cmd.extend(["--tags", ",".join(str(t) for t in tags)])
    if mode == "check":
        cmd.append("--check")

    extra_vars_payload = {
        "provision_profile": profile_id,
        "provision_inputs": profile_inputs,
        "node_name": node_name,
    }
    cmd.extend(["--extra-vars", json.dumps(extra_vars_payload)])
    cmd_for_log = cmd[:-1] + ["<redacted-provision-inputs>"]

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=420,
            cwd=str(BASE_DIR.parent),
        )
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return {
            "profile_id": profile_id,
            "status": "ok" if proc.returncode == 0 else "failed",
            "mode": mode,
            "checked_at": checked_at,
            "command": cmd_for_log,
            "exit_code": proc.returncode,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": output[-12000:],
        }
    except FileNotFoundError:
        return {
            "profile_id": profile_id,
            "status": "unavailable",
            "mode": mode,
            "checked_at": checked_at,
            "command": cmd_for_log,
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": "ansible-playbook command not found on host.",
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return {
            "profile_id": profile_id,
            "status": "timeout",
            "mode": mode,
            "checked_at": checked_at,
            "command": cmd_for_log,
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": ((stdout + "\n" + stderr).strip() or "Provisioning run timed out.")[-12000:],
        }


def _apply_node_provisioning(node_name: str, profile_ids: List[str], mode: str, run_ansible: bool) -> Dict[str, Any]:
    profile_by_id = _profile_map()
    state = _load_node_provisioning()
    valid_profile_ids = [pid for pid in profile_ids if pid in profile_by_id]
    inputs_by_profile = _node_profile_inputs(node_name, state)
    node_results: Dict[str, Any] = {}
    for pid in valid_profile_ids:
        profile_inputs = inputs_by_profile.get(pid, {})
        result = _run_node_profile(node_name, profile_by_id[pid], mode=mode, run_ansible=run_ansible, profile_inputs=profile_inputs)
        node_results[pid] = result

    state_results = state.setdefault("results", {})
    existing = state_results.get(node_name)
    if not isinstance(existing, dict):
        existing = {}
    existing.update(node_results)
    state_results[node_name] = existing
    _save_node_provisioning(state)

    return {
        "node": node_name,
        "mode": mode,
        "run_ansible": run_ansible,
        "results": [node_results[pid] for pid in valid_profile_ids],
    }


def _inventory_nodes() -> List[Dict[str, Any]]:
    try:
        result = subprocess.run(
            ["ansible-inventory", "-i", str(INVENTORY_FILE), "--list"],
            check=True,
            capture_output=True,
            text=True,
        )
        inv = json.loads(result.stdout)
    except Exception:
        return _inventory_nodes_from_yaml_fallback()

    hostvars = inv.get("_meta", {}).get("hostvars", {})
    groups = ["rpi5_active", "rpi_bplus_active"]
    nodes: List[Dict[str, Any]] = []
    for group in groups:
        for host in inv.get(group, {}).get("hosts", []):
            hv = hostvars.get(host, {})
            nodes.append(
                {
                    "name": host,
                    "group": group,
                    "address": hv.get("ansible_host", host),
                }
            )
    return nodes


def _inventory_nodes_from_yaml_fallback() -> List[Dict[str, Any]]:
    if not INVENTORY_FILE.exists():
        return []

    text = INVENTORY_FILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    target_groups = {"rpi5_active", "rpi_bplus_active"}

    nodes: List[Dict[str, Any]] = []
    current_group = ""
    group_indent = -1
    in_hosts = False
    hosts_indent = -1
    current_host: Dict[str, Any] | None = None
    host_indent = -1

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].rstrip("\n")
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        match = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()

        if key in target_groups and value == "":
            current_group = key
            group_indent = indent
            in_hosts = False
            current_host = None
            continue

        if current_group and indent <= group_indent:
            current_group = ""
            in_hosts = False
            current_host = None

        if not current_group:
            continue

        if key == "hosts" and value == "" and indent > group_indent:
            in_hosts = True
            hosts_indent = indent
            current_host = None
            continue

        if in_hosts and indent <= hosts_indent:
            in_hosts = False
            current_host = None

        if not in_hosts:
            continue

        if value == "" and indent > hosts_indent:
            current_host = {
                "name": key,
                "group": current_group,
                "address": key,
            }
            host_indent = indent
            nodes.append(current_host)
            continue

        if current_host and indent > host_indent and key == "ansible_host":
            current_host["address"] = value.strip('"\'')

    return nodes


def _ping_status(address: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "1", address],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return {"reachable": False, "latency_ms": None, "error": "ping command not found"}

    reachable = proc.returncode == 0
    latency = None
    if reachable:
        match = re.search(r"time=([0-9.]+)\s*ms", proc.stdout or "")
        if match:
            latency = float(match.group(1))
    return {"reachable": reachable, "latency_ms": latency, "error": "" if reachable else (proc.stderr or proc.stdout).strip()}


def _last_callback_by_host() -> Dict[str, str]:
    seen: Dict[str, str] = {}
    for event in _read_events(limit=1000):
        host = event.get("hostname")
        ts = event.get("received_at")
        if host and ts and host not in seen:
            seen[host] = ts
    return seen


def _send_webhook(url: str, payload: Dict[str, Any], verify_tls: bool = True) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    context = None
    if not verify_tls and url.startswith("https://"):
        import ssl

        context = ssl._create_unverified_context()
    try:
        with urlrequest.urlopen(req, timeout=10, context=context) as resp:  # nosec B310
            return {"ok": True, "status": resp.status}
    except (urlerror.URLError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc)}


def _is_safe_key_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name))


def _generate_or_load_ssh_key(key_name: str, comment: str = "rack-bootstrap", mode: str = "kload") -> Dict[str, Any]:
    _ensure_keys_dir()
    private_key = KEYS_DIR / key_name
    public_key = KEYS_DIR / f"{key_name}.pub"
    mode = str(mode or "kload").strip().lower()
    if mode not in {"generate", "kload"}:
        raise ValueError("mode must be generate or kload")

    created = False
    private_exists = private_key.exists()
    public_exists = public_key.exists()

    if mode == "generate" and (private_exists or public_exists):
        raise FileExistsError("key already exists; use mode 'kload' to load existing key")

    if mode == "generate" or not private_exists or not public_exists:
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-a",
                "64",
                "-N",
                "",
                "-C",
                comment,
                "-f",
                str(private_key),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        created = True

    pub = public_key.read_text(encoding="utf-8").strip()
    return {
        "created": created,
        "mode": mode,
        "key_name": key_name,
        "public_key": pub,
        "private_key_path": str(private_key),
        "public_key_path": str(public_key),
    }


def _list_sd_devices() -> List[Dict[str, Any]]:
    try:
        result = subprocess.run(
            [
                "lsblk",
                "-J",
                "-o",
                "NAME,PATH,SIZE,MODEL,RM,TYPE,TRAN,RO,HOTPLUG,MOUNTPOINTS",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
    except Exception:
        return []

    devices: List[Dict[str, Any]] = []
    for d in payload.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        if int(d.get("ro") or 0) != 0:
            continue
        rm = int(d.get("rm") or 0)
        hotplug = int(d.get("hotplug") or 0)
        tran = (d.get("tran") or "").lower()
        if rm != 1 and hotplug != 1 and tran not in {"usb", "mmc"}:
            continue
        devices.append(
            {
                "name": d.get("name"),
                "path": d.get("path"),
                "size": d.get("size"),
                "model": d.get("model") or "",
                "transport": d.get("tran") or "",
                "mountpoints": d.get("mountpoints") or [],
            }
        )
    return devices


def _device_signature(devices: List[Dict[str, Any]]) -> str:
    reduced = [
        {
            "path": d.get("path", ""),
            "size": d.get("size", ""),
            "model": d.get("model", ""),
            "transport": d.get("transport", ""),
            "mountpoints": d.get("mountpoints", []),
        }
        for d in devices
    ]
    reduced.sort(key=lambda item: item.get("path", ""))
    return json.dumps(reduced, sort_keys=True, separators=(",", ":"))


def _device_event_message(devices: List[Dict[str, Any]]) -> str:
    payload = {
        "devices": devices,
        "count": len(devices),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return f"event: devices\\ndata: {json.dumps(payload, separators=(',', ':'))}\\n\\n"


def _broadcast_device_update(devices: List[Dict[str, Any]]) -> None:
    message = _device_event_message(devices)
    with DEVICE_SUBSCRIBERS_LOCK:
        subscribers = list(DEVICE_SUBSCRIBERS)
    for sub in subscribers:
        try:
            while sub.full():
                sub.get_nowait()
            sub.put_nowait(message)
        except queue.Empty:
            continue
        except queue.Full:
            continue


def _device_monitor_loop() -> None:
    last_sig = ""
    while True:
        devices = _list_sd_devices()
        sig = _device_signature(devices)
        if sig != last_sig:
            last_sig = sig
            _broadcast_device_update(devices)
        time.sleep(2)


def _ensure_device_monitor_started() -> None:
    global DEVICE_MONITOR_STARTED
    with DEVICE_MONITOR_LOCK:
        if DEVICE_MONITOR_STARTED:
            return
        thread = threading.Thread(target=_device_monitor_loop, daemon=True)
        thread.start()
        DEVICE_MONITOR_STARTED = True


def _detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def _default_callback_url() -> str:
    host = os.getenv("CALLBACK_PUBLIC_HOST", "").strip()
    if not host:
        host = _detect_local_ip()

    port = os.getenv("PORT", "8088").strip() or "8088"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/api/cloud-init/callback"


def _first_forwarded_ip(value: str) -> str:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts[0] if parts else ""


def _resolve_source_ip(payload: Dict[str, Any]) -> tuple[str, str]:
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        ip = _first_forwarded_ip(forwarded_for)
        if ip:
            return ip, "x-forwarded-for"

    for header in ("X-Real-IP", "CF-Connecting-IP", "True-Client-IP"):
        value = request.headers.get(header, "").strip()
        if value:
            return value, header.lower()

    for key in ("source_ip", "client_ip", "ip", "remote_addr"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value, f"payload:{key}"

    return request.remote_addr or "", "remote_addr"


def _header_or_payload(payload: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        header_val = request.headers.get(key)
        if header_val:
            return str(header_val).strip()
        payload_val = payload.get(key)
        if payload_val:
            return str(payload_val).strip()
    return ""


def _strip_dataset_fields(payload: Any) -> Any:
    # Drop bulky/sensitive dataset blobs from inbound payloads before persistence.
    if isinstance(payload, dict):
        cleaned: Dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).strip().lower() == "dataset":
                continue
            cleaned[str(key)] = _strip_dataset_fields(value)
        return cleaned
    if isinstance(payload, list):
        return [_strip_dataset_fields(item) for item in payload]
    return payload


def _sanitize_sd_prepare_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = {
        "profile",
        "hostname",
        "device",
        "ssh_public_key",
        "callback_url",
        "image_url",
        "image_file",
        "image_sha256",
        "workspace",
    }
    cleaned: Dict[str, Any] = {}
    for key in allowed_keys:
        if key in payload:
            cleaned[key] = payload.get(key)
    return cleaned


def _run_prepare_job(job_id: str, form: Dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="sdprep-key-") as tdir:
        pubkey_file = Path(tdir) / "id.pub"
        ssh_public_key = form["ssh_public_key"].strip()
        pubkey_file.write_text(ssh_public_key + "\n", encoding="utf-8")
        archive_info = _archive_ssh_public_key(job_id, form, ssh_public_key)

        cmd = [
            str(SCRIPT_PATH),
            "--profile",
            form["profile"],
            "--hostname",
            form["hostname"],
            "--device",
            form["device"],
            "--ssh-key-file",
            str(pubkey_file),
            "--callback-url",
            form["callback_url"],
            "--yes",
        ]

        image_url = form.get("image_url", "").strip()
        image_file = form.get("image_file", "").strip()
        image_sha256 = form.get("image_sha256", "").strip()
        workspace = form.get("workspace", "").strip()
        if image_url:
            cmd.extend(["--image-url", image_url])
        if image_file:
            cmd.extend(["--image-file", image_file])
        if image_sha256:
            cmd.extend(["--image-sha256", image_sha256])
        if workspace:
            cmd.extend(["--workspace", workspace])

        run_cmd: List[str]
        if os.geteuid() == 0:
            run_cmd = cmd
        else:
            suggested = " ".join(["sudo"] + cmd)
            with JOBS_LOCK:
                JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
                JOBS[job_id]["stdout"] = ""
                JOBS[job_id]["stderr"] = (
                    "SD preparation requires root privileges. "
                    "Run the callback site as root (e.g. sudo PORT=9821 .venv/bin/python3 app.py) "
                    f"or run this command manually: {suggested}"
                )
                JOBS[job_id]["exit_code"] = 1
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["command"] = cmd
            return

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["command"] = run_cmd
            JOBS[job_id]["stdout"] = (
                "Starting SD preparation.\n"
                f"Target device: {form.get('device', '')}\n"
                f"Archived SSH key: {archive_info.get('archive_path', '')}\n"
                "The selected device will be overwritten.\n"
            )

        proc = subprocess.Popen(
            run_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        output_chunks: List[str] = []
        if proc.stdout is not None:
            for line in proc.stdout:
                output_chunks.append(line)
                combined = "".join(output_chunks)[-50000:]
                with JOBS_LOCK:
                    JOBS[job_id]["stdout"] = combined

        proc.wait()
        final_stdout = "".join(output_chunks)[-50000:]

        with JOBS_LOCK:
            JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["stdout"] = final_stdout
            JOBS[job_id]["stderr"] = ""
            JOBS[job_id]["exit_code"] = proc.returncode
            JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"

        _record_image_history(form, final_stdout, proc.returncode)


@app.get("/")
def index() -> str:
    _ensure_device_monitor_started()
    events = _read_events(limit=200)
    return render_template(
        "index.html",
        events=events,
        image_history=_read_image_history(limit=150),
        image_summary=_image_state_summary(),
        devices=_list_sd_devices(),
        callback_url_default=_default_callback_url(),
        image_catalog=IMAGE_CATALOG,
    )


@app.get("/tailscale-acl")
def tailscale_acl() -> str:
    return render_template("tailscale_acl.html")


@app.get("/fragments/tailscale-acl/overview")
def tailscale_acl_overview_fragment() -> str:
    return render_template("fragments/tailscale_acl_overview.html")


@app.get("/fragments/tailscale-acl/inputs")
def tailscale_acl_inputs_fragment() -> str:
    return render_template("fragments/tailscale_acl_inputs.html", defaults=_tailscale_policy_defaults())


@app.get("/fragments/tailscale-acl/output")
def tailscale_acl_output_fragment() -> str:
    return render_template("fragments/tailscale_acl_output.html")


@app.get("/fragments/tailscale-acl/apply-guide")
def tailscale_acl_apply_guide_fragment() -> str:
    return render_template("fragments/tailscale_acl_apply_guide.html")


@app.get("/nodes")
def nodes_page() -> str:
    return render_template("nodes.html")


@app.get("/monitor")
def monitor_page() -> str:
    return render_template("monitor.html")


@app.get("/images")
def images_page() -> str:
    image_history = _read_image_history(limit=500)
    return render_template("images.html", image_catalog=IMAGE_CATALOG, image_history=image_history, image_records=image_history)


@app.get("/settings")
def settings_page() -> str:
    return render_template("settings.html")


@app.get("/api/settings")
def settings_get() -> Any:
    return jsonify({"ok": True, "settings": _load_app_settings()})


@app.post("/api/settings")
def settings_update() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    settings = _save_app_settings(payload)
    return jsonify({"ok": True, "settings": settings})


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True})


@app.get("/api/cloud-init/events")
def events() -> Any:
    return jsonify({"events": _read_events(limit=500)})


@app.post("/api/cloud-init/events/clear")
def clear_events() -> Any:
    cleared = _clear_events()
    return jsonify({"ok": True, "cleared": cleared})


@app.get("/fragments/cloud-init/event-rows")
def cloud_init_event_rows() -> str:
    return render_template("fragments/callback_rows.html", events=_read_events(limit=200))


@app.post("/api/cloud-init/callback")
def callback() -> Any:
    payload_json = request.get_json(silent=True)
    payload_form = request.form.to_dict(flat=True)
    payload: Dict[str, Any] = payload_json if isinstance(payload_json, dict) and payload_json else payload_form
    payload = _strip_dataset_fields(payload)

    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    source_ip, source_ip_source = _resolve_source_ip(payload)
    cert_subject = _header_or_payload(
        payload,
        "X-SSL-Client-Subject",
        "X-Client-Cert-Subject",
        "TLS-Client-Subject",
        "cert_subject",
    )
    cert_issuer = _header_or_payload(
        payload,
        "X-SSL-Client-Issuer",
        "X-Client-Cert-Issuer",
        "TLS-Client-Issuer",
        "cert_issuer",
    )
    cert_fingerprint = _header_or_payload(
        payload,
        "X-SSL-Client-Fingerprint",
        "X-Client-Cert-Fingerprint",
        "TLS-Client-Fingerprint",
        "cert_fingerprint",
    )

    event = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source_ip": source_ip,
        "source_ip_source": source_ip_source,
        "forwarded_for": forwarded_for,
        "remote_addr": request.remote_addr,
        "user_agent": request.headers.get("User-Agent", ""),
        "instance_id": payload.get("instance_id") or request.form.get("instance_id"),
        "hostname": payload.get("hostname") or request.form.get("hostname"),
        "fqdn": payload.get("fqdn") or request.form.get("fqdn"),
        "profile": payload.get("profile"),
        "stage": payload.get("stage"),
        "cert_subject": cert_subject,
        "cert_issuer": cert_issuer,
        "cert_fingerprint": cert_fingerprint,
        "cert_present": bool(cert_subject or cert_fingerprint),
        "raw": payload,
    }
    _append_event(event)
    return jsonify({"ok": True})


@app.get("/api/sd/devices")
def sd_devices() -> Any:
    _ensure_device_monitor_started()
    prereq_errors = _ensure_prereqs()
    return jsonify({"devices": _list_sd_devices(), "errors": prereq_errors})


@app.get("/api/sd/devices/webhook")
def sd_device_webhook() -> Response:
    _ensure_device_monitor_started()

    def event_stream() -> Any:
        sub: queue.Queue[str] = queue.Queue(maxsize=4)
        with DEVICE_SUBSCRIBERS_LOCK:
            DEVICE_SUBSCRIBERS.append(sub)

        try:
            initial_devices = _list_sd_devices()
            yield _device_event_message(initial_devices)
            while True:
                try:
                    yield sub.get(timeout=20)
                except queue.Empty:
                    yield "event: ping\\ndata: {}\\n\\n"
        finally:
            with DEVICE_SUBSCRIBERS_LOCK:
                if sub in DEVICE_SUBSCRIBERS:
                    DEVICE_SUBSCRIBERS.remove(sub)

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/sd/prepare")
def sd_prepare() -> Any:
    prereq_errors = _ensure_prereqs()
    if prereq_errors:
        return jsonify({"ok": False, "errors": prereq_errors}), 400

    payload_raw = request.get_json(silent=True) or {}
    payload = _sanitize_sd_prepare_payload(payload_raw if isinstance(payload_raw, dict) else {})
    required = ["profile", "hostname", "device", "ssh_public_key", "callback_url"]
    missing = [k for k in required if not str(payload.get(k, "")).strip()]
    if missing:
        return jsonify({"ok": False, "errors": [f"Missing fields: {', '.join(missing)}"]}), 400

    if payload["profile"] not in {"pi5", "pibplus"}:
        return jsonify({"ok": False, "errors": ["profile must be pi5 or pibplus"]}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "request": {
                "profile": payload["profile"],
                "hostname": payload["hostname"],
                "device": payload["device"],
                "callback_url": payload["callback_url"],
                "image_sha256": str(payload.get("image_sha256", "")).strip(),
            },
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    thread = threading.Thread(target=_run_prepare_job, args=(job_id, payload), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.post("/api/ssh/generate")
def ssh_generate() -> Any:
    payload = request.get_json(silent=True) or {}
    key_name = str(payload.get("key_name", "rack-bootstrap-ed25519")).strip()
    comment = str(payload.get("comment", "rack-bootstrap")).strip()
    mode = str(payload.get("mode", "kload")).strip().lower()

    if not key_name:
        return jsonify({"ok": False, "errors": ["key_name is required"]}), 400
    if not _is_safe_key_name(key_name):
        return jsonify({"ok": False, "errors": ["key_name may contain only letters, numbers, dot, dash, and underscore"]}), 400
    if mode not in {"generate", "kload"}:
        return jsonify({"ok": False, "errors": ["mode must be generate or kload"]}), 400

    try:
        result = _generate_or_load_ssh_key(key_name, comment=comment, mode=mode)
    except FileNotFoundError:
        return jsonify({"ok": False, "errors": ["ssh-keygen command not found on host"]}), 500
    except FileExistsError as exc:
        return jsonify({"ok": False, "errors": [str(exc)]}), 409
    except ValueError as exc:
        return jsonify({"ok": False, "errors": [str(exc)]}), 400
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return jsonify({"ok": False, "errors": [f"ssh-keygen failed: {stderr}"]}), 500

    return jsonify({"ok": True, **result})


@app.get("/api/ssh/download/<key_name>/<kind>")
def ssh_download(key_name: str, kind: str) -> Any:
    if not _is_safe_key_name(key_name):
        return jsonify({"ok": False, "error": "invalid key name"}), 400
    if kind not in {"private", "public"}:
        return jsonify({"ok": False, "error": "kind must be private or public"}), 400

    file_name = key_name if kind == "private" else f"{key_name}.pub"
    path = KEYS_DIR / file_name
    if not path.exists() or not path.is_file():
        return jsonify({"ok": False, "error": "key file not found"}), 404

    return send_file(path, as_attachment=True, download_name=file_name)


@app.get("/api/nodes/summary")
def nodes_summary() -> Any:
    config = _load_node_config()
    nodes = _inventory_nodes()
    provision = _load_node_provisioning()

    enriched_nodes: List[Dict[str, Any]] = []
    for node in nodes:
        enriched = dict(node)
        selected = _effective_node_profiles(node, provision)
        enriched["selected_profiles"] = selected
        enriched["latest_check"] = _latest_node_check(node["name"], provision)
        enriched_nodes.append(enriched)

    masked = {
        "username": config.get("username", ""),
        "password_set": bool(config.get("password")),
        "webhook_url": config.get("webhook_url", ""),
        "verify_tls": bool(config.get("verify_tls", True)),
    }
    return jsonify(
        {
            "ok": True,
            "nodes": enriched_nodes,
            "config": masked,
            "profiles": NODE_PROVISION_PROFILES,
            "node_inputs": provision.get("node_inputs", {}),
            "image_summary": _image_state_summary(),
        }
    )


@app.get("/api/monitor/summary")
def monitor_summary() -> Any:
    summary = _monitor_nodes_summary()
    return jsonify(
        {
            "ok": True,
            "nodes": summary["nodes"],
            "profiles": NODE_PROVISION_PROFILES,
            "cloud_init_options": summary["cloud_init_options"],
            "node_cloud_init": summary["node_cloud_init"],
        }
    )


@app.post("/api/monitor/nodes")
def monitor_node_upsert() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    try:
        node = _sanitize_managed_node(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    state = _load_monitor_state()
    managed_nodes = state.setdefault("managed_nodes", {})
    managed_nodes[node["name"]] = node
    _save_monitor_state(state)
    return jsonify({"ok": True, "node": node})


@app.delete("/api/monitor/nodes/<node_name>")
def monitor_node_delete(node_name: str) -> Any:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", node_name):
        return jsonify({"ok": False, "error": "invalid node name"}), 400

    state = _load_monitor_state()
    managed_nodes = state.setdefault("managed_nodes", {})
    if node_name not in managed_nodes:
        return jsonify({"ok": False, "error": "managed node not found"}), 404
    managed_nodes.pop(node_name, None)

    node_cloud_init = state.setdefault("node_cloud_init", {})
    node_cloud_init.pop(node_name, None)
    _save_monitor_state(state)
    return jsonify({"ok": True})


@app.get("/api/monitor/nodes/<node_name>/certificate")
def monitor_node_certificate(node_name: str) -> Any:
    cert = _latest_node_cert(node_name, _read_events(limit=2000))
    return jsonify({"ok": True, "node": node_name, "certificate": cert})


@app.post("/api/monitor/cloud-init/options")
def monitor_cloud_init_option_upsert() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    try:
        option = _sanitize_cloud_init_option(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    state = _load_monitor_state()
    cloud_options = state.setdefault("cloud_init_options", {})
    cloud_options[option["id"]] = option
    _save_monitor_state(state)
    return jsonify({"ok": True, "option": option})


@app.delete("/api/monitor/cloud-init/options/<option_id>")
def monitor_cloud_init_option_delete(option_id: str) -> Any:
    option_key = str(option_id).strip().lower()
    state = _load_monitor_state()
    cloud_options = state.setdefault("cloud_init_options", {})
    if option_key not in cloud_options:
        return jsonify({"ok": False, "error": "option not found"}), 404

    cloud_options.pop(option_key, None)
    node_cloud_init = state.setdefault("node_cloud_init", {})
    for node_name, selected in list(node_cloud_init.items()):
        if not isinstance(selected, list):
            continue
        node_cloud_init[node_name] = [v for v in selected if str(v) != option_key]

    _save_monitor_state(state)
    return jsonify({"ok": True})


@app.post("/api/monitor/nodes/<node_name>/cloud-init")
def monitor_node_cloud_init_assign(node_name: str) -> Any:
    payload = request.get_json(silent=True) or {}
    option_ids = payload.get("option_ids") or []
    if not isinstance(option_ids, list):
        return jsonify({"ok": False, "error": "option_ids must be an array"}), 400

    state = _load_monitor_state()
    cloud_options = state.setdefault("cloud_init_options", {})
    valid_ids = [str(v).strip().lower() for v in option_ids if str(v).strip().lower() in cloud_options]
    node_cloud_init = state.setdefault("node_cloud_init", {})
    node_cloud_init[node_name] = valid_ids
    _save_monitor_state(state)
    return jsonify({"ok": True, "node": node_name, "option_ids": valid_ids})


@app.post("/api/nodes/config")
def nodes_config() -> Any:
    payload = request.get_json(silent=True) or {}
    config = _save_node_config(
        {
            "username": str(payload.get("username", "")).strip(),
            "password": str(payload.get("password", "")),
            "webhook_url": str(payload.get("webhook_url", "")).strip(),
            "verify_tls": bool(payload.get("verify_tls", True)),
        }
    )
    return jsonify(
        {
            "ok": True,
            "config": {
                "username": config.get("username", ""),
                "password_set": bool(config.get("password")),
                "webhook_url": config.get("webhook_url", ""),
                "verify_tls": bool(config.get("verify_tls", True)),
            },
        }
    )


@app.post("/api/nodes/monitor")
def nodes_monitor() -> Any:
    payload = request.get_json(silent=True) or {}
    emit_webhook = bool(payload.get("send_webhook", False))

    nodes = _inventory_nodes()
    by_host = _last_callback_by_host()
    statuses: List[Dict[str, Any]] = []
    for node in nodes:
        status = _ping_status(node["address"])
        statuses.append(
            {
                "name": node["name"],
                "group": node["group"],
                "address": node["address"],
                "reachable": status["reachable"],
                "latency_ms": status["latency_ms"],
                "last_callback": by_host.get(node["name"], ""),
                "error": status["error"],
            }
        )

    webhook_result: Dict[str, Any] = {"sent": False}
    config = _load_node_config()
    webhook_url = config.get("webhook_url", "")
    if emit_webhook and webhook_url:
        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reachable": sum(1 for s in statuses if s["reachable"]),
            "unreachable": sum(1 for s in statuses if not s["reachable"]),
            "nodes": statuses,
        }
        result = _send_webhook(webhook_url, summary, verify_tls=bool(config.get("verify_tls", True)))
        webhook_result = {"sent": True, **result}

    return jsonify({"ok": True, "statuses": statuses, "webhook": webhook_result})


@app.get("/api/nodes/profiles")
def nodes_profiles() -> Any:
    state = _load_node_provisioning()
    return jsonify(
        {
            "ok": True,
            "profiles": NODE_PROVISION_PROFILES,
            "node_configs": state.get("node_configs", {}),
            "node_inputs": state.get("node_inputs", {}),
        }
    )


@app.post("/api/nodes/node-configs")
def nodes_node_configs() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("node", "")).strip()
    profile_ids = payload.get("profile_ids") or []

    if not node_name:
        return jsonify({"ok": False, "error": "node is required"}), 400
    if not isinstance(profile_ids, list):
        return jsonify({"ok": False, "error": "profile_ids must be an array"}), 400

    known_nodes = {n["name"] for n in _inventory_nodes()}
    if node_name not in known_nodes:
        return jsonify({"ok": False, "error": "node not found in active inventory"}), 404

    valid_profile_ids = [str(pid) for pid in profile_ids if str(pid) in _profile_map()]
    state = _load_node_provisioning()
    node_configs = state.setdefault("node_configs", {})
    node_configs[node_name] = valid_profile_ids
    _save_node_provisioning(state)
    return jsonify({"ok": True, "node": node_name, "profile_ids": valid_profile_ids})


@app.post("/api/nodes/node-inputs")
def nodes_node_inputs() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("node", "")).strip()
    raw_inputs = payload.get("profile_inputs") or {}

    if not node_name:
        return jsonify({"ok": False, "error": "node is required"}), 400
    if not isinstance(raw_inputs, dict):
        return jsonify({"ok": False, "error": "profile_inputs must be an object"}), 400

    nodes = _inventory_nodes()
    node = next((n for n in nodes if n.get("name") == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "node not found in active inventory"}), 404

    state = _load_node_provisioning()
    profile_ids = _effective_node_profiles(node, state)
    sanitized = _sanitize_node_profile_inputs(raw_inputs, profile_ids=profile_ids)

    node_inputs = state.setdefault("node_inputs", {})
    node_inputs[node_name] = sanitized
    _save_node_provisioning(state)
    return jsonify({"ok": True, "node": node_name})


@app.post("/api/nodes/provision")
def nodes_provision() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("node", "")).strip()
    mode = str(payload.get("mode", "check")).strip().lower()
    run_ansible = bool(payload.get("run_ansible", True))

    if mode not in {"check", "apply"}:
        return jsonify({"ok": False, "error": "mode must be check or apply"}), 400

    nodes = _inventory_nodes()
    if node_name:
        target_nodes = [n for n in nodes if n["name"] == node_name]
        if not target_nodes:
            return jsonify({"ok": False, "error": "node not found in active inventory"}), 404
    else:
        target_nodes = nodes

    state = _load_node_provisioning()
    results: List[Dict[str, Any]] = []
    missing_by_node: List[Dict[str, Any]] = []

    for node in target_nodes:
        profile_ids = _effective_node_profiles(node, state)
        missing = _missing_required_inputs(node["name"], profile_ids, state)
        if missing:
            missing_by_node.append(
                {
                    "node": node["name"],
                    "profiles": missing,
                }
            )

    if missing_by_node:
        return jsonify(
            {
                "ok": False,
                "error": "missing required provisioning inputs",
                "missing_inputs": missing_by_node,
            }
        ), 400

    for node in target_nodes:
        profile_ids = _effective_node_profiles(node, state)
        results.append(_apply_node_provisioning(node["name"], profile_ids, mode=mode, run_ansible=run_ansible))

    return jsonify({"ok": True, "mode": mode, "run_ansible": run_ansible, "results": results})


@app.post("/api/settings/reset")
def settings_reset() -> Any:
    payload = request.get_json(silent=True) or {}
    scope = str(payload.get("scope", "node_ops")).strip().lower()

    if scope != "node_ops":
        return jsonify({"ok": False, "error": "scope must be node_ops"}), 400

    reset_result = _reset_node_ops_settings()
    return jsonify({"ok": True, "scope": scope, "reset": reset_result})


@app.get("/api/sd/jobs/<job_id>")
def sd_job(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.get("/api/sd/image-catalog")
def sd_image_catalog() -> Any:
    return jsonify({"ok": True, "entries": _read_image_history(limit=300)})


@app.get("/api/sd/catalog")
def sd_catalog() -> Any:
    available_urls = _available_image_urls_from_history(limit=5000)
    entries = [_catalog_entry_with_availability(entry, available_urls=available_urls) for entry in IMAGE_CATALOG]
    return jsonify({"ok": True, "entries": entries})


@app.post("/api/sd/catalog-pull")
def sd_catalog_pull() -> Any:
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("catalog_ids") or []
    skip_existing = bool(payload.get("skip_existing", True))

    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "catalog_ids must be an array"}), 400

    requested_ids = [str(v).strip() for v in raw_ids if str(v).strip()]
    if not requested_ids:
        return jsonify({"ok": False, "error": "at least one catalog id is required"}), 400

    by_id = {str(e.get("id", "")): e for e in IMAGE_CATALOG}
    selected: List[Dict[str, Any]] = []
    missing: List[str] = []
    no_url: List[str] = []
    for catalog_id in requested_ids:
        entry = by_id.get(catalog_id)
        if not entry:
            missing.append(catalog_id)
            continue
        if not str(entry.get("image_url", "")).strip():
            no_url.append(catalog_id)
            continue
        selected.append(entry)

    if not selected:
        return jsonify(
            {
                "ok": False,
                "error": "no downloadable catalog entries selected",
                "missing_ids": missing,
                "missing_url_ids": no_url,
            }
        ), 400

    _ensure_image_cache_dir()
    job_id = uuid.uuid4().hex
    with CATALOG_PULL_JOBS_LOCK:
        CATALOG_PULL_JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "storage_dir": str(IMAGE_CACHE_DIR),
            "requested_ids": requested_ids,
            "accepted_ids": [str(e.get("id", "")) for e in selected],
            "missing_ids": missing,
            "missing_url_ids": no_url,
            "skip_existing": skip_existing,
            "results": [],
            "summary": {"ok": 0, "failed": 0, "skipped": 0, "total": 0},
        }

    thread = threading.Thread(target=_run_catalog_pull_job, args=(job_id, selected, skip_existing), daemon=True)
    thread.start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "accepted_ids": [str(e.get("id", "")) for e in selected],
            "missing_ids": missing,
            "missing_url_ids": no_url,
            "storage_dir": str(IMAGE_CACHE_DIR),
        }
    )


@app.get("/api/sd/catalog-pull/jobs/<job_id>")
def sd_catalog_pull_job(job_id: str) -> Any:
    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.get("/fragments/sd/image-catalog-rows")
def sd_image_catalog_rows() -> str:
    return render_template("fragments/image_catalog_rows.html", image_history=_read_image_history(limit=150))


@app.get("/api/sd/images")
def sd_images() -> Any:
    return jsonify({"ok": True, "entries": _read_image_history(limit=500)})


@app.get("/fragments/sd/images-rows")
def sd_images_rows() -> str:
    return render_template("fragments/managed_image_rows.html", image_records=_read_image_history(limit=500))


@app.post("/api/sd/images")
def sd_images_create() -> Any:
    payload = request.get_json(silent=True) or {}
    record = _upsert_image_record(payload)
    return jsonify({"ok": True, "record": record}), 201


@app.put("/api/sd/images/<record_id>")
def sd_images_update(record_id: str) -> Any:
    if not _find_image_record(record_id):
        return jsonify({"ok": False, "error": "image record not found"}), 404
    payload = request.get_json(silent=True) or {}
    payload["id"] = record_id
    record = _upsert_image_record(payload, record_id=record_id)
    return jsonify({"ok": True, "record": record})


@app.delete("/api/sd/images/<record_id>")
def sd_images_delete(record_id: str) -> Any:
    deleted = _delete_image_record(record_id)
    if not deleted:
        return jsonify({"ok": False, "error": "image record not found"}), 404
    return jsonify({"ok": True})


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8088"))
    app.run(host=host, port=port, debug=False, threaded=True)
