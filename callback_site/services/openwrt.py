"""OpenWrt router probing, key push and inventory preview."""

from __future__ import annotations

import ipaddress
import os
import re
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


# Read-only and fixed: nothing from the request reaches the router's shell. Runs under BusyBox ash.
UCI_SUMMARY_SCRIPT = r"""
. /etc/openwrt_release 2>/dev/null
echo "release=${DISTRIB_DESCRIPTION:-}"
echo "board=$(cat /tmp/sysinfo/model 2>/dev/null)"
for k in system.@system[0].hostname system.@system[0].zonename \
  network.lan.proto network.lan.ipaddr network.lan.netmask \
  network.wan.proto network.wan.ipaddr dhcp.lan.ignore dhcp.lan.start dhcp.lan.limit dhcp.lan.leasetime; do
  echo "$k=$(uci -q get "$k")"
done
uci -q show wireless | grep -E '^wireless\.[][A-Za-z0-9_@-]+\.(ssid|mode|network|disabled|band|channel|device)='
exit 0
"""

# Only these keys are returned; anything else the router prints (keys, passwords) is dropped.
_UCI_FIXED_KEYS = (
    "release", "board",
    "system.@system[0].hostname", "system.@system[0].zonename",
    "network.lan.proto", "network.lan.ipaddr", "network.lan.netmask",
    "network.wan.proto", "network.wan.ipaddr",
    "dhcp.lan.ignore", "dhcp.lan.start", "dhcp.lan.limit", "dhcp.lan.leasetime",
)
_UCI_WIRELESS_KEY = re.compile(r"wireless\.[A-Za-z0-9_@\[\]-]{1,64}\.(ssid|mode|network|disabled|band|channel|device)")
UCI_SSH_TIMEOUT_SECONDS = 20


def parse_uci_summary(text: str) -> list[Dict[str, str]]:
    found: list[Dict[str, str]] = []
    for line in str(text or "").splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not (key in _UCI_FIXED_KEYS or _UCI_WIRELESS_KEY.fullmatch(key)):
            continue
        value = value.strip().strip("'")[:200]
        if value:
            found.append({"key": key, "value": value})
    return found


_WIFI_BANDS = {"2g": "2.4 GHz", "5g": "5 GHz", "6g": "6 GHz", "60g": "60 GHz"}


def _dhcp_pool(lan_ip: str, netmask: str, start: str, limit: str) -> str:
    try:
        network = ipaddress.IPv4Network(f"{lan_ip}/{netmask}", strict=False)
        first = int(network.network_address) + int(start)
        last = first + int(limit) - 1
    except (ValueError, TypeError):
        return ""
    last = min(last, int(network.broadcast_address) - 1)
    if first > last:
        return ""
    return f"{ipaddress.IPv4Address(first)} – {ipaddress.IPv4Address(last)}"


