from __future__ import annotations

import json
import os
import logging
import hashlib
import ipaddress
import shutil
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

# Configure logging
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
APPDATA_DIR = Path(os.getenv("CALLBACK_APPDATA_DIR", str(BASE_DIR / "data"))).expanduser()
DATA_DIR = APPDATA_DIR
DATA_FILE = DATA_DIR / "callbacks.jsonl"
IMAGE_HISTORY_FILE = DATA_DIR / "image_download_catalog.jsonl"
IMAGE_CACHE_DIR = DATA_DIR / "image_cache"
KEYS_DIR = DATA_DIR / "ssh_keys"
KNOWN_HOSTS_FILE = DATA_DIR / "known_hosts"
KEY_ARCHIVE_DIR = DATA_DIR / "ssh_key_archive"
KEY_ARCHIVE_INDEX_FILE = DATA_DIR / "ssh_key_archive.jsonl"
NODE_CONFIG_FILE = DATA_DIR / "node_monitor_config.json"
NODE_PROVISION_FILE = DATA_DIR / "node_provisioning.json"
MONITOR_STATE_FILE = DATA_DIR / "node_monitor_state.json"
APP_SETTINGS_FILE = DATA_DIR / "app_settings.json"
SD_OPTION_PROFILES_FILE = DATA_DIR / "sd_option_profiles.json"
SD_OPTION_PROFILE_STATE_FILE = DATA_DIR / "sd_option_profile_state.json"
PROFILE_SEGMENTS_FILE = DATA_DIR / "profile_segments.json"
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
KUBERNETES_STATUS_LOCK = threading.Lock()
KUBERNETES_STATUS_CACHE: Dict[str, Any] = {
    "checked_at": 0.0,
    "state": {
        "available": False,
        "active_service": "",
        "k3s_binary": "",
        "has_kubeconfig": False,
        "reason": "not checked",
    },
}
from domain_defaults import IMAGE_CATALOG, IMAGE_CATALOG_FILE, NODE_PROVISION_PROFILES, TAILSCALE_POLICY_DEFAULTS



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


def _atomic_write_text(path: Path, text: str) -> None:
    # Temp-file then replace: keeps JSON files from becoming cursed half-writes.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, 0o666)
        tmp_path.replace(path)
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _default_app_settings() -> Dict[str, Any]:
    return {
        "rack_name": "homelab",
        "tailscale_tailnet": str(TAILSCALE_POLICY_DEFAULTS.get("tailnet", "")).strip(),
        "tailscale_api_key": "",
    }


def _sanitize_rack_name(value: str) -> str:
    raw = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    return cleaned[:40] or "homelab"


def _sanitize_tailscale_tailnet(value: str) -> str:
    return str(value or "").strip().lower()


def _sanitize_tailscale_api_key(value: str) -> str:
    return str(value or "").strip()


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
    tailscale_tailnet = _sanitize_tailscale_tailnet(str(loaded.get("tailscale_tailnet", defaults["tailscale_tailnet"])))
    tailscale_api_key = _sanitize_tailscale_api_key(str(loaded.get("tailscale_api_key", defaults["tailscale_api_key"])))
    return {
        "rack_name": rack_name,
        "tailscale_tailnet": tailscale_tailnet,
        "tailscale_api_key": tailscale_api_key,
    }


def _save_app_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = _load_app_settings()
    if "rack_name" in payload:
        settings["rack_name"] = _sanitize_rack_name(str(payload.get("rack_name", "")))
    if "tailscale_tailnet" in payload:
        settings["tailscale_tailnet"] = _sanitize_tailscale_tailnet(str(payload.get("tailscale_tailnet", "")))
    if "tailscale_api_key" in payload:
        settings["tailscale_api_key"] = _sanitize_tailscale_api_key(str(payload.get("tailscale_api_key", "")))
    _ensure_data_file()
    _atomic_write_text(APP_SETTINGS_FILE, json.dumps(settings, indent=2))
    return settings


