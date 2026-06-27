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
KEYS_DIR = DATA_DIR / "ssh_keys"
KEY_ARCHIVE_DIR = DATA_DIR / "ssh_key_archive"
KEY_ARCHIVE_INDEX_FILE = DATA_DIR / "ssh_key_archive.jsonl"
NODE_CONFIG_FILE = DATA_DIR / "node_monitor_config.json"
INVENTORY_FILE = BASE_DIR.parent / "inventory.yml"
SCRIPT_PATH = BASE_DIR.parent / "scripts" / "prepare_sd_cloud_init.sh"
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
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

IMAGE_CATALOG: List[Dict[str, Any]] = [
    {
        "id": "ubuntu-24.04-pi-arm64",
        "label": "Ubuntu Server 24.04 LTS (Raspberry Pi ARM64)",
        "distro": "Ubuntu",
        "release": "24.04 LTS",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": True,
        "image_url": "https://cdimage.ubuntu.com/releases/24.04/release/ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz",
        "image_sha256": "790652faeb4f61ce7bb12f5cb61734595c61d3cd882915b8b5f9918106c80d37",
        "docs_url": "https://ubuntu.com/download/raspberry-pi",
        "notes": "Best tested default for Raspberry Pi 5 rack nodes.",
    },
    {
        "id": "ubuntu-22.04-pi-arm64",
        "label": "Ubuntu Server 22.04 LTS (Raspberry Pi ARM64)",
        "distro": "Ubuntu",
        "release": "22.04 LTS",
        "arch": "arm64",
        "profiles": ["pi5"],
        "recommended": False,
        "image_url": "https://cdimage.ubuntu.com/releases/22.04/release/ubuntu-22.04.5-preinstalled-server-arm64+raspi.img.xz",
        "image_sha256": "fd7687c5c9422a6c7ba4717c227bf6473fe4e0c954d5a9f664201dcecc63e822",
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


def _ensure_prereqs() -> List[str]:
    errors: List[str] = []
    if not SCRIPT_PATH.exists():
        errors.append(f"Missing script: {SCRIPT_PATH}")
    if not INVENTORY_FILE.exists():
        errors.append(f"Missing inventory file: {INVENTORY_FILE}")
    return errors


def _load_node_config() -> Dict[str, Any]:
    _ensure_data_file()
    default = {
        "username": "",
        "password": "",
        "webhook_url": "",
        "verify_tls": True,
    }
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


def _generate_or_load_ssh_key(key_name: str, comment: str = "rack-bootstrap") -> Dict[str, Any]:
    _ensure_keys_dir()
    private_key = KEYS_DIR / key_name
    public_key = KEYS_DIR / f"{key_name}.pub"

    created = False
    if not private_key.exists() or not public_key.exists():
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


def _header_or_payload(payload: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        header_val = request.headers.get(key)
        if header_val:
            return str(header_val).strip()
        payload_val = payload.get(key)
        if payload_val:
            return str(payload_val).strip()
    return ""


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
        devices=_list_sd_devices(),
        callback_url_default=_default_callback_url(),
        image_catalog=IMAGE_CATALOG,
    )


@app.get("/tailscale-acl")
def tailscale_acl() -> str:
    return render_template("tailscale_acl.html", defaults=TAILSCALE_POLICY_DEFAULTS)


@app.get("/nodes")
def nodes_page() -> str:
    return render_template("nodes.html")


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True})


@app.get("/api/cloud-init/events")
def events() -> Any:
    return jsonify({"events": _read_events(limit=500)})


@app.get("/fragments/cloud-init/event-rows")
def cloud_init_event_rows() -> str:
    return render_template("fragments/callback_rows.html", events=_read_events(limit=200))


@app.post("/api/cloud-init/callback")
def callback() -> Any:
    payload_json = request.get_json(silent=True)
    payload_form = request.form.to_dict(flat=True)
    payload: Dict[str, Any] = payload_json if isinstance(payload_json, dict) and payload_json else payload_form

    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    source_ip = _first_forwarded_ip(forwarded_for) or (request.remote_addr or "")
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

    payload = request.get_json(silent=True) or {}
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

    if not key_name:
        return jsonify({"ok": False, "errors": ["key_name is required"]}), 400
    if not _is_safe_key_name(key_name):
        return jsonify({"ok": False, "errors": ["key_name may contain only letters, numbers, dot, dash, and underscore"]}), 400

    try:
        result = _generate_or_load_ssh_key(key_name, comment=comment)
    except FileNotFoundError:
        return jsonify({"ok": False, "errors": ["ssh-keygen command not found on host"]}), 500
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
    masked = {
        "username": config.get("username", ""),
        "password_set": bool(config.get("password")),
        "webhook_url": config.get("webhook_url", ""),
        "verify_tls": bool(config.get("verify_tls", True)),
    }
    return jsonify({"ok": True, "nodes": nodes, "config": masked})


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
