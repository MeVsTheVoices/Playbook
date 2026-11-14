"""Connection profile endpoint: what a node can be reached at and which profile the strategy picks."""

from __future__ import annotations

import ipaddress
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
        grouped = {}
        for profile in candidates:
            try:
                address = str(ipaddress.ip_address(profile.address))
            except ValueError:
                address = profile.address.lower().rstrip(".")
            endpoint = (address, profile.port, profile.user, profile.key)
            record = asdict(profile)
            existing = grouped.get(endpoint)
            sources = list(existing["sources"]) if existing else []
            if profile.source not in sources:
                sources.append(profile.source)
            if existing:
                record["fqdn"] = record["fqdn"] or existing["fqdn"]
                record["tailnet"] = record["tailnet"] or existing["tailnet"]
            if existing and profile is not chosen:
                existing["sources"] = sources
                existing["fqdn"] = record["fqdn"]
                existing["tailnet"] = record["tailnet"]
                continue
            record["sources"] = sources
            grouped[endpoint] = record
        rendered = list(grouped.values())
        for record in rendered:
            if include_routes:
                address = record["address"]
                if address not in route_cache:
                    route_cache[address] = describe_route(address)
                record["route"] = route_cache[address]
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