def _inspect_openwrt_probe(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    host = str(payload.get("host", "")).strip()
    if not host:
        raise ValueError("host is required")

    port = int(payload.get("port", 22) or 22)
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")

    username = str(payload.get("username", "root")).strip() or "root"
    password = str(payload.get("password", ""))
    connection = str(payload.get("connection", "ssh")).strip().lower() or "ssh"
    if connection not in {"ssh", "paramiko", "local"}:
        connection = "ssh"

    become = bool(payload.get("become", False))
    module_hint = str(payload.get("module_hint", "")).strip().lower()
    ssh_key_name = str(payload.get("ssh_key_name", "")).strip()
    ssh_key_path = None
    if ssh_key_name:
        ssh_key = _resolve_openwrt_ssh_key(ssh_key_name)
        ssh_key_path = ssh_key["private_key_path"]

    discovered = _discover_openwrt_module(module_hint)
    inventory_preview = _render_openwrt_inventory_preview(
        host=host,
        port=port,
        username=username,
        password=password,
        connection=connection,
        become=become,
        module_name=discovered["name"],
        private_key_path=ssh_key_path,
    )

    probe = {
        "ok": False,
        "error": "",
        "details": "",
        "timed_out": False,
    }
    if connection == "ssh":
        probe = _probe_ssh_target(
            host=host,
            port=port,
            username=username,
            password=password,
            private_key_path=ssh_key_path,
        )

    return {
        "request": {
            "host": host,
            "port": port,
            "username": username,
            "connection": connection,
            "become": become,
            "module_hint": module_hint or None,
            "uses_password": bool(password),
            "ssh_key_name": ssh_key_name or None,
        },
        "module": discovered["name"],
        "stats": {
            "module_source": discovered["source"],
            "summary": discovered["summary"],
            "connection": connection,
            "port": port,
            "become": become,
            "uses_password": bool(password),
            "uses_ssh_key": bool(ssh_key_path),
            "inventory_lines": len(inventory_preview.splitlines()),
            "module_hint": module_hint or None,
            "ssh_key_name": ssh_key_name or None,
        },
        "inventory_preview": inventory_preview,
        "probe": probe,
    }


def _resolve_openwrt_ssh_key(key_name: str) -> Dict[str, str]:
    _ensure_keys_dir()
    name = str(key_name or "").strip()
    if not name:
        raise ValueError("ssh key name is required")

    private_key = KEYS_DIR / name
    public_key = KEYS_DIR / f"{name}.pub"
    if not private_key.exists() or not private_key.is_file():
        raise FileNotFoundError(f"private key not found: {name}")
    if not public_key.exists() or not public_key.is_file():
        raise FileNotFoundError(f"public key not found: {name}")

    return {
        "key_name": name,
        "private_key_path": str(private_key),
        "public_key_path": str(public_key),
        "public_key": public_key.read_text(encoding="utf-8").strip(),
    }


def _probe_ssh_target(*, host: str, port: int, username: str, password: str, private_key_path: str | None = None) -> Dict[str, Any]:
    _seed_known_host(host, port)
    cmd = [
        "ssh",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={KNOWN_HOSTS_FILE}",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "PreferredAuthentications=password,publickey",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-p",
        str(port),
    ]
    if private_key_path:
        key_preflight_error = _ssh_private_key_preflight(private_key_path)
        if key_preflight_error:
            return {"ok": False, "error": key_preflight_error, "details": key_preflight_error, "timed_out": False}
        cmd.extend(["-i", private_key_path])
    cmd.extend([f"{username}@{host}", "true"])

    password_input = f"{password}\n" if password else None
    try:
        proc = subprocess.run(cmd, input=password_input, capture_output=True, text=True, timeout=10, check=False)
    except FileNotFoundError:
        return {"ok": False, "error": "ssh is not installed or available on this host", "details": "", "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "ssh probe timed out", "details": "", "timed_out": True}

    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        return {"ok": True, "error": "", "details": output or "SSH authentication and connectivity succeeded.", "timed_out": False}
    return {"ok": False, "error": f"ssh probe failed with exit code {proc.returncode}", "details": output, "timed_out": False}


def _push_ssh_key_to_target(*, host: str, port: int, username: str, password: str, public_key: str, public_key_path: str, key_name: str) -> Dict[str, Any]:
    target = f"{username}@{host}"
    _seed_known_host(host, port)
    ssh_opts = [
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={KNOWN_HOSTS_FILE}",
        "-o",
        "ConnectTimeout=6",
        "-o",
        "PreferredAuthentications=password,publickey",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-p",
        str(port),
    ]
    remote_script = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        "if ! grep -Fqx \"$PUBLIC_KEY\" ~/.ssh/authorized_keys; then printf '%s\\n' \"$PUBLIC_KEY\" >> ~/.ssh/authorized_keys; fi"
    )

    env = os.environ.copy()
    env["PUBLIC_KEY"] = public_key

    cmd = ["ssh", *ssh_opts, target, "sh", "-c", remote_script]
    password_input = f"{password}\n" if password else None
    try:
        proc = subprocess.run(cmd, input=password_input, capture_output=True, text=True, timeout=20, check=False, env=env)
    except FileNotFoundError:
        return {
            "key_name": key_name,
            "pushed": False,
            "method": "ssh",
            "details": "ssh is not installed or available on this host",
            "error": "ssh is not installed or available on this host",
        }
    except subprocess.TimeoutExpired:
        return {
            "key_name": key_name,
            "pushed": False,
            "method": "ssh",
            "details": "The push attempt timed out.",
            "error": "The push attempt timed out.",
        }

    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return {
        "key_name": key_name,
        "pushed": proc.returncode == 0,
        "method": "ssh",
        "details": output or "The public key push completed without additional output.",
        "error": output if proc.returncode != 0 else "",
    }


def _discover_openwrt_module(module_hint: str) -> Dict[str, str]:
    lookup_names = []
    if module_hint:
        lookup_names.append(module_hint)
    lookup_names.extend(["opkg", "uci", "service", "sysctl", "ping"])

    known_modules = {
        "community.general.opkg": "Manage OpenWrt packages through opkg.",
        "community.general.uci": "Manage OpenWrt UCI configuration values.",
        "community.general.ping": "Simple connectivity check for the target device.",
    }

    for candidate in lookup_names:
        if candidate.startswith("community.general."):
            if candidate in known_modules:
                return {"name": candidate, "summary": known_modules[candidate], "source": "known-fallback"}
        if candidate in {"opkg", "uci", "service", "sysctl", "ping"}:
            if candidate == "opkg":
                return {"name": "community.general.opkg", "summary": known_modules["community.general.opkg"], "source": "known-fallback"}
            if candidate == "uci":
                return {"name": "community.general.uci", "summary": known_modules["community.general.uci"], "source": "known-fallback"}

    if shutil.which("ansible-doc"):
        try:
            proc = subprocess.run(["ansible-doc", "-l"], capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                output = proc.stdout or proc.stderr or ""
                for line in output.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if "openwrt" in stripped.lower() or "opkg" in stripped.lower() or "uci" in stripped.lower():
                        module_name = stripped.split()[0]
                        if module_name.startswith("community.general.") or module_name.startswith("ansible."):
                            return {"name": module_name, "summary": f"Discovered from local ansible-doc output: {module_name}", "source": "ansible-doc"}
        except OSError:
            pass

    return {
        "name": "community.general.opkg",
        "summary": known_modules["community.general.opkg"],
        "source": "fallback",
    }


def _render_openwrt_inventory_preview(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    connection: str,
    become: bool,
    module_name: str,
    private_key_path: str | None = None,
) -> str:
    lines = [
        "all:",
        "  children:",
        "    openwrt_probe:",
        "      hosts:",
        f"        openwrt-router:",
        f"          ansible_host: {host}",
        f"          ansible_port: {port}",
        f"          ansible_user: {username}",
        f"          ansible_connection: {connection}",
    ]

    if password:
        lines.append(f"          ansible_password: {password}")
    if private_key_path:
        lines.append(f"          ansible_ssh_private_key_file: {private_key_path}")
    if become:
        lines.append("          ansible_become: true")
        lines.append("          ansible_become_method: sudo")

    lines.append(f"          ansible_module: {module_name}")
    return "\n".join(lines)


def _tailscale_policy_defaults() -> Dict[str, Any]:
    settings = _load_app_settings()
    rack_name = _sanitize_rack_name(str(settings.get("rack_name", "homelab")))
    defaults = dict(TAILSCALE_POLICY_DEFAULTS)
    defaults["rack_name"] = rack_name
    defaults["homelab_tag"] = f"tag:{rack_name}"
    defaults["tailnet"] = _sanitize_tailscale_tailnet(str(settings.get("tailscale_tailnet", defaults.get("tailnet", "")))) or str(defaults.get("tailnet", "")).strip()
    defaults["tailscale_api_key_set"] = bool(str(settings.get("tailscale_api_key", "")).strip())
    return defaults


def _load_sd_option_profiles() -> Dict[str, Dict[str, Any]]:
    _ensure_data_file()
    if not SD_OPTION_PROFILES_FILE.exists():
        return {}
    try:
        loaded = json.loads(SD_OPTION_PROFILES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for name, profile in loaded.items():
        profile_name = str(name).strip()
        if not profile_name or not isinstance(profile, dict):
            continue
        options = profile.get("options") if isinstance(profile.get("options"), dict) else {}
        distribution = str(profile.get("distribution", "standard")).strip().lower() or "standard"
        if distribution not in {"standard", "k3s"}:
            distribution = "standard"
        out[profile_name] = {
            "name": profile_name,
            "description": str(profile.get("description", "")).strip(),
            "distribution": distribution,
            "options": options,
            "updated_at": str(profile.get("updated_at", "")).strip(),
        }
    return out


def _save_sd_option_profiles(profiles: Dict[str, Dict[str, Any]]) -> None:
    _ensure_data_file()
    _atomic_write_text(SD_OPTION_PROFILES_FILE, json.dumps(profiles, indent=2))


def _load_sd_option_profile_state() -> Dict[str, Any]:
    _ensure_data_file()
    if not SD_OPTION_PROFILE_STATE_FILE.exists():
        return {}
    try:
        loaded = json.loads(SD_OPTION_PROFILE_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    preferred_profile = str(loaded.get("preferred_profile", "")).strip()
    return {"preferred_profile": preferred_profile}


def _save_sd_option_profile_state(state: Dict[str, Any]) -> None:
    _ensure_data_file()
    preferred_profile = str(state.get("preferred_profile", "")).strip()
    payload = {"preferred_profile": preferred_profile}
    _atomic_write_text(SD_OPTION_PROFILE_STATE_FILE, json.dumps(payload, indent=2))


def _set_preferred_sd_option_profile(profile_name: str) -> None:
    preferred = str(profile_name or "").strip()
    _save_sd_option_profile_state({"preferred_profile": preferred})


def _ordered_sd_option_profile_names(profiles: Dict[str, Dict[str, Any]]) -> List[str]:
    preferred = str(_load_sd_option_profile_state().get("preferred_profile", "")).strip()

    def _updated_ts(name: str) -> float:
        updated_at = str((profiles.get(name) or {}).get("updated_at", "")).strip()
        if not updated_at:
            return 0.0
        try:
            return datetime.fromisoformat(updated_at).timestamp()
        except ValueError:
            return 0.0

    names = list(profiles.keys())
    names.sort(key=lambda name: (-_updated_ts(name), name.lower()))
    if preferred and preferred in profiles:
        return [preferred] + [name for name in names if name != preferred]
    return names


def _ordered_sd_option_profiles(profiles: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [profiles[name] for name in _ordered_sd_option_profile_names(profiles)]


def _load_profile_segments() -> Dict[str, List[Dict[str, Any]]]:
    _ensure_data_file()
    if not PROFILE_SEGMENTS_FILE.exists():
        return {}
    try:
        loaded = json.loads(PROFILE_SEGMENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}

    out: Dict[str, List[Dict[str, Any]]] = {}
    for profile_id, value in loaded.items():
        pid = str(profile_id).strip()
        if not pid:
            continue
        if isinstance(value, list):
            segments = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                segments.append(
                    {
                        "name": str(item.get("name", "")).strip() or "segment",
                        "config_block": str(item.get("config_block", "")).strip(),
                        "defaults_json": str(item.get("defaults_json", "")).strip(),
                        "galaxy_requirements": [str(v).strip() for v in item.get("galaxy_requirements", []) if str(v).strip()],
                        "updated_at": str(item.get("updated_at", "")).strip(),
                    }
                )
            out[pid] = segments
    return out


def _save_profile_segments(segments: Dict[str, List[Dict[str, Any]]]) -> None:
    _ensure_data_file()
    _atomic_write_text(PROFILE_SEGMENTS_FILE, json.dumps(segments, indent=2))


def _upsert_profile_segment(profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(profile_id).strip()
    if not pid:
        raise ValueError("profile_id is required")

    segments = _load_profile_segments()
    profile_segments = segments.setdefault(pid, [])
    name = str(payload.get("segment_name", "")).strip() or str(payload.get("name", "")).strip() or "segment"
    if not name:
        raise ValueError("segment_name is required")

    entry = {
        "name": name,
        "config_block": str(payload.get("config_block", "")).strip(),
        "defaults_json": str(payload.get("defaults_json", "")).strip(),
        "galaxy_requirements": [str(v).strip() for v in payload.get("galaxy_requirements", []) if str(v).strip()],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    existing_index = next((idx for idx, item in enumerate(profile_segments) if str(item.get("name", "")).strip() == name), None)
    if existing_index is None:
        profile_segments.append(entry)
    else:
        profile_segments[existing_index] = entry

    _save_profile_segments(segments)
    return entry


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
    _normalize_ssh_key_artifacts()


def _data_dir_owner() -> tuple[int, int] | None:
    try:
        stat_result = DATA_DIR.stat()
        return stat_result.st_uid, stat_result.st_gid
    except OSError:
        return None


def _chown_to_data_owner(path: Path) -> None:
    if os.geteuid() != 0:
        return
    owner = _data_dir_owner()
    if owner is None:
        return
    uid, gid = owner
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def _normalize_ssh_key_artifacts() -> None:
    try:
        key_files = list(KEYS_DIR.iterdir())
    except OSError:
        return

    for key_file in key_files:
        if not key_file.is_file():
            continue
        _chown_to_data_owner(key_file)
        target_mode = 0o644 if key_file.name.endswith(".pub") else 0o600
        try:
            os.chmod(key_file, target_mode)
        except PermissionError:
            pass


def _ensure_known_hosts_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not KNOWN_HOSTS_FILE.exists():
        KNOWN_HOSTS_FILE.touch()
    _chown_to_data_owner(KNOWN_HOSTS_FILE)
    try:
        os.chmod(KNOWN_HOSTS_FILE, 0o600)
    except PermissionError:
        pass


def _seed_known_host(host: str, port: int = 22) -> None:
    host_text = str(host or "").strip()
    if not host_text:
        return

    _ensure_known_hosts_file()
    known_hosts_path = str(KNOWN_HOSTS_FILE)
    match_tokens = [host_text]
    if int(port or 22) != 22:
        match_tokens.insert(0, f"[{host_text}]:{int(port)}")

    try:
        for token in match_tokens:
            match_proc = subprocess.run(
                ["ssh-keygen", "-F", token, "-f", known_hosts_path],
                check=False,
                capture_output=True,
                text=True,
            )
            if match_proc.returncode == 0:
                return

        scan_cmd = ["ssh-keyscan", "-T", "5"]
        if int(port or 22) != 22:
            scan_cmd.extend(["-p", str(int(port))])
        scan_cmd.append(host_text)
        scan_proc = subprocess.run(scan_cmd, check=False, capture_output=True, text=True)
        scanned = str(scan_proc.stdout or "").strip()
        if scan_proc.returncode != 0 or not scanned:
            return

        with KNOWN_HOSTS_FILE.open("a", encoding="utf-8") as kh:
            kh.write(scanned + "\n")
        _ensure_known_hosts_file()
    except (FileNotFoundError, OSError, ValueError):
        return


def _ansible_known_hosts_args() -> str:
    _ensure_known_hosts_file()
    return f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={KNOWN_HOSTS_FILE} -o PreferredAuthentications=publickey"


def _ssh_private_key_preflight(private_key_path: str) -> str:
    path = Path(str(private_key_path or "").strip())
    if not path.exists() or not path.is_file():
        return f"SSH private key not found: {path}"
    if not os.access(path, os.R_OK):
        return (
            f"SSH private key is not readable by user '{os.getenv('USER', 'current user')}': {path}. "
            f"Run: sudo chown $USER:$USER {path} {path}.pub && sudo chmod 600 {path} && sudo chmod 644 {path}.pub"
        )
    return ""


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
    hostname = event.get("hostname", "unknown")
    stage = event.get("stage", "unknown")
    instance_id = event.get("instance_id", "unknown")
    logger.info(f"Callback received: hostname={hostname}, stage={stage}, instance_id={instance_id}, source_ip={event.get('source_ip')}")
    with DATA_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def _event_matches_identity(event: Dict[str, Any], hostname: str, instance_id: str, fqdn: str) -> bool:
    if hostname and str(event.get("hostname", "")).strip() == hostname:
        return True
    if instance_id and str(event.get("instance_id", "")).strip() == instance_id:
        return True
    if fqdn and str(event.get("fqdn", "")).strip() == fqdn:
        return True
    return False


def _parse_event_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _callback_event_fingerprint(event: Dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    seed = {
        "hostname": str(event.get("hostname", "")).strip(),
        "instance_id": str(event.get("instance_id", "")).strip(),
        "fqdn": str(event.get("fqdn", "")).strip(),
        "profile": str(event.get("profile", "")).strip().lower(),
        "stage": str(event.get("stage", "")).strip(),
        "source_ip": str(event.get("source_ip", "")).strip(),
        "source_ip_source": str(event.get("source_ip_source", "")).strip(),
        "cert_subject": str(event.get("cert_subject", "")).strip(),
        "cert_fingerprint": str(event.get("cert_fingerprint", "")).strip(),
        "raw": raw,
    }
    return json.dumps(seed, sort_keys=True, separators=(",", ":"))


def _is_duplicate_callback_event(event: Dict[str, Any], within_seconds: int = 15) -> bool:
    hostname = str(event.get("hostname", "")).strip()
    instance_id = str(event.get("instance_id", "")).strip()
    fqdn = str(event.get("fqdn", "")).strip()
    if not any((hostname, instance_id, fqdn)):
        return False

    current_ts = _parse_event_time(str(event.get("received_at", "")))
    current_fp = _callback_event_fingerprint(event)

    for prior in _read_events(limit=120):
        if not isinstance(prior, dict):
            continue
        if not _event_matches_identity(prior, hostname, instance_id, fqdn):
            continue
        if _callback_event_fingerprint(prior) != current_fp:
            continue
        prior_ts = _parse_event_time(str(prior.get("received_at", "")))
        if current_ts is None or prior_ts is None:
            return True
        delta = abs((current_ts - prior_ts).total_seconds())
        if delta <= within_seconds:
            return True
    return False


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


def _event_matches_node_aliases(event: Dict[str, Any], aliases: set[str]) -> bool:
    host = str(event.get("hostname", "")).strip().lower()
    fqdn = str(event.get("fqdn", "")).strip().lower()
    event_aliases: set[str] = set()
    event_aliases.update(_node_aliases(host))
    event_aliases.update(_node_aliases(fqdn))
    return bool(event_aliases.intersection(aliases))


def _keys_matching_aliases(mapping: Dict[str, Any], aliases: set[str]) -> List[str]:
    matched: List[str] = []
    for key in mapping.keys():
        key_aliases = _node_aliases(str(key or ""))
        if key_aliases.intersection(aliases):
            matched.append(str(key))
    return matched


def _prune_stale_nodes(max_age_hours: int = 168, dry_run: bool = False) -> Dict[str, Any]:
    hours = max(1, int(max_age_hours))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    node_inventory = _node_ops_inventory_view()
    stale_nodes: List[Dict[str, Any]] = []
    stale_aliases: set[str] = set()

    for node in node_inventory:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "")).strip()
        last_callback = _parse_event_time(str(node.get("last_callback", "")))
        if not node_name or last_callback is None:
            continue
        if last_callback >= cutoff:
            continue

        stale_aliases.update(_node_aliases(node_name.lower()))
        stale_nodes.append(
            {
                "name": node_name,
                "group": str(node.get("group", "")).strip(),
                "address": str(node.get("address", "")).strip(),
                "last_callback": str(node.get("last_callback", "")).strip(),
                "address_source": str(node.get("address_source", "")).strip(),
            }
        )

    stale_nodes.sort(key=lambda item: str(item.get("name", "")).lower())

    if not stale_nodes:
        return {
            "ok": True,
            "dry_run": bool(dry_run),
            "cutoff": cutoff.isoformat(),
            "max_age_hours": hours,
            "stale_nodes": [],
            "removed": {
                "callbacks": 0,
                "managed_nodes": 0,
                "node_cloud_init": 0,
                "node_configs": 0,
                "node_inputs": 0,
                "node_results": 0,
                "manual_nodes": 0,
            },
        }

    removed_counts = {
        "callbacks": 0,
        "managed_nodes": 0,
        "node_cloud_init": 0,
        "node_configs": 0,
        "node_inputs": 0,
        "node_results": 0,
        "manual_nodes": 0,
    }

    # Plan callback event removals.
    _ensure_data_file()
    existing_lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    kept_lines: List[str] = []
    for line in existing_lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            kept_lines.append(line)
            continue
        if _event_matches_node_aliases(event, stale_aliases):
            removed_counts["callbacks"] += 1
            continue
        kept_lines.append(line)

    # Plan monitor-state removals.
    monitor_state = _load_monitor_state()
    managed_nodes = monitor_state.get("managed_nodes", {}) if isinstance(monitor_state.get("managed_nodes"), dict) else {}
    node_cloud_init = monitor_state.get("node_cloud_init", {}) if isinstance(monitor_state.get("node_cloud_init"), dict) else {}

    # Plan provisioning-state removals.
    node_prov = _load_node_provisioning()
    node_configs = node_prov.get("node_configs", {}) if isinstance(node_prov.get("node_configs"), dict) else {}
    node_inputs = node_prov.get("node_inputs", {}) if isinstance(node_prov.get("node_inputs"), dict) else {}
    node_results = node_prov.get("results", {}) if isinstance(node_prov.get("results"), dict) else {}
    manual_nodes = node_prov.get("manual_nodes", {}) if isinstance(node_prov.get("manual_nodes"), dict) else {}

    managed_keys = _keys_matching_aliases(managed_nodes, stale_aliases)
    cloud_init_keys = _keys_matching_aliases(node_cloud_init, stale_aliases)
    node_config_keys = _keys_matching_aliases(node_configs, stale_aliases)
    node_input_keys = _keys_matching_aliases(node_inputs, stale_aliases)
    node_result_keys = _keys_matching_aliases(node_results, stale_aliases)
    manual_node_keys = _keys_matching_aliases(manual_nodes, stale_aliases)

    removed_counts["managed_nodes"] = len(managed_keys)
    removed_counts["node_cloud_init"] = len(cloud_init_keys)
    removed_counts["node_configs"] = len(node_config_keys)
    removed_counts["node_inputs"] = len(node_input_keys)
    removed_counts["node_results"] = len(node_result_keys)
    removed_counts["manual_nodes"] = len(manual_node_keys)

    if not dry_run:
        rendered = "\n".join(kept_lines)
        DATA_FILE.write_text((rendered + "\n") if kept_lines else "", encoding="utf-8")

        for key in managed_keys:
            managed_nodes.pop(key, None)
        for key in cloud_init_keys:
            node_cloud_init.pop(key, None)
        for key in node_config_keys:
            node_configs.pop(key, None)
        for key in node_input_keys:
            node_inputs.pop(key, None)
        for key in node_result_keys:
            node_results.pop(key, None)
        for key in manual_node_keys:
            manual_nodes.pop(key, None)

        monitor_state["managed_nodes"] = managed_nodes
        monitor_state["node_cloud_init"] = node_cloud_init
        _save_monitor_state(monitor_state)

        node_prov["node_configs"] = node_configs
        node_prov["node_inputs"] = node_inputs
        node_prov["results"] = node_results
        node_prov["manual_nodes"] = manual_nodes
        _save_node_provisioning(node_prov)

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "cutoff": cutoff.isoformat(),
        "max_age_hours": hours,
        "stale_nodes": stale_nodes,
        "removed": removed_counts,
    }


def _backfill_event_profile(hostname: str, instance_id: str, fqdn: str, profile: str) -> int:
    profile_value = str(profile or "").strip().lower()
    if profile_value not in {"pi5", "pibplus"}:
        return 0

    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0

    updated = 0
    rewritten: List[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        event_profile = str(event.get("profile", "")).strip().lower()
        if event_profile and event_profile != "unknown":
            rewritten.append(line)
            continue

        if not _event_matches_identity(event, hostname, instance_id, fqdn):
            rewritten.append(line)
            continue

        event["profile"] = profile_value
        event["profile_source"] = event.get("profile_source") or "backfilled"
        rewritten.append(json.dumps(event, separators=(",", ":")))
        updated += 1

    if updated:
        DATA_FILE.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return updated


def _backfill_event_source_ip(hostname: str, instance_id: str, fqdn: str, source_ip: str, source_ip_source: str) -> int:
    ip_value = _normalize_ip(source_ip)
    if not ip_value or _is_transit_hop_ip(ip_value):
        return 0

    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0

    updated = 0
    rewritten: List[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        if not _event_matches_identity(event, hostname, instance_id, fqdn):
            rewritten.append(line)
            continue

        current_ip = _normalize_ip(str(event.get("source_ip", "")))
        current_source = str(event.get("source_ip_source", "")).strip().lower()
        if current_ip == ip_value:
            rewritten.append(line)
            continue

        if current_source in {"remote_addr", ""} or _is_transit_hop_ip(current_ip):
            event["source_ip"] = ip_value
            event["source_ip_source"] = source_ip_source or "backfilled"
            rewritten.append(json.dumps(event, separators=(",", ":")))
            updated += 1
            continue

        rewritten.append(line)

    if updated:
        DATA_FILE.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return updated


def _backfill_event_fields(hostname: str, instance_id: str, fqdn: str, event_fields: Dict[str, Any]) -> int:
    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0

    incoming = {
        "hostname": str(event_fields.get("hostname", "")).strip(),
        "instance_id": str(event_fields.get("instance_id", "")).strip(),
        "fqdn": str(event_fields.get("fqdn", "")).strip(),
        "profile": str(event_fields.get("profile", "")).strip().lower(),
        "raw_profile": str(event_fields.get("raw_profile", "")).strip().lower(),
        "profile_source": str(event_fields.get("profile_source", "")).strip(),
        "stage": str(event_fields.get("stage", "")).strip(),
        "cert_subject": str(event_fields.get("cert_subject", "")).strip(),
        "cert_issuer": str(event_fields.get("cert_issuer", "")).strip(),
        "cert_fingerprint": str(event_fields.get("cert_fingerprint", "")).strip(),
        "node_report_id": str(event_fields.get("node_report_id", "")).strip(),
        "source_ip_source": str(event_fields.get("source_ip_source", "")).strip(),
    }
    incoming_cert_present = bool(event_fields.get("cert_present", False))

    updated = 0
    rewritten: List[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        if not _event_matches_identity(event, hostname, instance_id, fqdn):
            rewritten.append(line)
            continue

        changed = False

        for key in ("hostname", "instance_id", "fqdn", "cert_subject", "cert_issuer", "cert_fingerprint", "node_report_id"):
            current = str(event.get(key, "")).strip()
            if not current and incoming.get(key):
                event[key] = incoming[key]
                changed = True

        current_profile = str(event.get("profile", "")).strip().lower()
        if incoming["profile"] in {"pi5", "pibplus"} and current_profile in {"", "unknown"}:
            event["profile"] = incoming["profile"]
            changed = True

        current_raw_profile = str(event.get("raw_profile", "")).strip().lower()
        if not current_raw_profile and incoming["raw_profile"]:
            event["raw_profile"] = incoming["raw_profile"]
            changed = True

        current_profile_source = str(event.get("profile_source", "")).strip()
        if not current_profile_source and incoming["profile_source"]:
            event["profile_source"] = incoming["profile_source"]
            changed = True

        current_stage = str(event.get("stage", "")).strip()
        if incoming["stage"] and current_stage in {"", "phone_home"}:
            event["stage"] = incoming["stage"]
            changed = True

        current_source_ip_source = str(event.get("source_ip_source", "")).strip()
        if not current_source_ip_source and incoming["source_ip_source"]:
            event["source_ip_source"] = incoming["source_ip_source"]
            changed = True

        if incoming_cert_present and not bool(event.get("cert_present", False)):
            event["cert_present"] = True
            changed = True

        if changed:
            rewritten.append(json.dumps(event, separators=(",", ":")))
            updated += 1
        else:
            rewritten.append(line)

    if updated:
        DATA_FILE.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return updated


def _store_callback_event(event: Dict[str, Any]) -> bool:
    hostname = str(event.get("hostname", "")).strip()
    instance_id = str(event.get("instance_id", "")).strip()
    fqdn = str(event.get("fqdn", "")).strip()
    profile = str(event.get("profile", "")).strip().lower()
    source_ip = str(event.get("source_ip", "")).strip()
    source_ip_source = str(event.get("source_ip_source", "")).strip()

    deduped = _is_duplicate_callback_event(event)
    if not deduped:
        _append_event(event)

    _backfill_event_profile(hostname, instance_id, fqdn, profile)
    _backfill_event_source_ip(hostname, instance_id, fqdn, source_ip, source_ip_source)
    _backfill_event_fields(hostname, instance_id, fqdn, event)
    return deduped


def _append_image_history(entry: Dict[str, Any]) -> None:
    _ensure_image_history_file()
    line = json.dumps(entry, separators=(',', ':'))
    try:
        with IMAGE_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except PermissionError:
        existing_lines: List[str] = []
        if IMAGE_HISTORY_FILE.exists():
            try:
                existing_lines = IMAGE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
            except OSError:
                existing_lines = []
        _atomic_write_text(IMAGE_HISTORY_FILE, "\n".join([*existing_lines, line]) + "\n")


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
    # Rewrite in one shot so history stays coherent even if the process gets bonked.
    rendered = "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries)
    _atomic_write_text(IMAGE_HISTORY_FILE, rendered)


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


def _compact_path_label(value: str) -> str:
    text = str(value or "").strip()
    if not text or "/" not in text:
        return text
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return text
    return f"{parts[-2]}/{parts[-1]}"


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

    # The newest record is not always the best cache-state signal (for example,
    # records captured before cache path resolution). Prefer the most recent
    # record that has a known local cache path or local-available flag.
    state_entry = latest
    for record in history:
        record_cache_path = str(record.get("download_path") or record.get("cache_path") or "").strip()
        if record_cache_path or bool(record.get("local_available", False)):
            state_entry = record
            break

    latest_available = bool(state_entry.get("local_available", False))
    cache_path = str(state_entry.get("download_path") or state_entry.get("cache_path") or "")
    latest_state_emoji = "🎉" if latest_available else "🕳️"
    latest_state_text = "Cached and ready" if latest_available else "No cached image recorded yet"

    return {
        "total_records": len(history),
        "latest_label": latest_label,
        "latest_status": latest_status,
        "latest_recorded_at": latest_recorded_at,
        "latest_available": latest_available,
        "latest_cache_path": cache_path,
        "latest_cache_path_short": _compact_path_label(cache_path),
        "latest_state_emoji": latest_state_emoji,
        "latest_state_text": latest_state_text,
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


def _catalog_pull_temp_path(target: Path) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=f"{target.name}.", suffix=".part", dir=str(target.parent))
    os.close(fd)
    return Path(temp_name)


def _download_catalog_image(
    url: str,
    target: Path,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> str:
    req = urlrequest.Request(url, headers={"User-Agent": "callback-site-catalog-pull/1.0"})
    digest = hashlib.sha256()
    with urlrequest.urlopen(req, timeout=60) as resp, target.open("wb") as dst:  # nosec B310
        total_bytes_raw = resp.headers.get("Content-Length")
        total_bytes: int | None = None
        if total_bytes_raw:
            try:
                total_bytes = int(total_bytes_raw)
            except (TypeError, ValueError):
                total_bytes = None

        downloaded_bytes = 0
        if on_progress:
            on_progress(downloaded_bytes, total_bytes)

        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
            downloaded_bytes += len(chunk)
            if on_progress:
                on_progress(downloaded_bytes, total_bytes)
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
    logger.info(f"Catalog pull job started: job_id={job_id}, entries={len(entries)}, skip_existing={skip_existing}")
    
    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            logger.warning(f"Catalog pull job {job_id} not found in CATALOG_PULL_JOBS")
            return
        job["status"] = "running"
        job["started_at"] = datetime.now(timezone.utc).isoformat()
        job["summary"] = {
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "running": len(entries),
            "total": len(entries),
        }

    for entry in entries:
        entry_id = str(entry.get("id", ""))
        label = str(entry.get("label", entry_id))
        image_url = str(entry.get("image_url", "")).strip()
        expected_sha = str(entry.get("image_sha256", "")).strip().lower()
        profile_value = ",".join(entry.get("profiles", [])) if isinstance(entry.get("profiles"), list) else ""
        
        logger.debug(f"Catalog pull: processing entry {entry_id} ({label}) from {image_url[:50]}...")

        item_result: Dict[str, Any] = {
            "id": entry_id,
            "label": label,
            "url": image_url,
            "status": "downloading",
            "download_path": "",
            "observed_sha256": "",
            "expected_sha256": expected_sha,
            "message": "",
            "bytes_downloaded": 0,
            "bytes_total": None,
            "progress_percent": 0,
        }

        with CATALOG_PULL_JOBS_LOCK:
            job = CATALOG_PULL_JOBS.get(job_id)
            if job:
                job.setdefault("results", []).append(item_result)

        filename = _safe_catalog_filename(image_url, entry_id)
        target = IMAGE_CACHE_DIR / filename
        tmp: Path | None = None

        try:
            if target.exists() and skip_existing:
                item_result["status"] = "skipped"
                item_result["download_path"] = str(target)
                item_result["message"] = "already exists"
                continue

            for stale_part in sorted(IMAGE_CACHE_DIR.glob(f"{target.name}*.part")):
                try:
                    stale_part.unlink(missing_ok=True)
                except OSError:
                    logger.warning(f"Unable to remove stale catalog temp file: {stale_part}")

            tmp = _catalog_pull_temp_path(target)

            def _on_item_progress(downloaded: int, total: int | None) -> None:
                percent = 0
                if total and total > 0:
                    percent = int(max(0, min(100, round((downloaded / total) * 100))))
                with CATALOG_PULL_JOBS_LOCK:
                    active_job = CATALOG_PULL_JOBS.get(job_id)
                    if not active_job:
                        return
                    for result in active_job.get("results", []):
                        if result is item_result:
                            result["bytes_downloaded"] = downloaded
                            result["bytes_total"] = total
                            result["progress_percent"] = percent
                            break

            observed_sha = _download_catalog_image(image_url, tmp, on_progress=_on_item_progress)
            tmp.replace(target)

            hash_status = "verified" if expected_sha and observed_sha == expected_sha else "downloaded"
            if expected_sha and observed_sha != expected_sha:
                hash_status = "failed"

            item_result["status"] = "ok" if hash_status != "failed" else "failed"
            item_result["download_path"] = str(target)
            item_result["observed_sha256"] = observed_sha
            item_result["message"] = hash_status
            item_result["progress_percent"] = 100

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
            try:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass

        with CATALOG_PULL_JOBS_LOCK:
            job = CATALOG_PULL_JOBS.get(job_id)
            if job:
                results = job.get("results", [])
                failed = sum(1 for r in results if r.get("status") == "failed")
                skipped = sum(1 for r in results if r.get("status") == "skipped")
                ok = sum(1 for r in results if r.get("status") == "ok")
                running = sum(1 for r in results if r.get("status") == "downloading")
                job["summary"] = {
                    "ok": ok,
                    "failed": failed,
                    "skipped": skipped,
                    "running": running,
                    "total": len(entries),
                }

    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            return
        results = job.get("results", [])
        failed = sum(1 for r in results if r.get("status") == "failed")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        ok = sum(1 for r in results if r.get("status") == "ok")
        job["summary"] = {"ok": ok, "failed": failed, "skipped": skipped, "running": 0, "total": len(entries)}
        job["status"] = "completed_with_errors" if failed else "completed"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(f"Catalog pull job completed: job_id={job_id}, ok={ok}, failed={failed}, skipped={skipped}")


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
    return {"node_configs": {}, "results": {}, "node_inputs": {}, "manual_nodes": {}}


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
    _atomic_write_text(MONITOR_STATE_FILE, json.dumps(state, indent=2))


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
    managed_nodes = state.get("managed_nodes", {}) if isinstance(state.get("managed_nodes", {}), dict) else {}
    node_cloud_init = state.get("node_cloud_init", {}) if isinstance(state.get("node_cloud_init", {}), dict) else {}
    events = _read_events(limit=2000)
    callbacks = _last_callback_by_host()
    hardware_by_host = _latest_hardware_by_host(limit=2000)

    combined: Dict[str, Dict[str, Any]] = {}

    # Auto-discover nodes from callback events so fresh nodes appear without manual add.
    # This keeps the monitor empty on a brand-new state until the first callback lands.
    for event in events:
        node_name = str(event.get("hostname", "")).strip()
        if not node_name or node_name in combined:
            continue
        event_profile = str(event.get("profile", "")).strip().lower()
        if event_profile == "pi5":
            group = "rpi5_active"
        elif event_profile == "pibplus":
            group = "rpi_bplus_active"
        else:
            group = "discovered"

        address = str(event.get("source_ip", "")).strip() or node_name
        combined[node_name] = {
            "name": node_name,
            "group": group,
            "address": address,
            "notes": "",
            "enabled": True,
            "source": "callback",
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
                "source": "callback+managed",
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

        hw: Dict[str, Any] = {}
        for alias in _node_aliases(node_name):
            hw = hardware_by_host.get(alias, {})
            if hw:
                break
        enriched["hardware"] = hw

        nodes.append(enriched)

    raw_options = state.get("cloud_init_options", {}) if isinstance(state.get("cloud_init_options", {}), dict) else {}
    cloud_init_options = [v for v in raw_options.values() if isinstance(v, dict)]
    cloud_init_options.sort(key=lambda item: str(item.get("label", "")).lower())

    return {
        "nodes": nodes,
        "cloud_init_options": cloud_init_options,
        "node_cloud_init": node_cloud_init,
    }


def _single_source_of_truth_summary() -> Dict[str, Any]:
    monitor_summary = _monitor_nodes_summary()
    provision_state = _load_node_provisioning()
    node_config = _load_node_config()
    nodes_view = _node_ops_inventory_view()
    image_summary = _image_state_summary()
    image_history = _read_image_history(limit=1000)

    latest_image_by_profile: Dict[str, Dict[str, Any]] = {}
    for record in image_history:
        profile_key = str(record.get("profile", "")).strip().lower() or "unknown"
        if profile_key in latest_image_by_profile:
            continue
        latest_image_by_profile[profile_key] = {
            "profile": profile_key,
            "label": str(record.get("label", "")).strip(),
            "source": str(record.get("source", "")).strip(),
            "source_value": str(record.get("source_value", "")).strip(),
            "download_path": str(record.get("download_path", "")).strip(),
            "recorded_at": str(record.get("recorded_at", "")).strip(),
            "local_available": bool(record.get("local_available", False)),
            "hash_status": str(record.get("hash_status", "")).strip(),
            "job_status": str(record.get("job_status", "")).strip(),
        }

    profile_meta: Dict[str, Dict[str, Any]] = {}
    for profile in NODE_PROVISION_PROFILES:
        if not isinstance(profile, dict):
            continue
        pid = str(profile.get("id", "")).strip()
        if not pid:
            continue
        profile_meta[pid] = {
            "id": pid,
            "label": str(profile.get("label", pid)).strip() or pid,
            "description": str(profile.get("description", "")).strip(),
            "tags": [str(tag).strip() for tag in profile.get("tags", []) if str(tag).strip()],
        }

    monitor_nodes = monitor_summary.get("nodes", []) if isinstance(monitor_summary.get("nodes", []), list) else []
    monitor_by_name: Dict[str, Dict[str, Any]] = {}
    for monitor_node in monitor_nodes:
        if not isinstance(monitor_node, dict):
            continue
        monitor_name = str(monitor_node.get("name", "")).strip()
        if monitor_name:
            monitor_by_name[monitor_name] = monitor_node

    node_ops_by_name: Dict[str, Dict[str, Any]] = {}
    for node in nodes_view:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "")).strip()
        if node_name:
            node_ops_by_name[node_name] = node

    normalized_nodes: List[Dict[str, Any]] = []
    all_node_names = sorted(set(node_ops_by_name.keys()).union(monitor_by_name.keys()), key=lambda name: name.lower())

    for node_name in all_node_names:
        node = dict(node_ops_by_name.get(node_name, {}))
        monitor_info = monitor_by_name.get(node_name, {})
        if not node:
            node = {
                "name": node_name,
                "group": str(monitor_info.get("group", "")).strip(),
                "address": str(monitor_info.get("address", "")).strip(),
                "address_source": "monitor",
                "last_callback": str(monitor_info.get("last_callback", "")).strip(),
                "source": str(monitor_info.get("source", "")).strip() or "monitor",
            }

        selected_profiles = _effective_node_profiles(node, provision_state)
        latest_check = _latest_node_check(node_name, provision_state)
        latest_check_steps: List[Dict[str, Any]] = []
        for raw_step in latest_check.get("results", []) if isinstance(latest_check, dict) else []:
            if not isinstance(raw_step, dict):
                continue
            latest_check_steps.append(
                {
                    "profile_id": str(raw_step.get("profile_id", "")).strip(),
                    "status": str(raw_step.get("status", "")).strip(),
                    "changed": bool(raw_step.get("changed", False)),
                    "command": str(raw_step.get("command", "")).strip(),
                    "output": str(raw_step.get("output", "")).strip()[:220],
                }
            )

        callback_profile = str(node.get("callback_profile", "")).strip().lower()
        image_profile_key = callback_profile or (selected_profiles[0] if selected_profiles else "")
        image_for_node = latest_image_by_profile.get(image_profile_key) if image_profile_key else {}
        cert_info = monitor_info.get("cert", {}) if isinstance(monitor_info.get("cert"), dict) else {}
        hardware_info = monitor_info.get("hardware", {}) if isinstance(monitor_info.get("hardware"), dict) else {}

        # Keep all existing node-ops fields while adding normalized contract fields.
        node_record = dict(node)
        node_record.update(
            {
                "in_node_ops_view": node_name in node_ops_by_name,
                "in_monitor_view": node_name in monitor_by_name,
                "source": str(node.get("source", "")).strip()
                or str(monitor_info.get("source", "")).strip()
                or ("node_ops" if node_name in node_ops_by_name else "monitor"),
                "callback_profile": callback_profile,
                "selected_profiles": selected_profiles,
                "provision_how": [profile_meta.get(pid, {"id": pid, "label": pid, "description": "", "tags": []}) for pid in selected_profiles],
                "latest_check": latest_check,
                "latest_check_steps": latest_check_steps,
                "image": image_for_node,
                "cert": cert_info,
                "hardware": hardware_info,
            }
        )

        normalized_nodes.append(node_record)

    hardware_nodes = sum(1 for node in monitor_nodes if isinstance(node, dict) and isinstance(node.get("hardware"), dict) and node.get("hardware", {}).get("received_at"))
    node_ops_nodes = sum(1 for node in normalized_nodes if bool(node.get("in_node_ops_view", False)))
    masked_config = {
        "username": str(node_config.get("username", "")).strip(),
        "password_set": bool(node_config.get("password")),
        "webhook_url": str(node_config.get("webhook_url", "")).strip(),
        "verify_tls": bool(node_config.get("verify_tls", True)),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "nodes_in_monitor": len(monitor_nodes),
            "nodes_in_node_ops": node_ops_nodes,
            "nodes_with_hardware": hardware_nodes,
            "catalog_entries": int(image_summary.get("catalog_entries", 0) or 0),
            "catalog_available": int(image_summary.get("available_catalog_entries", 0) or 0),
            "image_history_records": int(image_summary.get("total_records", 0) or 0),
        },
        "images": {
            "summary": image_summary,
            "latest_by_profile": latest_image_by_profile,
        },
        "monitor": {
            "cloud_init_options": monitor_summary.get("cloud_init_options", []),
            "node_cloud_init": monitor_summary.get("node_cloud_init", {}),
        },
        "provisioning": {
            "config": masked_config,
            "node_inputs": provision_state.get("node_inputs", {}),
            "manual_nodes": provision_state.get("manual_nodes", {}),
        },
        "nodes": normalized_nodes,
        "profiles": list(profile_meta.values()),
        "sources": {
            "callbacks": str(DATA_FILE),
            "monitor_state": str(MONITOR_STATE_FILE),
            "node_provisioning": str(NODE_PROVISION_FILE),
            "image_history": str(IMAGE_HISTORY_FILE),
            "image_catalog": str(IMAGE_CATALOG_FILE),
            "inventory": str(INVENTORY_FILE),
        },
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
    _atomic_write_text(NODE_CONFIG_FILE, json.dumps(config, indent=2))
    return config


def _default_profiles_for_group(group: str) -> List[str]:
    defaults = ["baseline", "tailscale", "telegraf"]
    if group == "rpi5_active":
        defaults.append("docker")
    return defaults


def _trigger_initial_callback_closer(event: Dict[str, Any]) -> None:
    hostname = str(event.get("hostname", "") or "").strip()
    if not hostname:
        return

    prior_events = _read_events(limit=2000)
    has_prior_event = any(
        str(prior.get("hostname", "") or "").strip() == hostname
        or str(prior.get("fqdn", "") or "").strip() == hostname
        for prior in prior_events
    )
    if has_prior_event:
        return

    profile = str(event.get("profile", "") or "").strip().lower()
    profile_group = ""
    if profile == "pi5":
        profile_group = "rpi5_active"
    elif profile == "pibplus":
        profile_group = "rpi_bplus_active"

    if not profile_group:
        return

    provision_state = _load_node_provisioning()
    node = {"name": hostname, "group": profile_group}
    profile_ids = _effective_node_profiles(node, provision_state)
    if not profile_ids:
        return

    logger.info(
        "Auto-triggering initial closer check for hostname=%s profiles=%s stage=%s",
        hostname,
        profile_ids,
        str(event.get("stage", "") or "").strip().lower() or "unknown",
    )
    _apply_node_provisioning(hostname, profile_ids, mode="check", run_ansible=True)


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
    manual_nodes = loaded.get("manual_nodes", {})
    if not isinstance(node_configs, dict):
        node_configs = {}
    if not isinstance(results, dict):
        results = {}
    if not isinstance(node_inputs, dict):
        node_inputs = {}
    if not isinstance(manual_nodes, dict):
        manual_nodes = {}
    return {"node_configs": node_configs, "results": results, "node_inputs": node_inputs, "manual_nodes": manual_nodes}


def _save_node_provisioning(state: Dict[str, Any]) -> None:
    _ensure_data_file()
    _atomic_write_text(NODE_PROVISION_FILE, json.dumps(state, indent=2))


def _reset_node_ops_settings() -> Dict[str, Any]:
    def _purge_files(path: Path) -> int:
        if not path.exists() or not path.is_dir():
            return 0
        removed = 0
        for child in path.iterdir():
            if child.is_file():
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    _ensure_data_file()
    node_cfg = _default_node_config()
    node_prov = _default_node_provisioning()
    _atomic_write_text(NODE_CONFIG_FILE, json.dumps(node_cfg, indent=2))
    _atomic_write_text(NODE_PROVISION_FILE, json.dumps(node_prov, indent=2))

    deleted_ssh_files = _purge_files(KEYS_DIR)
    deleted_archive_files = _purge_files(KEY_ARCHIVE_DIR)
    if KEY_ARCHIVE_INDEX_FILE.exists():
        try:
            KEY_ARCHIVE_INDEX_FILE.unlink()
        except OSError:
            pass
    return {
        "node_config": True,
        "node_provisioning": True,
        "deleted_ssh_files": deleted_ssh_files,
        "deleted_archive_files": deleted_archive_files,
        "deleted_archive_index": not KEY_ARCHIVE_INDEX_FILE.exists(),
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


def _run_node_profile(
    node_name: str,
    profile: Dict[str, Any],
    mode: str,
    run_ansible: bool,
    profile_inputs: Dict[str, str],
    node_group: str | None = None,
    node_address: str | None = None,
    node_username: str | None = None,
    node_certificate: str | None = None,
) -> Dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    profile_id = str(profile.get("id", ""))
    tags = profile.get("tags") or []

    logger.info(
        "Starting provisioning for node=%s profile=%s mode=%s run_ansible=%s",
        node_name,
        profile_id,
        mode,
        run_ansible,
    )

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

    ansible_cmd = shutil.which("ansible-playbook")
    if not ansible_cmd:
        venv_ansible = BASE_DIR.parent / ".venv" / "bin" / "ansible-playbook"
        if venv_ansible.exists() and venv_ansible.is_file():
            ansible_cmd = str(venv_ansible)

    node_config = _load_node_config()
    ssh_user = str(node_username or "").strip() or str(node_config.get("username", "") or "").strip() or "jdizzle"
    cert_name = str(node_certificate or "").strip()
    ssh_key_path = str(KEYS_DIR / "rack-bootstrap-ed25519")
    if cert_name and _is_safe_key_name(cert_name):
        ssh_key_path = str(KEYS_DIR / cert_name)

    inventory_path = str(INVENTORY_FILE)
    limit_target = node_name
    temporary_inventory_path = None
    host_target = str(node_address or "").strip()
    if host_target:
        _seed_known_host(host_target)

    key_preflight_error = _ssh_private_key_preflight(ssh_key_path)
    if key_preflight_error:
        return {
            "profile_id": profile_id,
            "status": "failed",
            "mode": mode,
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": key_preflight_error,
        }

    resolved_group = str(node_group or "").strip()
    if host_target:
        known_hosts_args = _ansible_known_hosts_args()
        temp_inventory = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8")
        temp_inventory.write("all:\n")
        temp_inventory.write("  children:\n")
        temp_inventory.write(f"    {resolved_group or 'dynamic_provisioning'}:\n")
        temp_inventory.write("      hosts:\n")
        temp_inventory.write(f"        {node_name}:\n")
        temp_inventory.write(f"          ansible_host: {host_target}\n")
        temp_inventory.write(f"          ansible_user: {ssh_user}\n")
        temp_inventory.write(f"          ansible_ssh_private_key_file: {ssh_key_path}\n")
        temp_inventory.write(f"          ansible_ssh_common_args: '{known_hosts_args}'\n")
        temp_inventory.write("          ansible_connection: ssh\n")
        temp_inventory.write("          ansible_ssh_extra_args: '-o PreferredAuthentications=publickey'\n")
        temp_inventory.flush()
        temp_inventory.close()
        temporary_inventory_path = temp_inventory.name
        inventory_path = temporary_inventory_path
        limit_target = node_name

    cmd = [
        ansible_cmd or "ansible-playbook",
        "-i",
        inventory_path,
        "site.yml",
        "--limit",
        limit_target,
    ]
    env = os.environ.copy()
    env.pop("ANSIBLE_ASK_VAULT_PASS", None)
    env.pop("ANSIBLE_VAULT_IDENTITY_LIST", None)
    env.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)
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
            stdin=subprocess.DEVNULL,
            timeout=420,
            cwd=str(BASE_DIR.parent),
            env=env,
        )
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        logger.info(
            "Completed provisioning for node=%s profile=%s mode=%s status=%s exit_code=%s",
            node_name,
            profile_id,
            mode,
            "ok" if proc.returncode == 0 else "failed",
            proc.returncode,
        )
        if proc.returncode != 0 and ("vault" in output.lower() or "password" in output.lower() or "prompt" in output.lower()):
            return {
                "profile_id": profile_id,
                "status": "failed",
                "mode": mode,
                "checked_at": checked_at,
                "command": cmd_for_log,
                "exit_code": proc.returncode,
                "inputs": _redact_profile_inputs(profile_id, profile_inputs),
                "output": output[-12000:] or "Ansible failed before provisioning completed. Check vault/password prompts and credentials.",
            }
        if proc.returncode != 0 and ("permission denied" in output.lower() or "unreachable" in output.lower() or "failed to connect" in output.lower()):
            ssh_hint = (
                "SSH authentication or reachability failed. Ensure the target node has the bootstrap SSH key installed "
                "for the expected user (often root or ops) and that the node is reachable at the configured address."
            )
            return {
                "profile_id": profile_id,
                "status": "failed",
                "mode": mode,
                "checked_at": checked_at,
                "command": cmd_for_log,
                "exit_code": proc.returncode,
                "inputs": _redact_profile_inputs(profile_id, profile_inputs),
                "output": f"{output[-12000:]}\n\n{ssh_hint}",
            }
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


def _run_hardware_check_callback(node_name: str, node_group: str | None = None, node_address: str | None = None) -> Dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    host_target = str(node_address or "").strip()
    if not host_target:
        return {
            "profile_id": "hardware_check_trigger",
            "status": "failed",
            "mode": "check",
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": {},
            "output": "Cannot trigger hardware-check callback because node address is empty.",
        }

    node_entry = next(
        (item for item in _node_ops_inventory_view() if isinstance(item, dict) and str(item.get("name", "")).strip() == node_name),
        {},
    )
    node_config = _load_node_config()
    ssh_user = str(node_entry.get("node_username", "") or "").strip() or str(node_config.get("username", "") or "").strip() or "jdizzle"
    cert_name = str(node_entry.get("certificate", "") or "").strip()
    ssh_key_path = str(KEYS_DIR / "rack-bootstrap-ed25519")
    if cert_name and _is_safe_key_name(cert_name):
        ssh_key_path = str(KEYS_DIR / cert_name)

    _seed_known_host(host_target)

    key_preflight_error = _ssh_private_key_preflight(ssh_key_path)
    if key_preflight_error:
        return {
            "profile_id": "hardware_check_trigger",
            "status": "failed",
            "mode": "check",
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": {},
            "output": key_preflight_error,
        }

    known_hosts_args = _ansible_known_hosts_args()

    ansible_cmd = shutil.which("ansible")
    if not ansible_cmd:
        venv_ansible = BASE_DIR.parent / ".venv" / "bin" / "ansible"
        if venv_ansible.exists() and venv_ansible.is_file():
            ansible_cmd = str(venv_ansible)

    temp_inventory = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8")
    temp_inventory.write("all:\n")
    temp_inventory.write("  children:\n")
    temp_inventory.write(f"    {str(node_group or '').strip() or 'dynamic_provisioning'}:\n")
    temp_inventory.write("      hosts:\n")
    temp_inventory.write(f"        {node_name}:\n")
    temp_inventory.write(f"          ansible_host: {host_target}\n")
    temp_inventory.write(f"          ansible_user: {ssh_user}\n")
    temp_inventory.write(f"          ansible_ssh_private_key_file: {ssh_key_path}\n")
    temp_inventory.write(f"          ansible_ssh_common_args: '{known_hosts_args}'\n")
    temp_inventory.write("          ansible_connection: ssh\n")
    temp_inventory.write("          ansible_ssh_extra_args: '-o PreferredAuthentications=publickey'\n")
    temp_inventory.flush()
    temp_inventory.close()

    profile_value = "pibplus" if "bplus" in str(node_group or "").lower() else "pi5"
    callback_url = _default_callback_url()
    cmd = [
        ansible_cmd or "ansible",
        "-i",
        temp_inventory.name,
        node_name,
        "-m",
        "ansible.builtin.setup",
        "-a",
        "gather_subset=hardware,network",
        "-o",
    ]
    env = os.environ.copy()
    env.pop("ANSIBLE_ASK_VAULT_PASS", None)
    env.pop("ANSIBLE_VAULT_IDENTITY_LIST", None)
    env.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=150,
            cwd=str(BASE_DIR.parent),
            env=env,
        )
        raw_output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        fact_payload: Dict[str, Any] = {}
        if proc.returncode == 0:
            try:
                parsed = json.loads(raw_output.split("=>", 1)[1].strip()) if "=>" in raw_output else json.loads(raw_output)
                if isinstance(parsed, dict):
                    fact_payload = parsed.get("ansible_facts", {}) if isinstance(parsed.get("ansible_facts"), dict) else parsed
            except (ValueError, IndexError):
                fact_payload = {}

        if proc.returncode != 0 or not isinstance(fact_payload, dict):
            return {
                "profile_id": "hardware_check_trigger",
                "status": "failed" if proc.returncode != 0 else "failed",
                "mode": "check",
                "checked_at": checked_at,
                "command": cmd,
                "exit_code": proc.returncode,
                "inputs": {},
                "output": raw_output[-12000:] or "Ansible setup failed to return hardware facts.",
            }

        model = str(
            fact_payload.get("ansible_product_name")
            or fact_payload.get("ansible_model")
            or (fact_payload.get("ansible_lsb") or {}).get("description")
            or ""
        ).strip()
        ram_mb = 0
        raw_ram_value = fact_payload.get("ansible_memtotal_mb")
        if raw_ram_value is None:
            mem_mb = fact_payload.get("ansible_memory_mb")
            if isinstance(mem_mb, dict):
                real = mem_mb.get("real") if isinstance(mem_mb.get("real"), dict) else {}
                raw_ram_value = real.get("total") if isinstance(real, dict) else mem_mb.get("total")
        try:
            ram_mb = int(float(str(raw_ram_value or 0).strip()))
        except (TypeError, ValueError):
            ram_mb = 0

        device_info = fact_payload.get("ansible_devices") if isinstance(fact_payload.get("ansible_devices"), dict) else {}
        sd_capacity_gb = 0
        for device_name, metadata in device_info.items():
            if not isinstance(metadata, dict):
                continue
            if str(device_name).startswith("mmcblk"):
                size_txt = str(metadata.get("size") or "").strip()
                value = re.search(r"(\d+(?:\.\d+)?)\s*(?:G|GB)", size_txt, flags=re.IGNORECASE)
                if value:
                    try:
                        sd_capacity_gb = max(sd_capacity_gb, int(float(value.group(1))))
                    except ValueError:
                        pass
        nvme_present = any(str(name).startswith("nvme") for name in device_info)
        payload = {
            "instance_id": f"{node_name}-hw",
            "hostname": str(fact_payload.get("ansible_hostname") or node_name).strip() or node_name,
            "profile": profile_value,
            "source_ip": host_target,
            "stage": "hardware-check",
            "node_report_b64": "",
            "hardware": {
                "model": model or "unknown",
                "ram_mb": ram_mb,
                "sd_capacity_gb": sd_capacity_gb,
                "nvme": nvme_present,
                "hat": False,
                "poe": False,
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urlrequest.Request(callback_url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlrequest.urlopen(req, timeout=12) as resp:
                callback_response = resp.read().decode("utf-8", errors="replace").strip()
        except Exception as exc:
            callback_response = json.dumps({"ok": False, "error": str(exc)})

        output = callback_response or json.dumps({"ok": True, "facts": {"model": model, "ram_mb": ram_mb}})
        return {
            "profile_id": "hardware_check_trigger",
            "status": "ok",
            "mode": "check",
            "checked_at": checked_at,
            "command": cmd,
            "exit_code": 0,
            "inputs": {},
            "output": output[-12000:],
        }
    except FileNotFoundError:
        return {
            "profile_id": "hardware_check_trigger",
            "status": "unavailable",
            "mode": "check",
            "checked_at": checked_at,
            "command": cmd,
            "exit_code": None,
            "inputs": {},
            "output": "ansible command not found on host.",
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return {
            "profile_id": "hardware_check_trigger",
            "status": "timeout",
            "mode": "check",
            "checked_at": checked_at,
            "command": cmd,
            "exit_code": None,
            "inputs": {},
            "output": ((stdout + "\n" + stderr).strip() or "Hardware check trigger timed out.")[-12000:],
        }
    finally:
        try:
            Path(temp_inventory.name).unlink(missing_ok=True)
        except OSError:
            pass


def _apply_node_provisioning(node_name: str, profile_ids: List[str], mode: str, run_ansible: bool) -> Dict[str, Any]:
    profile_by_id = _profile_map()
    state = _load_node_provisioning()
    valid_profile_ids = [pid for pid in profile_ids if pid in profile_by_id]
    inputs_by_profile = _node_profile_inputs(node_name, state)
    node_view = {item.get("name"): item for item in _node_ops_inventory_view() if isinstance(item, dict) and item.get("name")}
    node_entry = node_view.get(node_name, {})
    node_group = str(node_entry.get("group", "") or "").strip()
    node_address = str(node_entry.get("address", "") or "").strip()
    node_username = str(node_entry.get("node_username", "") or "").strip()
    node_certificate = str(node_entry.get("certificate", "") or "").strip()
    node_results: Dict[str, Any] = {}
    for pid in valid_profile_ids:
        profile_inputs = inputs_by_profile.get(pid, {})
        result = _run_node_profile(
            node_name,
            profile_by_id[pid],
            mode=mode,
            run_ansible=run_ansible,
            profile_inputs=profile_inputs,
            node_group=node_group or None,
            node_address=node_address or None,
            node_username=node_username or None,
            node_certificate=node_certificate or None,
        )
        node_results[pid] = result

    ordered_result_ids = list(valid_profile_ids)
    if mode == "check" and run_ansible:
        hardware_trigger_result = _run_hardware_check_callback(
            node_name,
            node_group=node_group or None,
            node_address=node_address or None,
        )
        node_results["hardware_check_trigger"] = hardware_trigger_result
        ordered_result_ids.append("hardware_check_trigger")

    state_results = state.setdefault("results", {})
    current_results = {pid: node_results[pid] for pid in ordered_result_ids if pid in node_results}
    state_results[node_name] = current_results
    _save_node_provisioning(state)

    return {
        "node": node_name,
        "mode": mode,
        "run_ansible": run_ansible,
        "results": [node_results[pid] for pid in ordered_result_ids if pid in node_results],
    }


def _telegraf_influx_dummy_check(inputs: Dict[str, str], timeout: int = 8) -> Dict[str, Any]:
    endpoint = str(inputs.get("influx_endpoint", "")).strip().rstrip("/")
    bucket = str(inputs.get("influx_bucket", "")).strip()
    org = str(inputs.get("influx_org", "")).strip()
    access_key = str(inputs.get("influx_access_key", "")).strip()
    secret_key = str(inputs.get("influx_secret_key", "")).strip()

    required = {
        "influx_endpoint": endpoint,
        "influx_bucket": bucket,
        "influx_org": org,
        "influx_access_key": access_key,
        "influx_secret_key": secret_key,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        return {
            "ok": False,
            "error": "missing required telegraf inputs",
            "missing": missing,
            "checks": [],
        }

    parsed = urlparse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "ok": False,
            "error": "invalid influx endpoint; expected http(s) URL",
            "checks": [],
        }

    checks: List[Dict[str, Any]] = []

    def _http_error_message(exc: Exception) -> str:
        if isinstance(exc, urlerror.HTTPError):
            body = ""
            try:
                body = exc.read(512).decode("utf-8", errors="replace").strip()
            except Exception:
                body = ""
            if body:
                return f"HTTP {exc.code}: {body}"
            return f"HTTP {exc.code}"
        return str(exc)

    health_url = f"{endpoint}/health"
    try:
        req = urlrequest.Request(health_url, headers={"Accept": "application/json"}, method="GET")
        with urlrequest.urlopen(req, timeout=timeout) as resp:  # nosec B310
            checks.append(
                {
                    "name": "health",
                    "ok": 200 <= int(getattr(resp, "status", 0) or 0) < 300,
                    "status": int(getattr(resp, "status", 0) or 0),
                    "url": health_url,
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "health",
                "ok": False,
                "status": None,
                "url": health_url,
                "error": _http_error_message(exc),
            }
        )

    write_params = urlparse.urlencode({"org": org, "bucket": bucket, "precision": "s"})
    write_url = f"{endpoint}/api/v2/write?{write_params}"
    line = f"callback_site_dummy,source=node_ops,host={socket.gethostname()} value=1i {int(time.time())}"

    token_candidates: List[Dict[str, str]] = [{"label": "access_key", "token": access_key}]
    combo = f"{access_key}:{secret_key}"
    if combo != access_key:
        token_candidates.append({"label": "access_key:secret_key", "token": combo})

    write_ok = False
    last_write_error = ""
    last_status: int | None = None

    for candidate in token_candidates:
        try:
            req = urlrequest.Request(
                write_url,
                data=line.encode("utf-8"),
                headers={
                    "Authorization": f"Token {candidate['token']}",
                    "Content-Type": "text/plain; charset=utf-8",
                    "Accept": "application/json",
                },
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=timeout) as resp:  # nosec B310
                status = int(getattr(resp, "status", 0) or 0)
                last_status = status
                if status in {204, 202, 200}:
                    write_ok = True
                    checks.append(
                        {
                            "name": "dummy_write",
                            "ok": True,
                            "status": status,
                            "url": write_url,
                            "auth_mode": candidate["label"],
                        }
                    )
                    break
                last_write_error = f"unexpected status {status}"
        except Exception as exc:
            last_write_error = _http_error_message(exc)

    if not write_ok:
        checks.append(
            {
                "name": "dummy_write",
                "ok": False,
                "status": last_status,
                "url": write_url,
                "error": last_write_error or "write failed",
            }
        )

    ok = bool(checks and all(bool(c.get("ok")) for c in checks))
    return {
        "ok": ok,
        "endpoint": endpoint,
        "bucket": bucket,
        "org": org,
        "checks": checks,
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
            address = str(hv.get("ansible_host", "")).strip()
            if _is_placeholder_inventory_address(address):
                address = ""
            nodes.append(
                {
                    "name": host,
                    "group": group,
                    "address": address,
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
                "address": "",
            }
            host_indent = indent
            nodes.append(current_host)
            continue

        if current_host and indent > host_indent and key == "ansible_host":
            address = value.strip('\"\'')
            current_host["address"] = "" if _is_placeholder_inventory_address(address) else address

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
        ts = str(event.get("received_at", "")).strip()
        if not ts:
            continue
        candidates = set()
        for raw in (event.get("hostname"), event.get("fqdn")):
            candidates.update(_node_aliases(str(raw or "")))
        for host in candidates:
            if host and host not in seen:
                seen[host] = ts
    return seen


def _is_placeholder_inventory_address(address: str) -> bool:
    value = str(address or "").strip()
    if not value:
        return True
    upper = value.upper()
    return upper.startswith("CHANGE_ME") or upper.startswith("TODO")


def _latest_hardware_by_host(limit: int = 2000) -> Dict[str, Dict[str, Any]]:
    """Return the most recent hardware-check event payload per hostname."""
    seen: Dict[str, Dict[str, Any]] = {}
    seen_ts: Dict[str, datetime] = {}
    for event in _read_events(limit=limit):
        if str(event.get("stage", "")).strip() != "hardware-check":
            continue
        candidates: set[str] = set()
        for raw in (event.get("hostname"), event.get("fqdn")):
            candidates.update(_node_aliases(str(raw or "")))
        if not candidates:
            continue
        event_ts = _parse_event_time(str(event.get("received_at", "")))
        hw = event.get("raw", {})
        if isinstance(hw, dict):
            hw_block = hw.get("hardware") or {}
        else:
            hw_block = {}
        payload: Dict[str, Any] = {
            "received_at": str(event.get("received_at", "")).strip(),
            "model": str(hw_block.get("model", "")).strip(),
            "ram_mb": hw_block.get("ram_mb"),
            "sd_capacity_gb": hw_block.get("sd_capacity_gb"),
            "nvme": hw_block.get("nvme"),
            "hat": hw_block.get("hat"),
            "poe": hw_block.get("poe"),
            "node_report_id": str(event.get("node_report_id", "")).strip(),
            "source_ip": str(event.get("source_ip", "")).strip(),
        }
        for host in candidates:
            if not host:
                continue
            current_ts = seen_ts.get(host)
            if current_ts is None or event_ts is None or event_ts > current_ts:
                seen[host] = payload
                if event_ts is not None:
                    seen_ts[host] = event_ts
    return seen


def _latest_callback_details_by_host(limit: int = 2000) -> Dict[str, Dict[str, str]]:
    seen: Dict[str, Dict[str, str]] = {}
    for event in _read_events(limit=limit):
        candidates = set()
        for raw in (event.get("hostname"), event.get("fqdn")):
            candidates.update(_node_aliases(str(raw or "")))
        if not candidates:
            continue
        payload = {
            "last_callback": str(event.get("received_at", "")).strip(),
            "source_ip": str(event.get("source_ip", "")).strip(),
            "source_ip_source": str(event.get("source_ip_source", "")).strip(),
            "profile": str(event.get("profile", "")).strip(),
            "raw_profile": str(event.get("raw_profile", "")).strip(),
            "profile_source": str(event.get("profile_source", "")).strip(),
            "stage": str(event.get("stage", "")).strip(),
            "instance_id": str(event.get("instance_id", "")).strip(),
            "cert_present": bool(event.get("cert_present", False)),
            "cert_subject": str(event.get("cert_subject", "")).strip(),
            "cert_issuer": str(event.get("cert_issuer", "")).strip(),
            "cert_fingerprint": str(event.get("cert_fingerprint", "")).strip(),
            "node_report_id": str(event.get("node_report_id", "")).strip(),
            "node_report_present": bool(event.get("node_report_present", False)),
        }
        for host in candidates:
            if host and host not in seen:
                seen[host] = payload
    return seen


def _node_ops_inventory_view() -> List[Dict[str, Any]]:
    nodes = _inventory_nodes()
    callback_details = _latest_callback_details_by_host()
    provision_state = _load_node_provisioning()

    node_configs = provision_state.get("node_configs", {}) if isinstance(provision_state.get("node_configs"), dict) else {}
    node_inputs = provision_state.get("node_inputs", {}) if isinstance(provision_state.get("node_inputs"), dict) else {}
    node_results = provision_state.get("results", {}) if isinstance(provision_state.get("results"), dict) else {}
    manual_nodes = provision_state.get("manual_nodes", {}) if isinstance(provision_state.get("manual_nodes"), dict) else {}

    # Show Node Ops rows only when we have prior callback/provisioning state.
    state_node_names: set[str] = set()
    for source in (node_configs, node_inputs, node_results, manual_nodes):
        for raw_name in source.keys():
            name = str(raw_name or "").strip()
            if name:
                state_node_names.add(name)

    inventory_by_name: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        name = str(node.get("name", "")).strip()
        if name:
            inventory_by_name[name] = dict(node)

    for name, raw in manual_nodes.items():
        node_name = str(name or "").strip()
        if not node_name:
            continue
        entry = raw if isinstance(raw, dict) else {}
        if node_name in inventory_by_name:
            inventory_by_name[node_name]["manual_node"] = True
            inventory_by_name[node_name]["node_username"] = str(entry.get("username", "")).strip()
            inventory_by_name[node_name]["default_shell"] = str(entry.get("default_shell", "")).strip()
            inventory_by_name[node_name]["certificate"] = str(entry.get("certificate", "")).strip()
            inventory_by_name[node_name]["password_set"] = bool(str(entry.get("password", "")))
            if _is_placeholder_inventory_address(str(inventory_by_name[node_name].get("address", ""))):
                inventory_by_name[node_name]["address"] = str(entry.get("address", "")).strip()
            continue
        inventory_by_name[node_name] = {
            "name": node_name,
            "group": str(entry.get("group", "manual")).strip() or "manual",
            "address": str(entry.get("address", "")).strip(),
            "manual_node": True,
            "node_username": str(entry.get("username", "")).strip(),
            "default_shell": str(entry.get("default_shell", "")).strip(),
            "certificate": str(entry.get("certificate", "")).strip(),
            "password_set": bool(str(entry.get("password", ""))),
        }

    # Callback-discovered nodes should appear even when not yet in inventory or
    # provisioning state so Node Ops can immediately act on fresh callbacks.
    for discovered_name, details in callback_details.items():
        node_name = str(discovered_name or "").strip()
        if not node_name or node_name in inventory_by_name:
            continue
        if "." in node_name and node_name.split(".", 1)[0] in callback_details:
            # Avoid duplicate rows when both fqdn and short hostname aliases exist.
            continue
        profile = str(details.get("profile", "")).strip().lower()
        if profile == "pi5":
            group = "rpi5_active"
        elif profile == "pibplus":
            group = "rpi_bplus_active"
        else:
            group = "callback"
        inventory_by_name[node_name] = {
            "name": node_name,
            "group": group,
            "address": str(details.get("source_ip", "")).strip(),
            "address_source": "callback",
        }

    # Provisioning state can reference nodes not yet present in inventory groups.
    for name in sorted(state_node_names):
        if name in inventory_by_name:
            continue
        inventory_by_name[name] = {
            "name": name,
            "group": "provisioned",
            "address": "",
        }

    enriched: List[Dict[str, Any]] = []
    for node_name in sorted(inventory_by_name):
        node = inventory_by_name[node_name]
        item = dict(node)
        callback = {}
        for alias in _node_aliases(str(node.get("name", ""))):
            callback = callback_details.get(alias, {})
            if callback:
                break

        has_provision_state = node_name in state_node_names
        has_callback_state = bool(callback)
        if not has_provision_state and not has_callback_state:
            continue

        callback_ip = str(callback.get("source_ip", "")).strip()
        inventory_addr = str(node.get("address", "")).strip()

        if str(node.get("address_source", "")).strip() == "callback":
            item["address"] = inventory_addr or callback_ip
            item["address_source"] = "callback"
        elif _is_usable_callback_ip(callback_ip) and _is_placeholder_inventory_address(inventory_addr):
            item["address"] = callback_ip
            item["address_source"] = "callback"
        else:
            item["address"] = inventory_addr
            item["address_source"] = "inventory"

        item["last_callback"] = str(callback.get("last_callback", "")).strip()
        item["callback_profile"] = str(callback.get("profile", "")).strip()
        item["callback_raw_profile"] = str(callback.get("raw_profile", "")).strip()
        item["callback_profile_source"] = str(callback.get("profile_source", "")).strip()
        item["callback_stage"] = str(callback.get("stage", "")).strip()
        item["callback_instance_id"] = str(callback.get("instance_id", "")).strip()
        item["callback_source_ip"] = str(callback.get("source_ip", "")).strip()
        item["callback_source_ip_source"] = str(callback.get("source_ip_source", "")).strip()
        item["callback_cert_present"] = bool(callback.get("cert_present", False))
        item["callback_cert_subject"] = str(callback.get("cert_subject", "")).strip()
        item["callback_cert_issuer"] = str(callback.get("cert_issuer", "")).strip()
        item["callback_cert_fingerprint"] = str(callback.get("cert_fingerprint", "")).strip()
        item["callback_node_report_id"] = str(callback.get("node_report_id", "")).strip()
        item["callback_node_report_present"] = bool(callback.get("node_report_present", False))
        item["manual_node"] = bool(item.get("manual_node", False))
        enriched.append(item)
    return enriched


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


def _ensure_ssh_key_permissions(private_key: Path, public_key: Path) -> None:
    try:
        os.chmod(private_key, 0o600)
    except (FileNotFoundError, PermissionError):
        pass
    try:
        os.chmod(public_key, 0o644)
    except (FileNotFoundError, PermissionError):
        pass


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

    _ensure_ssh_key_permissions(private_key, public_key)

    pub = public_key.read_text(encoding="utf-8").strip()
    return {
        "created": created,
        "mode": mode,
        "key_name": key_name,
        "public_key": pub,
        "private_key_path": str(private_key),
        "public_key_path": str(public_key),
    }


def _list_ssh_keys() -> List[Dict[str, Any]]:
    _ensure_keys_dir()
    records: List[Dict[str, Any]] = []
    for private_key in sorted(KEYS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not private_key.is_file() or private_key.name.endswith(".pub"):
            continue
        public_key = KEYS_DIR / f"{private_key.name}.pub"
        records.append(
            {
                "key_name": private_key.name,
                "private_key_exists": True,
                "public_key_exists": public_key.exists(),
            }
        )
    return records


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
    full_url = os.getenv("CALLBACK_PUBLIC_URL", "").strip()
    if full_url:
        parsed = urlparse.urlparse(full_url)
        if parsed.scheme and parsed.netloc:
            return parsed._replace(path="/api/cloud-init/callback", params="", query="", fragment="").geturl()

    host = os.getenv("CALLBACK_PUBLIC_HOST", "").strip()
    if not host:
        host = _detect_local_ip()

    if "://" in host:
        parsed = urlparse.urlparse(host)
        if parsed.hostname:
            host = parsed.hostname or host
            supplied_port = parsed.port
            if supplied_port:
                port = str(supplied_port)
            else:
                port = os.getenv("CALLBACK_PUBLIC_PORT", "").strip() or os.getenv("PORT", "8088").strip() or "8088"
            if host and ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"http://{host}:{port}/api/cloud-init/callback"

    port = (
        os.getenv("CALLBACK_PUBLIC_PORT", "").strip()
        or os.getenv("PORT", "8088").strip()
        or "8088"
    )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/api/cloud-init/callback"


def _first_forwarded_ip(value: str) -> str:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts[0] if parts else ""


def _default_k3s_config() -> str:
    host = os.getenv("K3S_SERVER_HOST", "").strip()
    if not host:
        host = _detect_local_ip()
    node_name = socket.gethostname().strip() or "rack-node"
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", node_name):
        node_name = "rack-node"
    return f"# pragma: k3s-config-v1\nserver: https://{host}:6443\nnode-name: {node_name}\n"


def _normalize_ip(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ""


def _is_transit_hop_ip(ip: str) -> bool:
    parsed = _normalize_ip(ip)
    if not parsed:
        return False
    try:
        addr = ipaddress.ip_address(parsed)
    except ValueError:
        return False

    # Common non-LAN transit ranges observed behind VPN/proxy edges.
    return (
        addr in ipaddress.ip_network("10.255.0.0/16")
        or addr in ipaddress.ip_network("100.64.0.0/10")
    )


def _is_usable_callback_ip(ip: str) -> bool:
    parsed = _normalize_ip(ip)
    if not parsed:
        return False
    try:
        addr = ipaddress.ip_address(parsed)
    except ValueError:
        return False
    return addr.is_private and not _is_transit_hop_ip(parsed) and not addr.is_loopback and not addr.is_link_local


def _resolve_lan_ip_from_names(*names: str) -> tuple[str, str]:
    seen: set[str] = set()
    for name in names:
        target = str(name or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        try:
            infos = socket.getaddrinfo(target, None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            continue

        for info in infos:
            sockaddr = info[4] if len(info) > 4 else ()
            candidate = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else ""
            normalized = _normalize_ip(candidate)
            if not normalized:
                continue
            try:
                addr = ipaddress.ip_address(normalized)
            except ValueError:
                continue
            if addr.is_private and not addr.is_loopback and not addr.is_link_local:
                return normalized, f"dns:{target}"
    return "", ""


def _resolve_source_ip(payload: Dict[str, Any], hostname: str = "", fqdn: str = "") -> tuple[str, str]:
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        ip = _normalize_ip(_first_forwarded_ip(forwarded_for))
        if ip:
            return ip, "x-forwarded-for"

    for header in ("X-Real-IP", "CF-Connecting-IP", "True-Client-IP"):
        value = _normalize_ip(request.headers.get(header, ""))
        if value:
            return value, header.lower()

    for key in ("source_ip", "client_ip", "ip", "remote_addr"):
        value = _normalize_ip(str(payload.get(key, "")).strip())
        if value:
            return value, f"payload:{key}"

    remote = _normalize_ip(request.remote_addr or "")
    if remote and _is_transit_hop_ip(remote):
        return remote, "remote_addr_transit"

    return remote, "remote_addr"


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
        "distribution",
        "hostname",
        "device",
        "ssh_public_key",
        "callback_url",
        "image_url",
        "image_file",
        "image_sha256",
        "workspace",
        "sd_options_profile",
        "cloud_init_options",
    }
    cleaned: Dict[str, Any] = {}
    for key in allowed_keys:
        if key in payload:
            cleaned[key] = payload.get(key)
    if not isinstance(cleaned.get("cloud_init_options"), dict):
        cleaned["cloud_init_options"] = {}
    cleaned["sd_options_profile"] = str(cleaned.get("sd_options_profile", "")).strip()
    cloud_options = cleaned["cloud_init_options"]
    distribution = str(cleaned.get("distribution", "standard")).strip().lower() or "standard"
    k3s_config = str(cloud_options.get("k3s_config", "") or "").strip()
    if distribution != "k3s" and k3s_config:
        # If the UI sent an explicit k3s config block, treat this as k3s intent
        # even if the distribution select was left on standard.
        distribution = "k3s"
    if distribution == "k3s":
        lines = []
        seen_node_name = False
        for raw_line in k3s_config.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if re.match(r"(?i)^node-name\s*:\s*.*$", stripped):
                if not seen_node_name:
                    lines.append(f"node-name: {str(cleaned.get('hostname', '') or socket.gethostname() or 'rack-node').strip()}")
                    seen_node_name = True
                continue
            if re.match(r"(?i)^node-ip\s*:\s*.*$", stripped):
                continue
            if stripped:
                lines.append(line)
        if not lines:
            k3s_config = _default_k3s_config().strip()
        else:
            k3s_config = "\n".join(lines).strip() + "\n"
        cloud_options["k3s_config"] = k3s_config
    cleaned["distribution"] = distribution
    if distribution == "k3s" and not cloud_options.get("k3s_config"):
        cloud_options["k3s_config"] = _default_k3s_config().strip()
    return cleaned


def _service_is_active(service_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return False
    return proc.returncode == 0 and str(proc.stdout or "").strip() == "active"


def _detect_kubernetes_runtime(force_refresh: bool = False) -> Dict[str, Any]:
    now = time.time()
    with KUBERNETES_STATUS_LOCK:
        cached_at = float(KUBERNETES_STATUS_CACHE.get("checked_at", 0.0) or 0.0)
        if not force_refresh and now - cached_at < 8.0:
            cached = KUBERNETES_STATUS_CACHE.get("state", {})
            return dict(cached) if isinstance(cached, dict) else {
                "available": False,
                "active_service": "",
                "k3s_binary": "",
                "has_kubeconfig": False,
                "reason": "invalid cache",
            }

    k3s_binary = shutil.which("k3s") or ""
    if not k3s_binary:
        candidate = Path("/usr/local/bin/k3s")
        if candidate.exists() and candidate.is_file():
            k3s_binary = str(candidate)

    has_kubeconfig = Path("/etc/rancher/k3s/k3s.yaml").exists()
    active_service = ""
    if _service_is_active("k3s"):
        active_service = "k3s"
    elif _service_is_active("k3s-agent"):
        active_service = "k3s-agent"

    reason = ""
    if active_service:
        reason = f"{active_service} service is active"
    elif k3s_binary:
        reason = "k3s binary found but service is inactive"
    else:
        reason = "k3s service and binary not found"

    state = {
        "available": bool(active_service),
        "active_service": active_service,
        "k3s_binary": k3s_binary,
        "has_kubeconfig": bool(has_kubeconfig),
        "reason": reason,
    }

    with KUBERNETES_STATUS_LOCK:
        KUBERNETES_STATUS_CACHE["checked_at"] = now
        KUBERNETES_STATUS_CACHE["state"] = state

    return dict(state)


def _run_kubectl_json(args: List[str]) -> tuple[Any | None, str]:
    runtime = _detect_kubernetes_runtime(force_refresh=False)
    k3s_binary = str(runtime.get("k3s_binary", "") or "")
    kubeconfig = "/etc/rancher/k3s/k3s.yaml"
    attempts: List[List[str]] = []

    if k3s_binary:
        attempts.append([k3s_binary, "kubectl", *args, "-o", "json"])
    kubectl_binary = shutil.which("kubectl") or ""
    if kubectl_binary:
        attempts.append([kubectl_binary, "--kubeconfig", kubeconfig, *args, "-o", "json"])
    if os.geteuid() != 0 and k3s_binary:
        attempts.append(["sudo", "-n", k3s_binary, "kubectl", *args, "-o", "json"])

    if not attempts:
        return None, "No kubectl command available"

    errors: List[str] = []
    for cmd in attempts:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
        except Exception as exc:
            errors.append(f"{' '.join(cmd[:2])}: {exc}")
            continue

        if proc.returncode != 0:
            stderr = str(proc.stderr or proc.stdout or "").strip()
            errors.append(stderr or f"command failed ({proc.returncode})")
            continue

        try:
            return json.loads(proc.stdout or "{}"), ""
        except json.JSONDecodeError:
            errors.append("kubectl returned non-JSON output")

    return None, " | ".join(e for e in errors if e)[:1000] or "kubectl command failed"


def _kubernetes_local_summary() -> Dict[str, Any]:
    runtime = _detect_kubernetes_runtime(force_refresh=False)
    summary: Dict[str, Any] = {
        "ok": True,
        "available": bool(runtime.get("available", False)),
        "runtime": runtime,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "nodes_total": 0,
        "nodes_ready": 0,
        "control_plane_nodes": 0,
        "pods_total": 0,
        "pods_running": 0,
        "namespaces_total": 0,
        "jobs_total": 0,
        "jobs_running": 0,
        "node_rows": [],
        "pod_rows": [],
        "namespace_rows": [],
        "job_rows": [],
        "last_error": "",
    }

    if not summary["available"]:
        return summary

    nodes_doc, nodes_err = _run_kubectl_json(["get", "nodes"])
    pods_doc, pods_err = _run_kubectl_json(["get", "pods", "-A"])
    ns_doc, ns_err = _run_kubectl_json(["get", "namespaces"])
    jobs_doc, jobs_err = _run_kubectl_json(["get", "jobs", "-A"])

    if nodes_doc is None:
        summary["last_error"] = nodes_err
        return summary

    node_items = nodes_doc.get("items", []) if isinstance(nodes_doc, dict) else []
    if not isinstance(node_items, list):
        node_items = []
    summary["nodes_total"] = len(node_items)

    nodes_ready = 0
    control_plane_nodes = 0
    for item in node_items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
        labels = metadata.get("labels", {}) if isinstance(metadata.get("labels"), dict) else {}
        if "node-role.kubernetes.io/control-plane" in labels or str(labels.get("role", "")) == "control-plane":
            control_plane_nodes += 1

        status = item.get("status", {}) if isinstance(item.get("status"), dict) else {}
        conditions = status.get("conditions", []) if isinstance(status.get("conditions"), list) else []
        is_ready = any(
            isinstance(cond, dict)
            and str(cond.get("type", "")) == "Ready"
            and str(cond.get("status", "")) == "True"
            for cond in conditions
        )
        if is_ready:
            nodes_ready += 1

    summary["nodes_ready"] = nodes_ready
    summary["control_plane_nodes"] = control_plane_nodes

    node_rows: List[Dict[str, Any]] = []
    for item in node_items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
        labels = metadata.get("labels", {}) if isinstance(metadata.get("labels"), dict) else {}
        status = item.get("status", {}) if isinstance(item.get("status"), dict) else {}
        conditions = status.get("conditions", []) if isinstance(status.get("conditions"), list) else []
        is_ready = any(
            isinstance(cond, dict)
            and str(cond.get("type", "")) == "Ready"
            and str(cond.get("status", "")) == "True"
            for cond in conditions
        )
        addresses = status.get("addresses", []) if isinstance(status.get("addresses"), list) else []
        internal_ip = ""
        external_ip = ""
        for address in addresses:
            if not isinstance(address, dict):
                continue
            address_type = str(address.get("type", ""))
            address_value = str(address.get("address", ""))
            if address_type == "InternalIP" and not internal_ip:
                internal_ip = address_value
            elif address_type == "ExternalIP" and not external_ip:
                external_ip = address_value
        node_rows.append(
            {
                "name": str(metadata.get("name", "")),
                "ready": is_ready,
                "role": "control-plane" if "node-role.kubernetes.io/control-plane" in labels or str(labels.get("role", "")) == "control-plane" else "worker",
                "internal_ip": internal_ip,
                "external_ip": external_ip,
                "os_image": str(status.get("nodeInfo", {}).get("osImage", "")) if isinstance(status.get("nodeInfo"), dict) else "",
                "kernel": str(status.get("nodeInfo", {}).get("kernelVersion", "")) if isinstance(status.get("nodeInfo"), dict) else "",
                "container_runtime": str(status.get("nodeInfo", {}).get("containerRuntimeVersion", "")) if isinstance(status.get("nodeInfo"), dict) else "",
                "labels": ", ".join(sorted(str(k) for k in labels.keys())[:6]),
            }
        )
    summary["node_rows"] = node_rows

    if isinstance(pods_doc, dict):
        pod_items = pods_doc.get("items", []) if isinstance(pods_doc.get("items"), list) else []
        summary["pods_total"] = len(pod_items)
        pod_rows: List[Dict[str, Any]] = []
        pods_running = 0
        for pod in pod_items:
            if not isinstance(pod, dict):
                continue
            metadata = pod.get("metadata", {}) if isinstance(pod.get("metadata"), dict) else {}
            status = pod.get("status", {}) if isinstance(pod.get("status"), dict) else {}
            phase = str(status.get("phase", ""))
            if phase == "Running":
                pods_running += 1
            container_statuses = status.get("containerStatuses", []) if isinstance(status.get("containerStatuses"), list) else []
            ready_count = 0
            restart_count = 0
            for container_status in container_statuses:
                if not isinstance(container_status, dict):
                    continue
                if container_status.get("ready") is True:
                    ready_count += 1
                try:
                    restart_count += int(container_status.get("restartCount", 0) or 0)
                except (TypeError, ValueError):
                    continue
            pod_rows.append(
                {
                    "namespace": str(metadata.get("namespace", "")),
                    "name": str(metadata.get("name", "")),
                    "phase": phase,
                    "node": str(status.get("nodeName", "")),
                    "ready_containers": ready_count,
                    "restart_count": restart_count,
                    "created_at": str(metadata.get("creationTimestamp", "")),
                }
            )
        summary["pods_running"] = pods_running
        summary["pod_rows"] = pod_rows[:60]

    if isinstance(ns_doc, dict):
        ns_items = ns_doc.get("items", []) if isinstance(ns_doc.get("items"), list) else []
        summary["namespaces_total"] = len(ns_items)
        namespace_rows: List[Dict[str, Any]] = []
        pod_namespace_counts: Dict[str, Dict[str, int]] = {}
        for pod_row in summary.get("pod_rows", []):
            if not isinstance(pod_row, dict):
                continue
            namespace = str(pod_row.get("namespace", ""))
            if not namespace:
                continue
            counts = pod_namespace_counts.setdefault(namespace, {"total": 0, "running": 0})
            counts["total"] += 1
            if str(pod_row.get("phase", "")) == "Running":
                counts["running"] += 1
        for ns in ns_items:
            if not isinstance(ns, dict):
                continue
            metadata = ns.get("metadata", {}) if isinstance(ns.get("metadata"), dict) else {}
            status = ns.get("status", {}) if isinstance(ns.get("status"), dict) else {}
            name = str(metadata.get("name", ""))
            counts = pod_namespace_counts.get(name, {"total": 0, "running": 0})
            namespace_rows.append(
                {
                    "name": name,
                    "phase": str(status.get("phase", "")),
                    "total_pods": counts.get("total", 0),
                    "running_pods": counts.get("running", 0),
                }
            )
        summary["namespace_rows"] = namespace_rows

    if isinstance(jobs_doc, dict):
        job_items = jobs_doc.get("items", []) if isinstance(jobs_doc.get("items"), list) else []
        summary["jobs_total"] = len(job_items)
        job_rows: List[Dict[str, Any]] = []
        jobs_running = 0
        for job in job_items:
            if not isinstance(job, dict):
                continue
            metadata = job.get("metadata", {}) if isinstance(job.get("metadata"), dict) else {}
            status = job.get("status", {}) if isinstance(job.get("status"), dict) else {}
            active = 0
            succeeded = 0
            failed = 0
            try:
                active = int(status.get("active", 0) or 0)
            except (TypeError, ValueError):
                active = 0
            try:
                succeeded = int(status.get("succeeded", 0) or 0)
            except (TypeError, ValueError):
                succeeded = 0
            try:
                failed = int(status.get("failed", 0) or 0)
            except (TypeError, ValueError):
                failed = 0
            if active > 0:
                jobs_running += 1
            conditions = status.get("conditions", []) if isinstance(status.get("conditions"), list) else []
            if conditions:
                last_condition = conditions[-1]
                if isinstance(last_condition, dict):
                    condition_status = str(last_condition.get("type", "")).strip()
                    if condition_status == "Complete":
                        status_text = "Succeeded"
                    elif condition_status == "Failed":
                        status_text = "Failed"
                    elif active > 0:
                        status_text = "Running"
                    else:
                        status_text = condition_status or "Unknown"
                else:
                    status_text = "Unknown"
            elif active > 0:
                status_text = "Running"
            elif succeeded > 0:
                status_text = "Succeeded"
            elif failed > 0:
                status_text = "Failed"
            else:
                status_text = "Pending"
            job_rows.append(
                {
                    "namespace": str(metadata.get("namespace", "default")),
                    "name": str(metadata.get("name", "")),
                    "status": status_text,
                    "active": active,
                    "succeeded": succeeded,
                    "failed": failed,
                }
            )
        summary["jobs_running"] = jobs_running
        summary["job_rows"] = job_rows[:50]

    if not summary["last_error"] and (pods_err or ns_err or jobs_err):
        summary["last_error"] = pods_err or ns_err or jobs_err

    return summary


def _run_prepare_job(job_id: str, form: Dict[str, Any]) -> None:
    hostname = form.get("hostname", "unknown")
    profile = form.get("profile", "unknown")
    device = form.get("device", "unknown")
    logger.info(f"SD preparation job started: job_id={job_id}, hostname={hostname}, profile={profile}, device={device}")
    
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
            logger.error(f"SD preparation requires root privileges: job_id={job_id}, hostname={hostname}")
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

        # Build environment with cloud-init options
        env = os.environ.copy()
        distribution = str(form.get("distribution", "standard")).strip().lower() or "standard"
        env["CLOUD_INIT_DISTRIBUTION"] = distribution

        cloud_init_options = form.get("cloud_init_options", {})
        if isinstance(cloud_init_options, dict):
            # Map cloud-init options to environment variables
            enabled_options = []
            bool_env_fields = {
                "disable_ssh_password_auth": "CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH",
                "disable_root_ssh": "CLOUD_INIT_DISABLE_ROOT_SSH",
                "enable_fail2ban": "CLOUD_INIT_ENABLE_FAIL2BAN",
                "enable_unattended_upgrades": "CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES",
                "reboot_after_bootstrap": "CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP",
            }
            for option_key, env_key in bool_env_fields.items():
                if bool(cloud_init_options.get(option_key, False)):
                    env[env_key] = "true"
                    enabled_options.append(option_key)
            if "boot_summary" in cloud_init_options:
                env["CLOUD_INIT_BOOT_SUMMARY"] = "true" if bool(cloud_init_options.get("boot_summary", False)) else "false"
                enabled_options.append("boot_summary")
            if distribution == "k3s":
                k3s_config = str(cloud_init_options.get("k3s_config", "") or "").strip()
                if not k3s_config:
                    k3s_config = _default_k3s_config().strip()
                env["CLOUD_INIT_K3S_CONFIG"] = k3s_config
                enabled_options.append("k3s_config")
            if cloud_init_options.get("enable_hardware_checks"):
                env["CLOUD_INIT_ENABLE_HARDWARE_CHECKS"] = "true"
                enabled_options.append("hardware_checks")
            if cloud_init_options.get("fancy_motd"):
                env["CLOUD_INIT_FANCY_MOTD"] = "true"
                enabled_options.append("fancy_motd")
            if cloud_init_options.get("install_telegraf"):
                env["CLOUD_INIT_INSTALL_TELEGRAF"] = "true"
                enabled_options.append("install_telegraf")
            user_fields = {
                "username": "CLOUD_INIT_USERNAME",
                "password": "CLOUD_INIT_USER_PASSWORD",
            }
            for option_key, env_key in user_fields.items():
                value = str(cloud_init_options.get(option_key, "")).strip()
                if value:
                    env[env_key] = value
                    enabled_options.append(option_key)
            telegraf_fields = {
                "telegraf_influx_url": "CLOUD_INIT_TELEGRAF_INFLUX_URL",
                "telegraf_influx_token": "CLOUD_INIT_TELEGRAF_INFLUX_TOKEN",
                "telegraf_influx_org": "CLOUD_INIT_TELEGRAF_INFLUX_ORG",
                "telegraf_influx_bucket": "CLOUD_INIT_TELEGRAF_INFLUX_BUCKET",
                "telegraf_config_url": "CLOUD_INIT_TELEGRAF_CONFIG_URL",
            }
            for option_key, env_key in telegraf_fields.items():
                value = str(cloud_init_options.get(option_key, "")).strip()
                if value:
                    env[env_key] = value
                    enabled_options.append(option_key)
            ssh_scalar_fields = {
                "ssh_port": "CLOUD_INIT_SSH_PORT",
            }
            for option_key, env_key in ssh_scalar_fields.items():
                value = str(cloud_init_options.get(option_key, "")).strip()
                if value:
                    env[env_key] = value
                    enabled_options.append(option_key)

            ssh_list_fields = {
                "ssh_allow_groups": "CLOUD_INIT_SSH_ALLOW_GROUPS",
                "extra_ssh_keys": "CLOUD_INIT_EXTRA_SSH_KEYS",
            }
            for option_key, env_key in ssh_list_fields.items():
                raw_value = cloud_init_options.get(option_key)
                if isinstance(raw_value, list):
                    cleaned_items = [str(item).strip() for item in raw_value if str(item).strip()]
                    if cleaned_items:
                        env[env_key] = "\n".join(cleaned_items) if option_key == "extra_ssh_keys" else ",".join(cleaned_items)
                        enabled_options.append(option_key)
                else:
                    value = str(raw_value or "").strip()
                    if value:
                        env[env_key] = value
                        enabled_options.append(option_key)
            if enabled_options:
                logger.info(f"Cloud-init options enabled for {hostname}: {', '.join(enabled_options)}")

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["command"] = run_cmd
            JOBS[job_id]["stdout"] = (
                "Starting SD preparation.\n"
                f"Target device: {form.get('device', '')}\n"
                f"Cloud-init options profile: {form.get('sd_options_profile', '') or 'none'}\n"
                f"Archived SSH key: {archive_info.get('archive_path', '')}\n"
                "The selected device will be overwritten.\n"
            )

        proc = subprocess.Popen(
            run_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
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

        if proc.returncode == 0:
            logger.info(f"SD preparation completed successfully: job_id={job_id}, hostname={hostname}")
        else:
            logger.error(f"SD preparation failed: job_id={job_id}, hostname={hostname}, exit_code={proc.returncode}")

        _record_image_history(form, final_stdout, proc.returncode)


# Export both public and underscore-prefixed symbols for the thin route layer.
__all__ = [name for name in globals() if not name.startswith("__")]


