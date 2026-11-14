"""OpenWrt router probing, key push and inventory preview."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict

from services.ssh_keys import (
    _ensure_keys_dir,
    _is_safe_key_name,
    _seed_known_host,
    _ssh_destination,
    _ssh_private_key_preflight,
)
from services.storage import KEYS_DIR, KNOWN_HOSTS_FILE


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
    _ssh_destination(username, host)
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
    if not _is_safe_key_name(name):
        raise ValueError("invalid ssh key name")

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
    destination = _ssh_destination(username, host)
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
    cmd.extend(["--", destination, "true"])

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
    target = _ssh_destination(username, host)
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

    cmd = ["ssh", *ssh_opts, "--", target, "sh", "-c", remote_script]
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
        lines.append('          ansible_password: "<redacted>"')
    if private_key_path:
        lines.append(f"          ansible_ssh_private_key_file: {private_key_path}")
    if become:
        lines.append("          ansible_become: true")
        lines.append("          ansible_become_method: sudo")

    lines.append(f"          ansible_module: {module_name}")
    return "\n".join(lines)
