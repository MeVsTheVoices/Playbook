"""Named verifier checks and the test-terminal job runner."""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from services.events import _read_events
from services.network import _is_usable_callback_ip, _normalize_ip
from services.node_inventory import _node_ops_inventory_view
from services.node_state import _node_aliases


TEST_TERMINAL_JOBS: Dict[str, Dict[str, Any]] = {}


TEST_TERMINAL_LOCK = threading.Lock()


TERMINAL_CHECKS: Dict[str, Dict[str, str]] = {
    "identity": {
        "label": "Identity + Kernel",
        "category": "System",
        "description": "Validate hostname/user/kernel baseline",
        "command": "hostname && whoami && uname -a",
    },
    "cloud_init": {
        "label": "Cloud-Init State",
        "category": "Boot & logs",
        "description": "Check cloud-init completion and status",
        "command": "cloud-init status --long || cloud-init status",
    },
    "service_baseline": {
        "label": "Service Baseline",
        "category": "System",
        "description": "Quick system health and networking status",
        "command": "systemctl is-system-running || true; systemctl --failed --no-legend || true; ip -brief address",
    },
    "boot_log": {
        "label": "Boot Log",
        "category": "Boot & logs",
        "description": "Failed units, this boot's journal and the cloud-init output log (last lines)",
        "command": (
            'printf "=== failed units ===\\n"; systemctl --failed --no-pager || true; '
            'printf "\\n=== journal, this boot (last 300) ===\\n"; sudo -n journalctl -b --no-pager -o short-iso -n 300 || journalctl -b --no-pager -n 300 || true; '
            'printf "\\n=== cloud-init-output.log (last 200) ===\\n"; sudo -n tail -n 200 /var/log/cloud-init-output.log || true'
        ),
    },
    "remote_inventory": {
        "label": "Remote Inventory",
        "category": "Inventory",
        "description": "Collect hostname, OS, cloud-init, services, network, and storage state",
        "command": (
            'printf "=== identity ===\\n"; '
            'hostname; whoami; id; uname -a; hostnamectl || true; '
            'printf "\\n=== os-release ===\\n"; cat /etc/os-release || true; '
            'printf "\\n=== cloud-init ===\\n"; cloud-init status --long || cloud-init status || true; '
            'printf "\\n=== network ===\\n"; ip -brief address || true; ip route || true; '
            'printf "\\n=== storage ===\\n"; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL || true; df -h || true; '
            'printf "\\n=== services ===\\n"; systemctl is-active ssh || true; systemctl is-active cloud-init-local cloud-init cloud-config cloud-final k3s || true; '
            'printf "\\n=== k3s-config ===\\n"; sudo -n cat /etc/rancher/k3s/config.yaml || true'
        ),
    },
}


def _run_test_terminal_job(job_id: str, cmd: list[str]) -> None:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        with TEST_TERMINAL_LOCK:
            TEST_TERMINAL_JOBS[job_id]["status"] = "failed"
            TEST_TERMINAL_JOBS[job_id]["stderr"] = str(exc)
            TEST_TERMINAL_JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        return

    output_chunks: list[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            output_chunks.append(line)
            with TEST_TERMINAL_LOCK:
                TEST_TERMINAL_JOBS[job_id]["stdout"] = "".join(output_chunks)[-20000:]

    proc.wait()
    with TEST_TERMINAL_LOCK:
        TEST_TERMINAL_JOBS[job_id]["exit_code"] = proc.returncode
        TEST_TERMINAL_JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
        TEST_TERMINAL_JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


TERMINAL_IP_SOURCES = {"source_ip", "callback"}


def _terminal_preferred_host_for_node(node: Dict[str, Any]) -> str:
    callback_ip = str(node.get("callback_source_ip", "") or "").strip()
    if _is_usable_callback_ip(callback_ip):
        return callback_ip
    return str(node.get("address", "") or "").strip()


def _terminal_alias_connect_host_map(nodes: list[Dict[str, Any]]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "") or "").strip()
        preferred = _terminal_preferred_host_for_node(node)
        if not node_name or not preferred:
            continue
        for alias in _node_aliases(node_name):
            alias_key = str(alias or "").strip().lower()
            if alias_key:
                alias_map[alias_key] = preferred
    return alias_map


