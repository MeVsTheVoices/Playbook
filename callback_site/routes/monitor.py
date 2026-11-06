"""Hardware monitor, node summary and source-of-truth endpoints."""

from __future__ import annotations

import re
from typing import Any

from flask import jsonify, request

from domain_defaults import NODE_PROVISION_PROFILES

from services.events import _read_events
from services.kubernetes import (
    NODE_NAME_RE,
    _clear_stale_nodes,
    _clear_stale_pods,
    _kubernetes_local_summary,
)
from services.node_inventory import _ping_status
from services.node_state import (
    _latest_node_cert,
    _load_monitor_state,
    _sanitize_cloud_init_option,
    _sanitize_managed_node,
    _save_monitor_state,
)
from services.node_views import _monitor_nodes_summary, _prune_stale_nodes, _single_source_of_truth_summary


def _clear_request_options() -> tuple[int, bool, Any]:
    """(min_minutes, dry_run, error response or None) for the cluster cleanup endpoints; dry run is the default."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return 0, True, (jsonify({"ok": False, "error": "invalid payload"}), 400)
    try:
        min_minutes = int(payload.get("min_minutes", 10))
    except (TypeError, ValueError):
        return 0, True, (jsonify({"ok": False, "error": "min_minutes must be an integer"}), 400)
    if min_minutes < 0 or min_minutes > 10080:
        return 0, True, (jsonify({"ok": False, "error": "min_minutes must be between 0 and 10080"}), 400)
    return min_minutes, payload.get("dry_run", True) is not False, None


def _single_removal_response(result: dict) -> Any:
    if not result.get("ok") and "removed" not in result:
        return jsonify(result), 502
    if result.get("removed"):
        return jsonify(result)
    if result.get("failed"):
        return jsonify(result), 502
    skipped = result.get("skipped") or [{}]
    reason = str(skipped[0].get("reason", "not eligible"))
    return jsonify({**result, "ok": False, "error": reason}), 404 if reason == "not found" else 409


def register_monitor_routes(app: Any) -> None:
    @app.get("/api/nodes/summary")
    def nodes_summary() -> Any:
        state = _single_source_of_truth_summary()
        provisioning = state.get("provisioning", {}) if isinstance(state.get("provisioning"), dict) else {}
        nodes = [
            node
            for node in (state.get("nodes", []) if isinstance(state.get("nodes", []), list) else [])
            if isinstance(node, dict) and bool(node.get("in_node_ops_view", False))
        ]
        return jsonify(
            {
                "ok": True,
                "nodes": nodes,
                "config": provisioning.get("config", {}),
                "profiles": NODE_PROVISION_PROFILES,
                "node_inputs": provisioning.get("node_inputs", {}),
                "manual_nodes": provisioning.get("manual_nodes", {}),
                "image_summary": state.get("images", {}).get("summary", {}) if isinstance(state.get("images"), dict) else {},
            }
        )

    @app.get("/api/monitor/summary")
    def monitor_summary() -> Any:
        state = _single_source_of_truth_summary()
        monitor = state.get("monitor", {}) if isinstance(state.get("monitor"), dict) else {}
        nodes = [
            node
            for node in (state.get("nodes", []) if isinstance(state.get("nodes", []), list) else [])
            if isinstance(node, dict) and bool(node.get("in_monitor_view", False))
        ]
        return jsonify(
            {
                "ok": True,
                "nodes": nodes,
                "profiles": NODE_PROVISION_PROFILES,
                "cloud_init_options": monitor.get("cloud_init_options", []),
                "node_cloud_init": monitor.get("node_cloud_init", {}),
            }
        )

    @app.get("/api/state/source-of-truth")
    def source_of_truth_summary() -> Any:
        return jsonify({"ok": True, "state": _single_source_of_truth_summary()})

    @app.get("/api/kubernetes/summary")
    def kubernetes_summary() -> Any:
        return jsonify(_kubernetes_local_summary())

    @app.post("/api/kubernetes/nodes/clear")
    def kubernetes_clear_nodes() -> Any:
        min_minutes, dry_run, error = _clear_request_options()
        if error:
            return error
        result = _clear_stale_nodes(min_minutes=min_minutes, dry_run=dry_run)
        return jsonify(result), 200 if result.get("ok") or "removed" in result else 502

    @app.delete("/api/kubernetes/nodes/<name>")
    def kubernetes_remove_node(name: str) -> Any:
        if not NODE_NAME_RE.fullmatch(name) or len(name) > 253:
            return jsonify({"ok": False, "error": "invalid node name"}), 400
        return _single_removal_response(_clear_stale_nodes(min_minutes=0, dry_run=False, names=[name]))

    @app.post("/api/kubernetes/pods/clear")
    def kubernetes_clear_pods() -> Any:
        min_minutes, dry_run, error = _clear_request_options()
        if error:
            return error
        result = _clear_stale_pods(min_minutes=min_minutes, dry_run=dry_run)
        return jsonify(result), 200 if result.get("ok") or "removed" in result else 502

    @app.delete("/api/kubernetes/pods/<namespace>/<name>")
    def kubernetes_remove_pod(namespace: str, name: str) -> Any:
        for value in (namespace, name):
            if not NODE_NAME_RE.fullmatch(value) or len(value) > 253:
                return jsonify({"ok": False, "error": "invalid pod name"}), 400
        return _single_removal_response(_clear_stale_pods(min_minutes=0, dry_run=False, targets=[(namespace, name)]))

    @app.post("/api/nodes/stale/prune")
    def prune_stale_nodes() -> Any:
        payload = request.get_json(silent=True) or {}
        if payload and not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid payload"}), 400

        raw_hours = payload.get("max_age_hours", 168)
        raw_dry_run = payload.get("dry_run", False)
        if isinstance(raw_dry_run, str):
            dry_run = raw_dry_run.strip().lower() in {"1", "true", "yes", "on"}
        else:
            dry_run = bool(raw_dry_run)
        try:
            max_age_hours = int(raw_hours)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "max_age_hours must be an integer"}), 400
        if max_age_hours < 1 or max_age_hours > 24 * 365:
            return jsonify({"ok": False, "error": "max_age_hours must be between 1 and 8760"}), 400

        result = _prune_stale_nodes(max_age_hours=max_age_hours, dry_run=dry_run)
        return jsonify(result)

    @app.post("/api/monitor/nodes")
    def monitor_node_upsert() -> Any:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid payload"}), 400

        try:
            node = _sanitize_managed_node(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        state = _load_monitor_state()
        managed_nodes = state.setdefault("managed_nodes", {})
        managed_nodes[node["name"]] = node
        _save_monitor_state(state)
        return jsonify({"ok": True, "node": node})

    @app.delete("/api/monitor/nodes/<node_name>")
    def monitor_node_delete(node_name: str) -> Any:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", node_name):
            return jsonify({"ok": False, "error": "invalid node name"}), 400

        state = _load_monitor_state()
        managed_nodes = state.setdefault("managed_nodes", {})
        if node_name not in managed_nodes:
            return jsonify({"ok": False, "error": "managed node not found"}), 404
        managed_nodes.pop(node_name, None)

        node_cloud_init = state.setdefault("node_cloud_init", {})
        node_cloud_init.pop(node_name, None)
        _save_monitor_state(state)
        return jsonify({"ok": True})

    @app.get("/api/monitor/nodes/<node_name>/certificate")
    def monitor_node_certificate(node_name: str) -> Any:
        cert = _latest_node_cert(node_name, _read_events(limit=2000))
        return jsonify({"ok": True, "node": node_name, "certificate": cert})

    @app.get("/api/monitor/nodes/<node_name>/ping")
    def monitor_node_ping(node_name: str) -> Any:
        requested = str(node_name or "").strip()
        if not requested:
            return jsonify({"ok": False, "error": "node name is required"}), 400

        summary = _monitor_nodes_summary()
        node = next((item for item in summary.get("nodes", []) if str(item.get("name", "")) == requested), None)
        if not node:
            return jsonify({"ok": False, "error": "node not found"}), 404

        address = str(node.get("address", "")).strip()
        if not address:
            return jsonify({"ok": False, "error": "node has no address configured"}), 400

        status = _ping_status(address)
        return jsonify(
            {
                "ok": True,
                "node": requested,
                "address": address,
                "reachable": bool(status.get("reachable")),
                "latency_ms": status.get("latency_ms"),
                "error": str(status.get("error", "")),
            }
        )

    @app.post("/api/monitor/cloud-init/options")
    def monitor_cloud_init_option_upsert() -> Any:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid payload"}), 400

        try:
            option = _sanitize_cloud_init_option(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        state = _load_monitor_state()
        cloud_options = state.setdefault("cloud_init_options", {})
        cloud_options[option["id"]] = option
        _save_monitor_state(state)
        return jsonify({"ok": True, "option": option})

    @app.delete("/api/monitor/cloud-init/options/<option_id>")
    def monitor_cloud_init_option_delete(option_id: str) -> Any:
        option_key = str(option_id).strip().lower()
        state = _load_monitor_state()
        cloud_options = state.setdefault("cloud_init_options", {})
        if option_key not in cloud_options:
            return jsonify({"ok": False, "error": "option not found"}), 404

        cloud_options.pop(option_key, None)
        node_cloud_init = state.setdefault("node_cloud_init", {})
        for node_name, selected in list(node_cloud_init.items()):
            if not isinstance(selected, list):
                continue
            node_cloud_init[node_name] = [v for v in selected if str(v).strip().lower() != option_key]

        _save_monitor_state(state)
        return jsonify({"ok": True})

    @app.post("/api/monitor/nodes/<node_name>/cloud-init")
    def monitor_node_cloud_init_assign(node_name: str) -> Any:
        payload = request.get_json(silent=True) or {}
        option_ids = payload.get("option_ids") or []
        if not isinstance(option_ids, list):
            return jsonify({"ok": False, "error": "option_ids must be an array"}), 400

        state = _load_monitor_state()
        cloud_options = state.setdefault("cloud_init_options", {})
        valid_ids = [str(v).strip().lower() for v in option_ids if str(v).strip().lower() in cloud_options]
        node_cloud_init = state.setdefault("node_cloud_init", {})
        node_cloud_init[node_name] = valid_ids
        _save_monitor_state(state)
        return jsonify({"ok": True, "node": node_name, "option_ids": valid_ids})
