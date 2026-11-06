"""Pull a node's boot logs over SSH once it has called back, and keep the last few per node."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from services.network import _preferred_node_connect_target
from services.node_inventory import _node_ops_inventory_view
from services.node_state import _load_node_config
from services.ssh_keys import (
    _ansible_known_hosts_args,
    _is_safe_key_name,
    _seed_known_host,
    _ssh_private_key_preflight,
)
from services.storage import BOOT_LOGS_DIR, KEYS_DIR

logger = logging.getLogger(__name__)

SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
KEEP_PER_NODE = 10
MAX_LOG_CHARS = 1_000_000
SSH_TIMEOUT_SECONDS = 90
# Stages the site generates itself while probing a node; reacting to them would loop.
SKIP_STAGES = {"hardware-check"}
AUTO_DELAY_SECONDS = 20
AUTO_COOLDOWN_SECONDS = 600

_last_auto_fetch: Dict[str, float] = {}
_auto_lock = threading.Lock()

# Runs on the node: each source is best effort so one missing file never loses the rest.
_REMOTE_SCRIPT = r"""
S() { sudo -n "$@" 2>/dev/null || "$@" 2>/dev/null; }
echo "=== node ==="; hostname; uname -a; uptime -s 2>/dev/null; cat /proc/device-tree/model 2>/dev/null; echo
echo "=== cloud-init status ==="; cloud-init status --long 2>&1
echo "=== systemctl --failed ==="; systemctl --failed --no-pager 2>&1
echo "=== journalctl -b (last 1500) ==="; S journalctl -b --no-pager -o short-iso -n 1500
echo "=== /var/log/cloud-init.log (last 800) ==="; S tail -n 800 /var/log/cloud-init.log
echo "=== /var/log/cloud-init-output.log (last 800) ==="; S tail -n 800 /var/log/cloud-init-output.log
echo "=== dmesg (last 400) ==="; S dmesg --time-format iso | tail -n 400
"""


def _safe_name(value: str) -> str:
    text = str(value or "").strip()
    return text if SAFE_NAME.fullmatch(text) else ""


def _node_dir(node_name: str):
    return BOOT_LOGS_DIR / node_name


def _auto_fetch_enabled() -> bool:
    return os.getenv("CALLBACK_AUTO_BOOT_LOGS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _write_entry(node_name: str, meta: Dict[str, Any], text: str) -> Dict[str, Any]:
    directory = _node_dir(node_name)
    directory.mkdir(parents=True, exist_ok=True)
    log_id = meta["id"]
    if text:
        (directory / f"{log_id}.log").write_text(text, encoding="utf-8")
    (directory / f"{log_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    for stale in sorted(directory.glob("*.json"), reverse=True)[KEEP_PER_NODE:]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".log").unlink(missing_ok=True)
    return meta


def _list_boot_logs(node_name: str) -> List[Dict[str, Any]]:
    name = _safe_name(node_name)
    if not name or not _node_dir(name).is_dir():
        return []
    entries: List[Dict[str, Any]] = []
    for path in sorted(_node_dir(name).glob("*.json"), reverse=True):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(loaded, dict):
            entries.append(loaded)
    return entries


def _read_boot_log(node_name: str, log_id: str) -> str | None:
    name = _safe_name(node_name)
    ident = _safe_name(log_id)
    if not name or not ident:
        return None
    path = _node_dir(name) / f"{ident}.log"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _fetch_boot_logs(node_name: str, trigger: str = "manual", address: str | None = None) -> Dict[str, Any]:
    name = _safe_name(node_name)
    if not name:
        raise ValueError("invalid node name")
    now = datetime.now(timezone.utc)
    meta: Dict[str, Any] = {
        "id": now.strftime("%Y%m%dT%H%M%S%fZ"),
        "node": name,
        "fetched_at": now.isoformat(),
        "trigger": trigger,
        "status": "failed",
        "bytes": 0,
        "truncated": False,
        "target": "",
        "error": "",
    }

    node_entry = next(
        (item for item in _node_ops_inventory_view() if isinstance(item, dict) and str(item.get("name", "")).strip() == name),
        {},
    )
    callback_ip = str(node_entry.get("callback_connect_ip", "") or node_entry.get("callback_source_ip", "") or "").strip()
    host, source = _preferred_node_connect_target(name, str(address or node_entry.get("address", "") or "").strip(), callback_ip)
    if not host:
        meta["error"] = "No node address known yet."
        return _write_entry(name, meta, "")

    user = str(node_entry.get("node_username", "") or "").strip() or str(_load_node_config().get("username", "") or "").strip() or "jdizzle"
    cert = str(node_entry.get("certificate", "") or "").strip()
    key_path = str(KEYS_DIR / (cert if cert and _is_safe_key_name(cert) else "rack-bootstrap-ed25519"))
    meta["target"] = f"{user}@{host} ({source})"

    key_error = _ssh_private_key_preflight(key_path)
    if key_error:
        meta["error"] = key_error
        return _write_entry(name, meta, "")

    _seed_known_host(host)
    cmd = [
        "ssh",
        "-i", key_path,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        *_ansible_known_hosts_args().split(),
        f"{user}@{host}",
        "bash -s",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=_REMOTE_SCRIPT,
            check=False,
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        meta["error"] = "ssh command not found on this host."
        return _write_entry(name, meta, "")
    except subprocess.TimeoutExpired:
        meta["error"] = f"Timed out after {SSH_TIMEOUT_SECONDS}s collecting logs."
        return _write_entry(name, meta, "")

    text = proc.stdout or ""
    if proc.returncode != 0 and not text.strip():
        meta["error"] = (proc.stderr or "").strip()[-500:] or f"ssh exited with code {proc.returncode}."
        return _write_entry(name, meta, "")
    if len(text) > MAX_LOG_CHARS:
        text = "[earlier output truncated]\n" + text[-MAX_LOG_CHARS:]
        meta["truncated"] = True
    meta.update({"status": "ok", "bytes": len(text.encode("utf-8"))})
    logger.info("Stored boot logs for node=%s (%s bytes) via %s", name, meta["bytes"], meta["target"])
    return _write_entry(name, meta, text)


def _boot_log_auto_worker(name: str, address: str) -> None:
    time.sleep(AUTO_DELAY_SECONDS)
    try:
        _fetch_boot_logs(name, trigger="callback", address=address or None)
    except Exception:  # noqa: BLE001 - a background pull must never take the site down
        logger.exception("Automatic boot log fetch failed for node=%s", name)


def _schedule_boot_log_fetch(event: Dict[str, Any]) -> bool:
    """Queue a delayed pull after a callback; one per node per cooldown window."""
    if not _auto_fetch_enabled():
        return False
    name = _safe_name(str(event.get("hostname", "") or ""))
    if not name or str(event.get("stage", "") or "").strip().lower() in SKIP_STAGES:
        return False
    with _auto_lock:
        now = time.monotonic()
        if now - _last_auto_fetch.get(name, -AUTO_COOLDOWN_SECONDS) < AUTO_COOLDOWN_SECONDS:
            return False
        _last_auto_fetch[name] = now
    address = str(event.get("source_ip", "") or "").strip()
    threading.Thread(target=_boot_log_auto_worker, args=(name, address), daemon=True).start()
    return True