def _terminal_event_connect_host(event: Dict[str, Any], alias_map: Dict[str, str]) -> str:
    hostname = str(event.get("hostname", "") or "").strip().lower()
    fqdn = str(event.get("fqdn", "") or "").strip().lower()
    for alias in (hostname, fqdn):
        if alias and alias in alias_map:
            return alias_map[alias]

    source_ip = str(event.get("source_ip", "") or "").strip()
    if _is_usable_callback_ip(source_ip):
        return source_ip
    return ""


def _append_terminal_target(
    targets: list[Dict[str, Any]],
    seen: set[str],
    *,
    host: str,
    source: str,
    event: Dict[str, Any],
    connect_host: str = "",
) -> None:
    value = str(host or "").strip()
    if not value:
        return
    lowered = value.lower()
    if lowered in {"unknown", "none", "-"}:
        return
    if lowered in seen:
        return
    seen.add(lowered)

    hostname = str(event.get("hostname", "") or "").strip()
    fqdn = str(event.get("fqdn", "") or "").strip()
    profile = str(event.get("profile", "") or "").strip()
    target_connect_host = str(connect_host or value).strip()
    targets.append(
        {
            "host": value,
            "connect_host": target_connect_host,
            "source": source,
            "hostname": hostname,
            "fqdn": fqdn,
            "instance_id": str(event.get("instance_id", "") or "").strip(),
            "profile": profile,
            "last_seen": str(event.get("received_at", "") or "").strip(),
            "usable_callback_ip": _is_usable_callback_ip(target_connect_host) if source in TERMINAL_IP_SOURCES else None,
            "label_hint": hostname or fqdn or profile,
        }
    )


def _test_terminal_targets(limit: int = 2000) -> list[Dict[str, Any]]:
    events = _read_events(limit=limit)
    nodes = _node_ops_inventory_view()
    targets: list[Dict[str, Any]] = []
    seen: set[str] = set()
    alias_map = _terminal_alias_connect_host_map(nodes)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "") or "").strip()
        node_address = _terminal_preferred_host_for_node(node)
        callback_ip = str(node.get("callback_source_ip", "") or "").strip()
        if not node_name or not node_address:
            continue
        normalized_source = str(node.get("address_source", "") or "inventory").strip() or "inventory"
        if callback_ip and node_address == callback_ip and _is_usable_callback_ip(callback_ip):
            source = "source_ip"
        elif normalized_source == "callback":
            source = "source_ip"
        else:
            source = normalized_source
        event_like = {
            "hostname": node_name,
            "fqdn": "",
            "profile": str(node.get("callback_profile", "") or "").strip(),
            "instance_id": str(node.get("callback_instance_id", "") or "").strip(),
            "received_at": str(node.get("last_callback", "") or "").strip(),
        }
        _append_terminal_target(targets, seen, host=node_address, source=source, event=event_like, connect_host=node_address)

    for event in events:
        if not isinstance(event, dict):
            continue
        hostname = str(event.get("hostname", "") or "").strip()
        fqdn = str(event.get("fqdn", "") or "").strip()
        source_ip = str(event.get("source_ip", "") or "").strip()
        resolved_connect = _terminal_event_connect_host(event, alias_map)

        _append_terminal_target(
            targets,
            seen,
            host=hostname,
            source="hostname",
            event=event,
            connect_host=resolved_connect or hostname,
        )
        _append_terminal_target(
            targets,
            seen,
            host=fqdn,
            source="fqdn",
            event=event,
            connect_host=resolved_connect or fqdn,
        )
        _append_terminal_target(
            targets,
            seen,
            host=source_ip,
            source="source_ip",
            event=event,
            connect_host=source_ip,
        )

    return targets


def _resolve_terminal_host(host: str, limit: int = 2000) -> str:
    raw = str(host or "").strip()
    if not raw:
        return ""
    if _normalize_ip(raw):
        return raw

    lowered = raw.lower()
    nodes = _node_ops_inventory_view()
    alias_map = _terminal_alias_connect_host_map(nodes)
    from_nodes = alias_map.get(lowered, "")
    if from_nodes:
        return from_nodes

    for event in _read_events(limit=limit):
        if not isinstance(event, dict):
            continue
        event_connect = _terminal_event_connect_host(event, alias_map)
        if not event_connect:
            continue
        hostname = str(event.get("hostname", "") or "").strip().lower()
        fqdn = str(event.get("fqdn", "") or "").strip().lower()
        if lowered not in {hostname, fqdn}:
            continue
        return event_connect
    return raw
