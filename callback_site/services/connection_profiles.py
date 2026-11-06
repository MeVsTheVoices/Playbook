"""Connection profiles: every address a node can be reached at, and the replaceable strategy that picks one.

A profile describes one way to reach a node (callback address, inventory address, tailnet peer, DNS, bare
hostname). A strategy is a function that picks one profile from a node's list; swap it with
`register_strategy` or `CALLBACK_CONNECTION_STRATEGY` without touching the callers.
Profiles seen for each node are kept in `connection_profiles.json` so the history survives restarts.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.network import (
    _is_tailscale_ip,
    _is_transit_hop_ip,
    _is_usable_callback_ip,
    _resolve_lan_ip_from_names,
    _tailscale_peer_ip,
)
from services.storage import CONNECTION_PROFILES_FILE, _atomic_write_text, _ensure_data_file

DEFAULT_STRATEGY = "prefer_tailnet"
REFRESH_SECONDS = 300


@dataclass
class ConnectionProfile:
    id: str
    kind: str  # callback | inventory | tailnet | dns | hostname
    address: str
    source: str  # label shown to the operator: callback, inventory, tailscale, callback_recent, ...
    tailnet: bool = False
    usable: bool = True
    port: int = 22
    user: str = ""
    key: str = ""


Strategy = Callable[[List[ConnectionProfile]], Optional[ConnectionProfile]]


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
    if peer:
        profiles.append(ConnectionProfile("tailnet", "tailnet", peer, "tailscale", True, True, **extra))

    # DNS only matters when nothing better is known, and a lookup costs time on every poll.
    if not any(p.kind == "callback" or p.tailnet or (p.kind == "inventory" and p.usable) for p in profiles):
        resolved, resolved_source = _resolve_lan_ip_from_names(node_name)
        if resolved:
            profiles.append(ConnectionProfile("dns", "dns", resolved, resolved_source or "dns", False, True, **extra))
    if node_name:
        profiles.append(ConnectionProfile("hostname", "hostname", node_name, "hostname", False, False, **extra))
    return profiles


def _prefer_tailnet_rank(profile: ConnectionProfile) -> int:
    if profile.tailnet:
        return {"callback": 0, "inventory": 1}.get(profile.kind, 2)
    if profile.kind == "callback":
        return 3 if profile.usable else 5
    if profile.kind == "inventory":
        return 4 if profile.usable else 7
    return {"dns": 6}.get(profile.kind, 8)


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
