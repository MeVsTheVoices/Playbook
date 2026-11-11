"""Node Ops configuration, input and provisioning endpoints."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict

from flask import jsonify, request

from domain_defaults import NODE_PROVISION_PROFILES

from services.k3s_config import LONGFORM_CHECKS, _check_longform, _longform_input_errors
from services.module_presets import _record_save
from services.connection_profiles import gather_profiles, remember_profiles, select_profile
from services.live_config import read_live_config
from services.node_inventory import _node_group_options, _node_ops_inventory_view, _ping_status
from services.node_runs import _apply_node_provisioning, _send_webhook, _telegraf_influx_dummy_check
from services.node_state import (
    _effective_node_profiles,
    _load_node_config,
    _load_node_provisioning,
    _merge_node_profile_inputs,
    _missing_required_inputs,
    _profile_map,
    _profiles_to_run,
    _redact_node_profile_inputs,
    _reset_node_ops_settings,
    _sanitize_node_profile_inputs,
    _save_node_config,
    _save_node_provisioning,
)
from services.provision_jobs import PROVISION_JOBS, PROVISION_JOBS_LOCK, _start_node_provision_job
from services.sd_profiles import _load_profile_segments, _upsert_profile_segment


def _without_blank_secrets(values: Dict[str, Any]) -> Dict[str, str]:
    # The form never shows saved secrets, so a blank secret means "keep the saved one".
    return {str(k): str(v) for k, v in values.items() if str(v) or str(k) not in {"influx_access_key", "influx_secret_key"}}


def _merged_telegraf_inputs(state: Dict[str, Any], node_name: str, raw_inputs: Any) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    saved_node_inputs = state.get("node_inputs", {}).get(node_name, {}) if isinstance(state.get("node_inputs", {}), dict) else {}

    if isinstance(saved_node_inputs, dict):
        saved_telegraf = saved_node_inputs.get("telegraf", {})
        if isinstance(saved_telegraf, dict):
            merged.update({str(k): str(v) for k, v in saved_telegraf.items()})

    if isinstance(raw_inputs, dict):
        candidate = raw_inputs.get("telegraf")
        if isinstance(candidate, dict):
            merged.update(_without_blank_secrets(candidate))
        else:
            direct_keys = {
                "influx_endpoint",
                "influx_bucket",
                "influx_org",
                "influx_access_key",
                "influx_secret_key",
            }
            direct_values = {k: str(v) for k, v in raw_inputs.items() if str(k) in direct_keys}
            if direct_values:
                merged.update(_without_blank_secrets(direct_values))

    return merged


def register_node_routes(app: Any) -> None:
    @app.get("/api/nodes/profile-segments")
    def nodes_profile_segments() -> Any:
        return jsonify({"ok": True, "segments": _load_profile_segments()})

    @app.post("/api/nodes/profile-segments")
    def nodes_profile_segments_upsert() -> Any:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid payload"}), 400
        profile_id = str(payload.get("profile_id", "")).strip()
        if not profile_id:
            return jsonify({"ok": False, "error": "profile_id is required"}), 400

        try:
            entry = _upsert_profile_segment(profile_id, payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        return jsonify({"ok": True, "profile_id": profile_id, "segment": entry})

    @app.post("/api/nodes/config")
    def nodes_config() -> Any:
        payload = request.get_json(silent=True) or {}
        config = _save_node_config(
            {
                "username": str(payload.get("username", "")).strip(),
                "password": str(payload.get("password", "")),
                "webhook_url": str(payload.get("webhook_url", "")).strip(),
                "verify_tls": bool(payload.get("verify_tls", True)),
            }
        )
        return jsonify(
            {
                "ok": True,
                "config": {
                    "username": config.get("username", ""),
                    "password_set": bool(config.get("password")),
                    "webhook_url": config.get("webhook_url", ""),
                    "verify_tls": bool(config.get("verify_tls", True)),
                },
            }
        )

    @app.post("/api/nodes/monitor")
    def nodes_monitor() -> Any:
        payload = request.get_json(silent=True) or {}
        emit_webhook = bool(payload.get("send_webhook", False))

        nodes = _node_ops_inventory_view()
        statuses: List[Dict[str, Any]] = []
        for node in nodes:
            callback_ip = str(node.get("callback_connect_ip", "") or node.get("callback_source_ip", "") or "")
            profiles = gather_profiles(
                node["name"],
                node["address"],
                callback_ip,
                user=str(node.get("node_username", "") or ""),
                key=str(node.get("certificate", "") or ""),
            )
            chosen = select_profile(profiles)
            connect_target = chosen.address if chosen else ""
            connect_source = chosen.source if chosen else "none"
            status = _ping_status(connect_target or node["address"], port=chosen.port if chosen else 22)
            remember_profiles(node["name"], profiles, chosen.id if chosen else "", status["reachable"])
            statuses.append(
                {
                    "name": node["name"],
                    "group": node["group"],
                    "address": node["address"],
                    "connect_target": connect_target,
                    "connect_source": connect_source,
                    "address_source": node.get("address_source", "inventory"),
                    "reachable": status["reachable"],
                    "latency_ms": status["latency_ms"],
                    "probe": status.get("probe", ""),
                    "last_callback": str(node.get("last_callback", "")),
                    "error": status["error"],
                }
            )

        webhook_result: Dict[str, Any] = {"sent": False}
        config = _load_node_config()
        webhook_url = config.get("webhook_url", "")
        if emit_webhook and webhook_url:
            summary = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "reachable": sum(1 for s in statuses if s["reachable"]),
                "unreachable": sum(1 for s in statuses if not s["reachable"]),
                "nodes": statuses,
            }
            result = _send_webhook(webhook_url, summary, verify_tls=bool(config.get("verify_tls", True)))
            webhook_result = {"sent": True, **result}

        return jsonify({"ok": True, "statuses": statuses, "webhook": webhook_result})

    @app.get("/api/nodes/profiles")
    def nodes_profiles() -> Any:
        state = _load_node_provisioning()
        return jsonify(
            {
                "ok": True,
                "profiles": NODE_PROVISION_PROFILES,
                "node_configs": state.get("node_configs", {}),
                "node_inputs": _redact_node_profile_inputs(state.get("node_inputs", {})),
                "manual_nodes": state.get("manual_nodes", {}),
            }
        )

    @app.get("/api/nodes/groups")
    def nodes_groups() -> Any:
        return jsonify({"ok": True, "groups": _node_group_options()})

    @app.post("/api/nodes/manual-nodes")
    def nodes_manual_node_upsert() -> Any:
        payload = request.get_json(silent=True) or {}
        node_name = str(payload.get("name", "")).strip()
        if not node_name:
            return jsonify({"ok": False, "error": "name is required"}), 400
        if not re.fullmatch(r"[A-Za-z0-9._-]+", node_name):
            return jsonify({"ok": False, "error": "invalid node name"}), 400

        group = str(payload.get("group", "manual")).strip() or "manual"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", group):
            return jsonify({"ok": False, "error": "invalid group"}), 400

        certificate = str(payload.get("certificate", "")).strip()
        if not certificate:
            return jsonify({"ok": False, "error": "certificate is required"}), 400

        state = _load_node_provisioning()
        manual_nodes = state.setdefault("manual_nodes", {})
        manual_nodes[node_name] = {
            "name": node_name,
            "group": group,
            "address": str(payload.get("address", "")).strip(),
            "username": str(payload.get("username", "")).strip(),
            "password": str(payload.get("password", "")),
            "default_shell": str(payload.get("default_shell", "")).strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        manual_nodes[node_name]["certificate"] = certificate
        _save_node_provisioning(state)

        redacted = dict(manual_nodes[node_name])
        redacted["password_set"] = bool(redacted.get("password"))
        redacted.pop("password", None)
        return jsonify({"ok": True, "node": redacted})

    @app.delete("/api/nodes/manual-nodes/<node_name>")
    def nodes_manual_node_delete(node_name: str) -> Any:
        name = str(node_name or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "node name is required"}), 400
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            return jsonify({"ok": False, "error": "invalid node name"}), 400

        state = _load_node_provisioning()
        manual_nodes = state.setdefault("manual_nodes", {})
        if name not in manual_nodes:
            return jsonify({"ok": False, "error": "manual node not found"}), 404

        manual_nodes.pop(name, None)
        node_configs = state.setdefault("node_configs", {})
        node_inputs = state.setdefault("node_inputs", {})
        node_results = state.setdefault("results", {})
        node_configs.pop(name, None)
        node_inputs.pop(name, None)
        node_results.pop(name, None)
        _save_node_provisioning(state)
        return jsonify({"ok": True, "node": name})

    @app.post("/api/nodes/node-configs")
    def nodes_node_configs() -> Any:
        payload = request.get_json(silent=True) or {}
        node_name = str(payload.get("node", "")).strip()
        profile_ids = payload.get("profile_ids") or []

        if not node_name:
            return jsonify({"ok": False, "error": "node is required"}), 400
        if not isinstance(profile_ids, list):
            return jsonify({"ok": False, "error": "profile_ids must be an array"}), 400

        known_nodes = {n["name"] for n in _node_ops_inventory_view()}
        if node_name not in known_nodes:
            return jsonify({"ok": False, "error": "node not found in active node set"}), 404

        valid_profile_ids = [str(pid) for pid in profile_ids if str(pid) in _profile_map()]
        state = _load_node_provisioning()
        node_configs = state.setdefault("node_configs", {})
        node_configs[node_name] = valid_profile_ids
        _save_node_provisioning(state)
        return jsonify({"ok": True, "node": node_name, "profile_ids": valid_profile_ids})

    @app.post("/api/config/check")
    def config_check() -> Any:
        payload = request.get_json(silent=True) or {}
        kind = str(payload.get("kind", "")).strip()
        result = _check_longform(kind, payload.get("text", ""))
        return jsonify(result), (200 if kind in LONGFORM_CHECKS else 400)

    @app.post("/api/nodes/node-inputs")
    def nodes_node_inputs() -> Any:
        payload = request.get_json(silent=True) or {}
        node_name = str(payload.get("node", "")).strip()
        raw_inputs = payload.get("profile_inputs") or {}

        if not node_name:
            return jsonify({"ok": False, "error": "node is required"}), 400
        if not isinstance(raw_inputs, dict):
            return jsonify({"ok": False, "error": "profile_inputs must be an object"}), 400

        nodes = _node_ops_inventory_view()
        node = next((n for n in nodes if n.get("name") == node_name), None)
        if not node:
            return jsonify({"ok": False, "error": "node not found in active node set"}), 404

        state = _load_node_provisioning()
        profile_ids = _effective_node_profiles(node, state)
        ignored = [str(pid) for pid in raw_inputs if str(pid) not in profile_ids]
        sanitized = _sanitize_node_profile_inputs(raw_inputs, profile_ids=profile_ids)
        longform_errors = [err for pid, values in sanitized.items() for err in _longform_input_errors(pid, values)]
        if longform_errors:
            return jsonify({"ok": False, "error": "; ".join(longform_errors)}), 400

        node_inputs = state.setdefault("node_inputs", {})
        node_inputs[node_name] = _merge_node_profile_inputs(node_inputs.get(node_name), sanitized)
        _save_node_provisioning(state)
        for module_id in sanitized:
            _record_save(node_name, module_id, node_inputs[node_name].get(module_id, {}))
        return jsonify({"ok": True, "node": node_name, "ignored_unselected_modules": ignored})

    @app.get("/api/nodes/live-config")
    def nodes_live_config() -> Any:
        node_name = str(request.args.get("node", "")).strip()
        module_id = str(request.args.get("module", "")).strip()
        if not node_name or not module_id:
            return jsonify({"ok": False, "error": "node and module are required"}), 400
        if module_id not in _profile_map():
            return jsonify({"ok": False, "error": "unknown module"}), 400
        if not any(n.get("name") == node_name for n in _node_ops_inventory_view()):
            return jsonify({"ok": False, "error": "node not found in active node set"}), 404
        # The read can fail for ordinary reasons (node off, SSH blocked); that is reported in the body, not as an HTTP error.
        return jsonify(read_live_config(node_name, module_id))

    @app.post("/api/nodes/telegraf/test")
    def nodes_telegraf_test() -> Any:
        payload = request.get_json(silent=True) or {}
        node_name = str(payload.get("node", "")).strip()
        raw_inputs = payload.get("profile_inputs") or {}

        if not node_name:
            return jsonify({"ok": False, "error": "node is required"}), 400

        nodes = _node_ops_inventory_view()
        node = next((n for n in nodes if n.get("name") == node_name), None)
        if not node:
            return jsonify({"ok": False, "error": "node not found in active node set"}), 404

        state = _load_node_provisioning()
        telegraf_inputs = _merged_telegraf_inputs(state, node_name, raw_inputs)

        check = _telegraf_influx_dummy_check(telegraf_inputs)
        status_code = 200 if check.get("ok") else 400
        return jsonify({"ok": bool(check.get("ok")), "node": node_name, "result": check}), status_code

    @app.post("/api/nodes/provision")
    def nodes_provision() -> Any:
        payload = request.get_json(silent=True) or {}
        node_name = str(payload.get("node", "")).strip()
        mode = str(payload.get("mode", "check")).strip().lower()
        run_ansible = bool(payload.get("run_ansible", True))

        if mode not in {"check", "apply"}:
            return jsonify({"ok": False, "error": "mode must be check or apply"}), 400

        profile_filter: List[str] = []
        raw_profile_ids = payload.get("profile_ids")
        if raw_profile_ids is not None:
            if not isinstance(raw_profile_ids, list):
                return jsonify({"ok": False, "error": "profile_ids must be an array"}), 400
            known_profiles = _profile_map()
            profile_filter = [str(pid) for pid in raw_profile_ids if str(pid) in known_profiles]

        nodes = _node_ops_inventory_view()
        if node_name:
            target_nodes = [n for n in nodes if n["name"] == node_name]
            if not target_nodes:
                return jsonify({"ok": False, "error": "node not found in active node set"}), 404
        else:
            target_nodes = nodes

        state = _load_node_provisioning()
        missing_by_node: List[Dict[str, Any]] = []

        for node in target_nodes:
            profile_ids = _profiles_to_run(node, state, profile_filter)
            if node_name and profile_filter and not profile_ids:
                labels = ", ".join(str(_profile_map()[pid].get("label", pid)) for pid in profile_filter)
                return jsonify({"ok": False, "error": f"{labels} does not apply to {node['name']} (group {node.get('group') or 'none'})."}), 400
            missing = _missing_required_inputs(node["name"], profile_ids, state)
            if missing and mode == "apply":
                missing_by_node.append(
                    {
                        "node": node["name"],
                        "profiles": missing,
                    }
                )

        if missing_by_node:
            return jsonify(
                {
                    "ok": False,
                    "error": "missing required provisioning inputs",
                    "missing_inputs": missing_by_node,
                }
            ), 400

        if mode == "apply" and profile_filter:
            # Applying a module makes it part of the node, like saving its inputs does.
            remembered = False
            for node in target_nodes:
                selected = _effective_node_profiles(node, state)
                added = [pid for pid in _profiles_to_run(node, state, profile_filter) if pid not in selected]
                if added:
                    state.setdefault("node_configs", {})[node["name"]] = selected + added
                    remembered = True
            if remembered:
                _save_node_provisioning(state)

        if run_ansible:
            job_id = _start_node_provision_job([node["name"] for node in target_nodes], mode, run_ansible, profile_filter)
            return jsonify({"ok": True, "mode": mode, "run_ansible": run_ansible, "job_id": job_id, "status": "queued"}), 202

        results: List[Dict[str, Any]] = []
        for node in target_nodes:
            profile_ids = _profiles_to_run(node, state, profile_filter)
            results.append(_apply_node_provisioning(node["name"], profile_ids, mode=mode, run_ansible=run_ansible))

        return jsonify({"ok": True, "mode": mode, "run_ansible": run_ansible, "results": results})

    @app.get("/api/nodes/provision/jobs/<job_id>")
    def node_provision_job_status(job_id: str) -> Any:
        with PROVISION_JOBS_LOCK:
            job = PROVISION_JOBS.get(job_id)
            if not job:
                return jsonify({"ok": False, "error": "job not found"}), 404
            return jsonify({"ok": True, "job": job})

    @app.post("/api/settings/reset")
    def settings_reset() -> Any:
        payload = request.get_json(silent=True) or {}
        scope = str(payload.get("scope", "node_ops")).strip().lower()

        if scope != "node_ops":
            return jsonify({"ok": False, "error": "scope must be node_ops"}), 400

        reset_result = _reset_node_ops_settings()
        return jsonify({"ok": True, "scope": scope, "reset": reset_result})
