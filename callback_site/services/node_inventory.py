"""Inventory loading, reachability and callback-derived facts per node."""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List

from k3s_lightweight import BOARDS, group_for_profile as _k3s_group_for_profile, NODE_GROUPS as K3S_NODE_GROUPS
from network_toolkit import GROUP as TOOLKIT_GROUP, PROFILE as TOOLKIT_PROFILE
from playbook_server import GROUP as SERVER_GROUP, PROFILE as SERVER_PROFILE

from services.events import _parse_event_time, _read_events
from services.node_archive import active_nodes
from services.network import _is_usable_callback_ip, _preferred_connect_ip_from_history
from services.node_state import _load_node_provisioning, _node_aliases
from services.storage import BASE_DIR, DATA_FILE, INVENTORY_FILE


def _node_group_options() -> List[Dict[str, str]]:
    """Groups a node can be assigned to: the module families, the built-in manual and openwrt groups, then the rest of the inventory."""
    boards = {board["group"]: board["label"] for board in BOARDS.values()}
    options = [{"name": group, "description": f"{boards.get(group, group)} nodes, modules available"} for group in K3S_NODE_GROUPS]
    options.append({"name": TOOLKIT_GROUP, "description": "Pi 3B+ Network Toolkit appliance, no Kubernetes"})
    options.append({"name": SERVER_GROUP, "description": "Ubuntu server running this dashboard under Docker"})
    options.append({"name": "manual", "description": "no modules, listed in Node Ops only"})
    options.append({"name": "openwrt", "description": "routers, listed in the OpenWrt panel"})

    try:
        # The app often runs without the venv on PATH, like the ansible-playbook lookup in node_runs.
        venv_inventory = BASE_DIR.parent / ".venv" / "bin" / "ansible-inventory"
        command = shutil.which("ansible-inventory") or (str(venv_inventory) if venv_inventory.is_file() else "ansible-inventory")
        result = subprocess.run([command, "-i", str(INVENTORY_FILE), "--list"], check=True, capture_output=True, text=True)
        inventory = json.loads(result.stdout)
    except Exception:
        inventory = {}
    seen = {option["name"] for option in options}
    for name in sorted(inventory):
        group = inventory.get(name)
        if name in seen or name in {"_meta", "all", "ungrouped"} or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            continue
        if isinstance(group, dict) and (group.get("hosts") or group.get("children")):
            options.append({"name": name, "description": "inventory group"})
    return options


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
    groups = [*K3S_NODE_GROUPS, TOOLKIT_GROUP, SERVER_GROUP]
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
    target_groups = {*K3S_NODE_GROUPS, TOOLKIT_GROUP, SERVER_GROUP}

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


def _ping_status(address: str, port: int = 22) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "1", address],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        ping_error = "ping command not found"
    else:
        if proc.returncode == 0:
            match = re.search(r"time=([0-9.]+)\s*ms", proc.stdout or "")
            return {
                "reachable": True,
                "latency_ms": float(match.group(1)) if match else None,
                "error": "",
                "probe": "icmp",
            }
        ping_error = (proc.stderr or proc.stdout).strip() or "ping failed"

    # Docker Desktop and router firewalls can block ICMP while SSH still works.
    started = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=1):
            latency = round((time.monotonic() - started) * 1000, 3)
    except OSError as exc:
        return {
            "reachable": False,
            "latency_ms": None,
            "error": f"{ping_error}; TCP port {port}: {exc}",
            "probe": "icmp+tcp",
        }
    return {"reachable": True, "latency_ms": latency, "error": "", "probe": "tcp"}


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


def _first_callback_by_host() -> Dict[str, str]:
    seen: Dict[str, str] = {}
    if not DATA_FILE.exists():
        return seen

    try:
        lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return seen

    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
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
    first_seen: Dict[str, str] = _first_callback_by_host()
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
            "architecture": str(hw_block.get("architecture") or "").strip(),
            "ram_mb": hw_block.get("ram_mb"),
            "sd_capacity_gb": hw_block.get("sd_capacity_gb"),
            "nvme": hw_block.get("nvme"),
            "nvme_capacity_gb": hw_block.get("nvme_capacity_gb"),
            "hat": hw_block.get("hat"),
            "poe": hw_block.get("poe"),
            "node_report_id": str(event.get("node_report_id", "")).strip(),
            "source_ip": str(event.get("source_ip", "")).strip(),
        }
        for host in candidates:
            if not host:
                continue
            host_payload = dict(payload)
            host_payload["first_callback"] = str(first_seen.get(host, "")).strip()
            current_ts = seen_ts.get(host)
            if current_ts is None or event_ts is None or event_ts > current_ts:
                seen[host] = host_payload
                if event_ts is not None:
                    seen_ts[host] = event_ts
    return seen


