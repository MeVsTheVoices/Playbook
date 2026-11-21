"""Read a node's basic network facts with Ansible."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.network import _preferred_node_connect_target
from services.node_inventory import _node_ops_inventory_view
from services.node_state import _load_node_config
from services.ssh_keys import (
    _ansible_known_hosts_args,
    _is_safe_key_name,
    _seed_known_host,
    _ssh_private_key_preflight,
)
from services.storage import BASE_DIR, KEYS_DIR

logger = logging.getLogger(__name__)


def _network_snapshot(facts: dict[str, Any]) -> dict[str, Any]:
    if facts.get("ansible_system") != "Linux":
        raise ValueError("Ansible did not identify this node as Linux.")
    names = facts.get("ansible_interfaces")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("Ansible did not return interface facts; check that the node has the ip command.")
    interfaces = []
    for name in names:
        details = facts.get("ansible_" + name.replace("-", "_"), {})
        if not isinstance(details, dict):
            continue
        ipv4 = details.get("ipv4")
        ipv6 = details.get("ipv6")
        interfaces.append({
            "name": name,
            "type": details.get("type"),
            "active": details.get("active"),
            "mac": details.get("macaddress"),
            "mtu": details.get("mtu"),
            "ipv4": {key: ipv4[key] for key in ("address", "prefix", "netmask") if key in ipv4} if isinstance(ipv4, dict) else {},
            "ipv6": [
                {key: item[key] for key in ("address", "prefix", "scope") if key in item}
                for item in ipv6 if isinstance(item, dict)
            ] if isinstance(ipv6, list) else [],
            "ssid": details.get("ssid"),
        })
    default_ipv4 = facts.get("ansible_default_ipv4")
    dns = facts.get("ansible_dns")
    return {
        "interfaces": interfaces,
        "default_ipv4": {
            key: default_ipv4[key] for key in ("interface", "address", "gateway")
            if key in default_ipv4
        } if isinstance(default_ipv4, dict) else {},
        "dns_servers": dns.get("nameservers", []) if isinstance(dns, dict) and isinstance(dns.get("nameservers", []), list) else [],
    }


ANSIBLE_TIMEOUT_SECONDS = 150


def run_ansible_module(
    node_name: str, module: str, args: str, *, alias: str, library: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run one read-only Ansible module ad hoc against a node, as its login user (no become).

    Returns (result, response): result carries ok/node/target/read_at/error, response is the module's JSON or None.
    """
    node = next((item for item in _node_ops_inventory_view() if item.get("name") == node_name), {})
    callback_ip = str(node.get("callback_connect_ip") or node.get("callback_source_ip") or "").strip()
    host, source = _preferred_node_connect_target(node_name, str(node.get("address") or "").strip(), callback_ip)
    user = str(node.get("node_username") or _load_node_config().get("username") or "jdizzle").strip()
    cert = str(node.get("certificate") or "").strip()
    key_path = str(KEYS_DIR / (cert if cert and _is_safe_key_name(cert) else "rack-bootstrap-ed25519"))
    result: dict[str, Any] = {
        "ok": False, "node": node_name, "target": f"{user}@{host} ({source})" if host else "",
        "read_at": datetime.now(timezone.utc).isoformat(), "error": "",
    }
    if not host:
        result["error"] = "No node address known yet."
        return result, None
    key_error = _ssh_private_key_preflight(key_path)
    if key_error:
        result["error"] = key_error
        return result, None
    _seed_known_host(host)
    ansible = shutil.which("ansible")
    if not ansible:
        venv_ansible = BASE_DIR.parent / ".venv" / "bin" / "ansible"
        ansible = str(venv_ansible) if venv_ansible.is_file() else "ansible"

    inventory = {"all": {"hosts": {alias: {
        "ansible_host": host,
        "ansible_user": user,
        "ansible_ssh_private_key_file": key_path,
        "ansible_ssh_common_args": _ansible_known_hosts_args(),
        "ansible_ssh_extra_args": "-o PreferredAuthentications=publickey",
        "ansible_connection": "ssh",
        "ansible_become": False,
    }}}}
    env = os.environ.copy()
    for key in ("ANSIBLE_ASK_VAULT_PASS", "ANSIBLE_VAULT_IDENTITY_LIST", "ANSIBLE_VAULT_PASSWORD_FILE"):
        env.pop(key, None)
    library_args = ["-M", str(BASE_DIR.parent / "library")] if library else []
    response: dict[str, Any] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="node-ansible-") as directory:
            inventory_path = Path(directory) / "inventory.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            proc = subprocess.run(
                [ansible, "-i", str(inventory_path), alias, *library_args, "-m", module, "-a", args, "-o"],
                check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                timeout=ANSIBLE_TIMEOUT_SECONDS, cwd=str(BASE_DIR.parent), env=env,
            )
        _, separator, payload = (proc.stdout or "").partition("=>")
        try:
            parsed = json.loads(payload.strip()) if separator else None
        except ValueError:
            parsed = None
        if proc.returncode:
            message = parsed.get("msg") if isinstance(parsed, dict) else ""
            result["error"] = (str(message or "") or proc.stderr or proc.stdout or f"Ansible exited with code {proc.returncode}.").strip()[-1000:]
        elif not isinstance(parsed, dict):
            result["error"] = "Could not parse the Ansible result."
        else:
            response = parsed
    except FileNotFoundError:
        result["error"] = "ansible command not found on this host."
    except subprocess.TimeoutExpired:
        result["error"] = f"Ansible timed out after {ANSIBLE_TIMEOUT_SECONDS} seconds."
    if result["error"]:
        logger.warning("Ansible %s failed for node=%s via %s: %s", module, node_name, result["target"], result["error"])
    return result, response


def read_node_network(node_name: str) -> dict[str, Any]:
    result, response = run_ansible_module(
        node_name, "ansible.builtin.setup", "gather_subset=!all,network", alias="network_target",
    )
    if response is None:
        return result
    try:
        facts = response.get("ansible_facts")
        if not isinstance(facts, dict):
            raise ValueError("missing Ansible facts")
        result.update(ok=True, network=_network_snapshot(facts))
    except (ValueError, TypeError) as exc:
        result["error"] = f"Could not read network facts: {exc}"
    return result
