"""Connection profile endpoint: what a node can be reached at and which profile the strategy picks."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from flask import jsonify, request

from services.boot_logs import _safe_name
from services.connection_profiles import (
    _read_node_profiles,
    active_strategy_name,
    describe_route,
    gather_profiles,
    select_profile,
    strategy_names,
)
from services.node_inventory import _node_ops_inventory_view


def register_connection_profile_routes(app: Any) -> None:
    @app.get("/api/nodes/<node_name>/connection-profiles")
    def connection_profiles(node_name: str) -> Any:
        name = _safe_name(node_name)
        if not name:
            return jsonify({"ok": False, "error": "invalid node name"}), 400
        node = next((item for item in _node_ops_inventory_view() if isinstance(item, dict) and item.get("name") == name), None)
        if node is None:
            return jsonify({"ok": False, "error": "node not found"}), 404
        candidates = gather_profiles(
            name,
            str(node.get("address", "") or ""),
            str(node.get("callback_connect_ip", "") or node.get("callback_source_ip", "") or ""),
            user=str(node.get("node_username", "") or ""),
            key=str(node.get("certificate", "") or ""),
        )
        chosen = select_profile(candidates)
        include_routes = request.args.get("routes") == "1"
        route_cache = {}
        rendered = []
        for profile in candidates:
            record = asdict(profile)
            if include_routes:
                if profile.address not in route_cache:
                    route_cache[profile.address] = describe_route(profile.address)
                record["route"] = route_cache[profile.address]
            rendered.append(record)
        return jsonify(
            {
                "ok": True,
                "strategy": active_strategy_name(),
                "strategies": strategy_names(),
                "selected": chosen.id if chosen else "",
                "candidates": rendered,
                "stored": _read_node_profiles(name),
            }
        )
