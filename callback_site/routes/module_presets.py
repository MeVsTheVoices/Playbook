"""Saved module configurations: list, name, delete and load into a node."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from flask import jsonify, request

from services.k3s_config import _longform_input_errors
from services.module_presets import (
    _delete_preset,
    _list_presets,
    _merge_source_into_node,
    _module_exists,
    _name_preset,
    _record_save,
    _resolve_source,
    _sources,
)
from services.node_inventory import _node_ops_inventory_view
from services.node_state import _effective_node_profiles, _load_node_provisioning, _save_node_provisioning


def _node_and_module(payload: Dict[str, Any]) -> Tuple[Dict[str, Any] | None, str, Any]:
    node_name = str(payload.get("node", "")).strip()
    module_id = str(payload.get("module", "")).strip()
    if not node_name or not module_id:
        return None, module_id, (jsonify({"ok": False, "error": "node and module are required"}), 400)
    if not _module_exists(module_id):
        return None, module_id, (jsonify({"ok": False, "error": "unknown module"}), 400)
    node = next((n for n in _node_ops_inventory_view() if n.get("name") == node_name), None)
    if node is None:
        return None, module_id, (jsonify({"ok": False, "error": "node not found in active node set"}), 404)
    return node, module_id, None


def register_module_preset_routes(app: Any) -> None:
    @app.get("/api/nodes/module-presets")
    def module_presets_list() -> Any:
        module_id = str(request.args.get("module", "")).strip()
        if not _module_exists(module_id):
            return jsonify({"ok": False, "error": "unknown module"}), 400
        node_name = str(request.args.get("node", "")).strip()
        return jsonify({"ok": True, "module": module_id, "presets": _list_presets(module_id), "sources": _sources(module_id, exclude_node=node_name)})

    @app.post("/api/nodes/module-presets")
    def module_presets_name() -> Any:
        payload = request.get_json(silent=True) or {}
        preset_id = str(payload.get("id", "")).strip()
        try:
            if preset_id:
                preset = _name_preset(preset_id, payload.get("name", ""))
            else:
                node, module_id, error = _node_and_module(payload)
                if error:
                    return error
                state = _load_node_provisioning()
                saved = state.get("node_inputs", {}).get(node["name"], {}).get(module_id, {})
                record = _record_save(node["name"], module_id, saved if isinstance(saved, dict) else {})
                if record is None:
                    return jsonify({"ok": False, "error": "nothing is saved for this module yet; save its inputs first"}), 400
                # A blank name keeps the configuration under the name the site suggested for it.
                preset = _name_preset(record["id"], payload.get("name", "") or record["auto_name"])
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except KeyError:
            return jsonify({"ok": False, "error": "saved configuration not found"}), 404
        return jsonify({"ok": True, "name": preset.get("name", ""), "id": preset["id"]})

    @app.delete("/api/nodes/module-presets/<preset_id>")
    def module_presets_delete(preset_id: str) -> Any:
        if not _delete_preset(str(preset_id).strip()):
            return jsonify({"ok": False, "error": "saved configuration not found"}), 404
        return jsonify({"ok": True})

    @app.post("/api/nodes/module-presets/load")
    def module_presets_load() -> Any:
        payload = request.get_json(silent=True) or {}
        node, module_id, error = _node_and_module(payload)
        if error:
            return error
        try:
            label, values = _resolve_source(module_id, str(payload.get("source", "")))
        except KeyError:
            return jsonify({"ok": False, "error": "that source no longer has settings for this module"}), 404

        state = _load_node_provisioning()
        merged = _merge_source_into_node(state, node["name"], module_id, values)
        problems = _longform_input_errors(module_id, merged)
        if problems:
            return jsonify({"ok": False, "error": "; ".join(problems)}), 400

        selected = _effective_node_profiles(node, state)
        if module_id not in selected:
            state.setdefault("node_configs", {})[node["name"]] = selected + [module_id]
        _save_node_provisioning(state)
        _record_save(node["name"], module_id, merged)
        filled = sorted(k for k, v in values.items() if v)
        return jsonify({"ok": True, "node": node["name"], "module": module_id, "label": label, "filled": filled})