def organize_uci_facts(facts: list[Dict[str, str]]) -> list[Dict[str, Any]]:
    """Group raw allowlisted facts into titled sections of labelled rows for display."""
    raw = {fact["key"]: fact["value"] for fact in facts}

    def section(title: str, rows: list[tuple[str, str]]) -> Dict[str, Any] | None:
        kept = [{"label": label, "value": value} for label, value in rows if value]
        return {"title": title, "rows": kept} if kept else None

    def address(prefix: str) -> str:
        ip, mask = raw.get(f"{prefix}.ipaddr", ""), raw.get(f"{prefix}.netmask", "")
        if ip and mask:
            try:
                return f"{ip}/{ipaddress.IPv4Network(f'0.0.0.0/{mask}').prefixlen}"
            except ValueError:
                pass
        return ip

    dhcp_off = raw.get("dhcp.lan.ignore") == "1"
    pool = "" if dhcp_off else _dhcp_pool(
        raw.get("network.lan.ipaddr", ""), raw.get("network.lan.netmask", "255.255.255.0"),
        raw.get("dhcp.lan.start", ""), raw.get("dhcp.lan.limit", ""),
    )
    sections = [
        section("System", [
            ("Firmware", raw.get("release", "")),
            ("Board", raw.get("board", "")),
            ("Hostname", raw.get("system.@system[0].hostname", "")),
            ("Timezone", raw.get("system.@system[0].zonename", "")),
        ]),
        section("LAN", [("Protocol", raw.get("network.lan.proto", "")), ("Address", address("network.lan"))]),
        section("WAN", [("Protocol", raw.get("network.wan.proto", "")), ("Address", address("network.wan"))]),
        section("DHCP (LAN)", [
            ("Server", "off" if dhcp_off else ("on" if "dhcp.lan.start" in raw else "")),
            ("Pool", pool),
            ("Leases", "" if dhcp_off else raw.get("dhcp.lan.limit", "")),
            ("Lease time", "" if dhcp_off else raw.get("dhcp.lan.leasetime", "")),
        ]),
    ]

    wifi: Dict[str, Dict[str, str]] = {}
    for key, value in raw.items():
        if key.startswith("wireless."):
            name, _, option = key[len("wireless."):].rpartition(".")
            wifi.setdefault(name, {})[option] = value
    radios = [name for name, opts in wifi.items() if "band" in opts or "channel" in opts]
    interfaces = [name for name in wifi if name not in radios]
    for radio in radios:
        opts = wifi[radio]
        band = _WIFI_BANDS.get(opts.get("band", ""), opts.get("band", ""))
        rows = [
            ("Channel", opts.get("channel", "")),
            ("Radio", "disabled" if opts.get("disabled") == "1" else "enabled"),
        ]
        for iface in interfaces:
            iopts = wifi[iface]
            if iopts.get("device", radio) != radio and iopts.get("device"):
                continue
            if not iopts.get("device") and iface != f"default_{radio}":
                continue
            state = " (disabled)" if iopts.get("disabled") == "1" else ""
            detail = ", ".join(v for v in (iopts.get("mode", ""), iopts.get("network", "")) if v)
            rows.append(("SSID", f"{iopts.get('ssid', iface)}{f' · {detail}' if detail else ''}{state}"))
        sections.append(section(f"Wi-Fi {band} ({radio})" if band else f"Wi-Fi {radio}", rows))
    return [item for item in sections if item]


def read_openwrt_uci(node_name: str, port: int = 22) -> Dict[str, Any]:
    """Basic UCI facts from an OpenWrt node over key-based SSH; never uses or stores a password."""
    from services.network import _preferred_node_connect_target
    from services.node_inventory import _node_ops_inventory_view

    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    entry = next(
        (item for item in _node_ops_inventory_view() if isinstance(item, dict) and str(item.get("name", "")).strip() == node_name),
        None,
    )
    if not entry:
        raise ValueError("unknown node")
    if str(entry.get("group", "")).strip().lower() != "openwrt":
        raise ValueError("UCI info is only read from nodes in group openwrt")
    callback_ip = str(entry.get("callback_connect_ip", "") or entry.get("callback_source_ip", "") or "").strip()
    host, _source = _preferred_node_connect_target(node_name, str(entry.get("address", "") or "").strip(), callback_ip)
    if not host:
        raise ValueError("no address known for this router yet")
    user = str(entry.get("node_username", "") or "").strip() or "root"
    destination = _ssh_destination(user, host)
    key = _resolve_openwrt_ssh_key(str(entry.get("certificate", "") or "").strip() or "rack-bootstrap-ed25519")
    preflight = _ssh_private_key_preflight(key["private_key_path"])
    result: Dict[str, Any] = {"node": node_name, "target": f"{destination}:{port}", "facts": [], "sections": [], "error": ""}
    if preflight:
        result["error"] = preflight
        return result

    _seed_known_host(host, port)
    cmd = [
        "ssh", "-i", key["private_key_path"],
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS_FILE}",
        "-o", "ConnectTimeout=6",
        "-p", str(port),
        "--", destination, UCI_SUMMARY_SCRIPT,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=UCI_SSH_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError:
        result["error"] = "ssh is not installed or available on this host"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = f"Timed out after {UCI_SSH_TIMEOUT_SECONDS}s reading the router."
        return result
    result["facts"] = parse_uci_summary(proc.stdout)
    result["sections"] = organize_uci_facts(result["facts"])
    if proc.returncode != 0 and not result["facts"]:
        result["error"] = (proc.stderr or "").strip()[-400:] or f"ssh exited with code {proc.returncode}"
    return result