def _latest_callback_details_by_host(limit: int = 2000) -> Dict[str, Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    seen_ts: Dict[str, datetime] = {}
    seen_ips: Dict[str, List[str]] = {}
    for event in _read_events(limit=limit):
        candidates = set()
        for raw in (event.get("hostname"), event.get("fqdn")):
            candidates.update(_node_aliases(str(raw or "")))
        if not candidates:
            continue
        source_ip = str(event.get("source_ip", "")).strip()
        event_ts = _parse_event_time(str(event.get("received_at", "")))
        payload: Dict[str, Any] = {
            "last_callback": str(event.get("received_at", "")).strip(),
            "source_ip": source_ip,
            "source_ips": [],
            "source_ip_source": str(event.get("source_ip_source", "")).strip(),
            "profile": str(event.get("profile", "")).strip(),
            "raw_profile": str(event.get("raw_profile", "")).strip(),
            "profile_source": str(event.get("profile_source", "")).strip(),
            "stage": str(event.get("stage", "")).strip(),
            "instance_id": str(event.get("instance_id", "")).strip(),
            "fqdn": str(event.get("fqdn", "")).strip(),
            "cert_present": bool(event.get("cert_present", False)),
            "cert_subject": str(event.get("cert_subject", "")).strip(),
            "cert_issuer": str(event.get("cert_issuer", "")).strip(),
            "cert_fingerprint": str(event.get("cert_fingerprint", "")).strip(),
            "node_report_id": str(event.get("node_report_id", "")).strip(),
            "node_report_present": bool(event.get("node_report_present", False)),
        }
        for host in candidates:
            if not host:
                continue
            if source_ip:
                ip_list = seen_ips.setdefault(host, [])
                if source_ip not in ip_list:
                    ip_list.append(source_ip)

            current_ts = seen_ts.get(host)
            if host not in seen or current_ts is None or event_ts is None or event_ts > current_ts:
                seen[host] = payload
                if event_ts is not None:
                    seen_ts[host] = event_ts

    for host, details in seen.items():
        details["source_ips"] = list(seen_ips.get(host, []))
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
        group = {TOOLKIT_PROFILE: TOOLKIT_GROUP, SERVER_PROFILE: SERVER_GROUP}.get(profile) or _k3s_group_for_profile(profile, "callback")
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
        callback_source_ips = callback.get("source_ips", [])
        callback_ip_history = callback_source_ips if isinstance(callback_source_ips, list) else []
        callback_connect_ip = _preferred_connect_ip_from_history(callback_ip, callback_ip_history)
        inventory_addr = str(node.get("address", "")).strip()
        callback_target = callback_connect_ip or callback_ip
        callback_target_usable = bool(callback_target and _is_usable_callback_ip(callback_target))

        if str(node.get("address_source", "")).strip() == "callback":
            item["address"] = callback_target or inventory_addr or callback_ip
            item["address_source"] = "callback"
        elif callback_target_usable and callback_ip:
            item["address"] = callback_target
            item["address_source"] = "callback"
        else:
            item["address"] = inventory_addr or callback_target or callback_ip
            item["address_source"] = "inventory" if inventory_addr else ("callback" if callback_ip else "")

        item["last_callback"] = str(callback.get("last_callback", "")).strip()
        item["callback_profile"] = str(callback.get("profile", "")).strip()
        item["callback_raw_profile"] = str(callback.get("raw_profile", "")).strip()
        item["callback_profile_source"] = str(callback.get("profile_source", "")).strip()
        item["callback_stage"] = str(callback.get("stage", "")).strip()
        item["callback_instance_id"] = str(callback.get("instance_id", "")).strip()
        item["callback_fqdn"] = str(callback.get("fqdn", "")).strip()
        item["callback_source_ip"] = str(callback.get("source_ip", "")).strip()
        item["callback_source_ips"] = callback_source_ips if isinstance(callback_source_ips, list) else []
        item["callback_connect_ip"] = callback_connect_ip
        item["callback_source_ip_source"] = str(callback.get("source_ip_source", "")).strip()
        item["callback_cert_present"] = bool(callback.get("cert_present", False))
        item["callback_cert_subject"] = str(callback.get("cert_subject", "")).strip()
        item["callback_cert_issuer"] = str(callback.get("cert_issuer", "")).strip()
        item["callback_cert_fingerprint"] = str(callback.get("cert_fingerprint", "")).strip()
        item["callback_node_report_id"] = str(callback.get("node_report_id", "")).strip()
        item["callback_node_report_present"] = bool(callback.get("node_report_present", False))
        item["manual_node"] = bool(item.get("manual_node", False))
        enriched.append(item)
    return active_nodes(enriched)
