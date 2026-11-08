"""Connection profiles: every address a node can be reached at, and the replaceable strategy that picks one.

A profile describes one way to reach a node (callback address, inventory address, tailnet peer, DNS, bare
hostname). A strategy is a function that picks one profile from a node's list; swap it with
`register_strategy` or `CALLBACK_CONNECTION_STRATEGY` without touching the callers.
Profiles seen for each node are kept in `connection_profiles.json` so the history survives restarts.
"""

from __future__ import annotations

import json
import ipaddress
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.network import (
    _is_tailscale_ip,
    _is_transit_hop_ip,
    _is_usable_callback_ip,
    _resolve_lan_ip_from_names,
    _tailscale_peer_fqdn,
    _tailscale_peer_ip,
)
from services.storage import CONNECTION_PROFILES_FILE, _atomic_write_text, _ensure_data_file

DEFAULT_STRATEGY = "prefer_tailnet"
REFRESH_SECONDS = 300
logger = logging.getLogger(__name__)


def describe_route(address: str) -> Dict[str, str]:
    """Read the dashboard host's route without probing or changing the node."""
    details = {"interface": "", "gateway": "", "source_ip": "", "family": "", "error": ""}
    try:
        destination = ipaddress.ip_address(address)
    except ValueError:
        details["error"] = "Hostname route is resolved by SSH at connection time."
        return details
    details["family"] = f"IPv{destination.version}"
    try:
        result = subprocess.run(
            ["ip", "-json", "route", "get", str(destination)],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Route lookup unavailable for %s: %s", address, exc)
        details["error"] = "Local route lookup unavailable (requires Linux iproute2)."
        return details
    if result.returncode:
        details["error"] = (result.stderr or "No local route found.").strip()[:300]
        logger.warning("Route lookup failed for %s: %s", address, details["error"])
        return details
    try:
        routes = json.loads(result.stdout)
        route = routes[0]
        if not isinstance(route, dict):
            raise ValueError("Invalid route record")
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        logger.warning("Invalid route lookup response for %s: %s", address, exc)
        details["error"] = "Local route lookup returned an invalid response."
        return details
    details.update(
        interface=str(route.get("dev") or ""),
        gateway=str(route.get("gateway") or ""),
        source_ip=str(route.get("prefsrc") or route.get("src") or ""),
    )
    return details


@dataclass
class ConnectionProfile:
    id: str
    kind: str  # callback | inventory | tailnet | tailnet_fqdn | dns | hostname
    address: str
    source: str  # label shown to the operator: callback, inventory, tailscale, tailscale_fqdn, callback_recent, ...
    tailnet: bool = False
    usable: bool = True
    port: int = 22
    user: str = ""
    key: str = ""
    fqdn: str = ""  # the node's MagicDNS name when known; what a Tailscale HTTPS certificate is issued for


Strategy = Callable[[List[ConnectionProfile]], Optional[ConnectionProfile]]


_RESOLVE_CACHE: Dict[str, Tuple[float, bool]] = {}


RESOLVE_CACHE_SECONDS = 60


def _name_resolves(name: str) -> bool:
    """Whether this host can resolve a name right now (MagicDNS may be off); bounded to 2s and cached."""
    cached = _RESOLVE_CACHE.get(name)
    if cached and time.monotonic() - cached[0] < RESOLVE_CACHE_SECONDS:
        return cached[1]
    result: List[bool] = []

    def lookup() -> None:
        try:
            socket.getaddrinfo(name, None, socket.AF_INET)
            result.append(True)
        except OSError:
            result.append(False)

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(2.0)
    resolves = bool(result and result[0])
    _RESOLVE_CACHE[name] = (time.monotonic(), resolves)
    return resolves


def gather_profiles(node_name: str, inventory_address: str = "", callback_source_ip: str = "", user: str = "", key: str = "") -> List[ConnectionProfile]:
    node_name = str(node_name or "").strip()
    inventory_address = str(inventory_address or "").strip()
    callback_source_ip = str(callback_source_ip or "").strip()
    extra = {"user": str(user or "").strip(), "key": str(key or "").strip()}
    profiles: List[ConnectionProfile] = []

    if callback_source_ip:
        usable = _is_usable_callback_ip(callback_source_ip)
        profiles.append(ConnectionProfile("callback", "callback", callback_source_ip, "callback" if usable else "callback_recent", _is_tailscale_ip(callback_source_ip), usable, **extra))
    if inventory_address:
        tailnet = _is_tailscale_ip(inventory_address)
        usable = tailnet or not _is_transit_hop_ip(inventory_address)
        profiles.append(ConnectionProfile("inventory", "inventory", inventory_address, "inventory" if usable else "inventory_transit", tailnet, usable, **extra))
    peer = _tailscale_peer_ip(node_name)
    fqdn = _tailscale_peer_fqdn(node_name)
    if peer:
        profiles.append(ConnectionProfile("tailnet", "tailnet", peer, "tailscale", True, True, fqdn=fqdn, **extra))
    if fqdn:
        profiles.append(ConnectionProfile("tailnet_fqdn", "tailnet_fqdn", fqdn, "tailscale_fqdn", True, _name_resolves(fqdn), fqdn=fqdn, **extra))

    # DNS only matters when nothing better is known, and a lookup costs time on every poll.
    if not any(p.kind == "callback" or p.tailnet or (p.kind == "inventory" and p.usable) for p in profiles):
        resolved, resolved_source = _resolve_lan_ip_from_names(node_name)
        if resolved:
            profiles.append(ConnectionProfile("dns", "dns", resolved, resolved_source or "dns", False, True, **extra))
    if node_name:
        profiles.append(ConnectionProfile("hostname", "hostname", node_name, "hostname", False, False, **extra))
    return profiles


def _prefer_tailnet_rank(profile: ConnectionProfile) -> int:
    if profile.kind == "tailnet_fqdn":
        return 3 if profile.usable else 10
    if profile.tailnet:
        return {"callback": 0, "inventory": 1}.get(profile.kind, 2)
    if profile.kind == "callback":
        return 4 if profile.usable else 6
    if profile.kind == "inventory":
        return 5 if profile.usable else 8
    return {"dns": 7}.get(profile.kind, 9)


def _prefer_fqdn_rank(profile: ConnectionProfile) -> Tuple[int, int]:
    return (0 if profile.kind == "tailnet_fqdn" and profile.usable else 1, _prefer_tailnet_rank(profile))


def _lan_first_rank(profile: ConnectionProfile) -> Tuple[int, int]:
    lan = not profile.tailnet and profile.usable and profile.kind in {"callback", "inventory"}
    return (0 if lan else 1, _prefer_tailnet_rank(profile))


def ranked_strategy(rank: Callable[[ConnectionProfile], Any]) -> Strategy:
    def pick(profiles: List[ConnectionProfile]) -> Optional[ConnectionProfile]:
        return min(profiles, key=rank) if profiles else None

    return pick


_STRATEGIES: Dict[str, Strategy] = {}


def register_strategy(name: str, strategy: Strategy) -> None:
    _STRATEGIES[name] = strategy


register_strategy("prefer_tailnet", ranked_strategy(_prefer_tailnet_rank))
register_strategy("prefer_fqdn", ranked_strategy(_prefer_fqdn_rank))
register_strategy("lan_first", ranked_strategy(_lan_first_rank))


def strategy_names() -> List[str]:
    return sorted(_STRATEGIES)


def active_strategy_name() -> str:
    name = os.getenv("CALLBACK_CONNECTION_STRATEGY", "").strip()
    return name if name in _STRATEGIES else DEFAULT_STRATEGY


def select_profile(profiles: List[ConnectionProfile], strategy: str | None = None) -> Optional[ConnectionProfile]:
    chosen = _STRATEGIES.get(strategy or "") or _STRATEGIES[active_strategy_name()]
    return chosen(profiles)


def select_connection_target(node_name: str, inventory_address: str = "", callback_source_ip: str = "") -> Tuple[str, str]:
    chosen = select_profile(gather_profiles(node_name, inventory_address, callback_source_ip))
    return (chosen.address, chosen.source) if chosen else ("", "none")


_STORE_LOCK = threading.Lock()


def _load_store() -> Dict[str, Any]:
    try:
        loaded = json.loads(CONNECTION_PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"nodes": {}}
    return loaded if isinstance(loaded, dict) and isinstance(loaded.get("nodes"), dict) else {"nodes": {}}


def _read_node_profiles(node_name: str) -> Dict[str, Any]:
    entry = _load_store()["nodes"].get(node_name)
    return entry if isinstance(entry, dict) else {"profiles": {}, "selected": ""}


def remember_profiles(node_name: str, profiles: List[ConnectionProfile], selected_id: str = "", reachable: bool | None = None) -> None:
    """Keep each profile seen for a node; writes only when something changed or a record went stale."""
    name = str(node_name or "").strip()
    if not name or not profiles:
        return
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    with _STORE_LOCK:
        store = _load_store()
        entry = store["nodes"].setdefault(name, {"profiles": {}, "selected": ""})
        stored = entry.setdefault("profiles", {})
        changed = False
        for profile in profiles:
            record = stored.get(profile.id)
            fresh = asdict(profile)
            if record is None:
                stored[profile.id] = {**fresh, "first_seen": stamp, "last_seen": stamp}
                changed = True
                continue
            if any(record.get(key) != value for key, value in fresh.items()):
                record.update(fresh)
                record["last_seen"] = stamp
                changed = True
                continue
            try:
                age = (now - datetime.fromisoformat(str(record.get("last_seen", "")))).total_seconds()
            except ValueError:
                age = REFRESH_SECONDS
            if age >= REFRESH_SECONDS:
                record["last_seen"] = stamp
                changed = True
        if entry.get("selected") != selected_id:
            entry["selected"] = selected_id
            changed = True
        selected = stored.get(selected_id)
        if selected is not None and reachable is not None and selected.get("reachable") is not reachable:
            selected["reachable"] = reachable
            selected["last_ok_at" if reachable else "last_failed_at"] = stamp
            changed = True
        if changed:
            _ensure_data_file()
            _atomic_write_text(CONNECTION_PROFILES_FILE, json.dumps(store, indent=2))
