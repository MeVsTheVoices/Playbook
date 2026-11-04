"""Saved module configurations: auto-recorded history, named presets, and values drawn from earlier setups."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from services.node_state import (
    CONFIGURED_SECRET_MARKER,
    _load_node_provisioning,
    _merge_node_profile_inputs,
    _profile_input_spec_map,
    _profile_map,
)
from services.sd_profiles import _load_sd_option_profiles
from services.storage import MODULE_PRESETS_FILE, _atomic_write_text, _ensure_data_file

HISTORY_LIMIT = 30  # unnamed saves kept per module; named configurations are never trimmed
NAME_MAX = 60

# SD option key -> module input id, where the two differ. Same-named keys (k3s_*, timezone, ...) map directly.
_SD_KEY_ALIASES: Dict[str, Dict[str, str]] = {
    "tailscale": {"tailscale_auth_key": "auth_key", "tailscale_ssh": "enable_ssh", "tailscale_tags": "advertise_tags"},
    "kubernetes": {"k3s_role": "node_role"},
    "telegraf": {
        "telegraf_influx_url": "influx_endpoint",
        "telegraf_influx_token": "influx_access_key",
        "telegraf_influx_org": "influx_org",
        "telegraf_influx_bucket": "influx_bucket",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_presets() -> List[Dict[str, Any]]:
    _ensure_data_file()
    if not MODULE_PRESETS_FILE.exists():
        return []
    try:
        loaded = json.loads(MODULE_PRESETS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [p for p in loaded if isinstance(p, dict) and p.get("id") and isinstance(p.get("inputs"), dict)]


def _save_presets(presets: List[Dict[str, Any]]) -> None:
    _ensure_data_file()
    _atomic_write_text(MODULE_PRESETS_FILE, json.dumps(presets, indent=2))


def _clean_inputs(module_id: str, values: Any) -> Dict[str, str]:
    allowed = set(_profile_input_spec_map(module_id))
    if not isinstance(values, dict):
        return {}
    return {str(k): str(v).strip() for k, v in values.items() if str(k) in allowed}


def _validate_name(raw: Any) -> str:
    name = " ".join(str(raw or "").split())
    if len(name) > NAME_MAX:
        raise ValueError(f"name must be {NAME_MAX} characters or fewer")
    return name


def _trim_history(presets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    keep_unnamed: Dict[str, int] = {}
    kept: List[Dict[str, Any]] = []
    for preset in sorted(presets, key=lambda p: str(p.get("saved_at", "")), reverse=True):
        if not preset.get("name"):
            module = str(preset.get("module", ""))
            keep_unnamed[module] = keep_unnamed.get(module, 0) + 1
            if keep_unnamed[module] > HISTORY_LIMIT:
                continue
        kept.append(preset)
    return kept


def _record_save(node_name: str, module_id: str, inputs: Dict[str, str]) -> Dict[str, Any] | None:
    """Remember the exact inputs saved for a module; an identical earlier save is just refreshed."""
    inputs = _clean_inputs(module_id, inputs)
    if not any(inputs.values()):
        return None
    presets = _load_presets()
    for preset in presets:
        if preset.get("module") == module_id and preset.get("node") == node_name and preset.get("inputs") == inputs:
            preset["saved_at"] = _now()
            _save_presets(presets)
            return preset
    record = {
        "id": uuid.uuid4().hex[:12],
        "module": module_id,
        "name": "",
        "node": node_name,
        "saved_at": _now(),
        "inputs": inputs,
    }
    presets.append(record)
    _save_presets(_trim_history(presets))
    return record


def _name_preset(preset_id: str, name: str) -> Dict[str, Any]:
    name = _validate_name(name)
    presets = _load_presets()
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if target is None:
        raise KeyError(preset_id)
    if name:
        # A name is unique per module: reusing it moves the label to the newer configuration.
        presets = [p for p in presets if p is target or not (p.get("module") == target.get("module") and p.get("name") == name)]
    target["name"] = name
    _save_presets(presets)
    return target


def _delete_preset(preset_id: str) -> bool:
    presets = _load_presets()
    remaining = [p for p in presets if p.get("id") != preset_id]
    if len(remaining) == len(presets):
        return False
    _save_presets(remaining)
    return True


def _field_summary(module_id: str, inputs: Dict[str, str]) -> List[Dict[str, str]]:
    specs = _profile_input_spec_map(module_id)
    out: List[Dict[str, str]] = []
    for field_id, value in inputs.items():
        if not value:
            continue
        spec = specs.get(field_id, {})
        sensitive = bool(spec.get("sensitive"))
        shown = CONFIGURED_SECRET_MARKER if sensitive else value
        if len(shown) > 80:
            shown = shown[:77] + "..."
        out.append({"id": field_id, "label": str(spec.get("label", field_id)), "value": shown, "sensitive": sensitive})
    return out


def _public_preset(preset: Dict[str, Any]) -> Dict[str, Any]:
    module_id = str(preset.get("module", ""))
    return {
        "id": str(preset["id"]),
        "source": f"preset:{preset['id']}",
        "module": module_id,
        "name": str(preset.get("name", "")),
        "node": str(preset.get("node", "")),
        "saved_at": str(preset.get("saved_at", "")),
        "origin": "node-ops",
        "fields": _field_summary(module_id, preset["inputs"]),
    }


def _list_presets(module_id: str) -> List[Dict[str, Any]]:
    mine = [p for p in _load_presets() if p.get("module") == module_id]
    named = sorted((p for p in mine if p.get("name")), key=lambda p: str(p["name"]).lower())
    history = sorted((p for p in mine if not p.get("name")), key=lambda p: str(p.get("saved_at", "")), reverse=True)
    return [_public_preset(p) for p in named + history]


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value).strip() if value is not None else ""


def _sd_profile_inputs(module_id: str, profile: Dict[str, Any]) -> Dict[str, str]:
    """What an SD-card option profile already enabled, expressed as this module's inputs."""
    options = profile.get("options") if isinstance(profile.get("options"), dict) else {}
    allowed = set(_profile_input_spec_map(module_id))
    if module_id == "tailscale" and not options.get("tailscale_enable"):
        return {}
    if module_id == "kubernetes" and profile.get("distribution") != "k3s":
        return {}
    if module_id == "telegraf" and not options.get("install_telegraf"):
        return {}

    out: Dict[str, str] = {}
    aliases = _SD_KEY_ALIASES.get(module_id, {})
    for key, value in options.items():
        field_id = aliases.get(key, key)
        if field_id in allowed and field_id != "k3s_config":
            text = _as_text(value)
            if text:
                out[field_id] = text

    if module_id == "kubernetes":
        config = str(options.get("k3s_config", ""))
        server = re.search(r"^server:\s*[\"']?([^\"'\s#]+)", config, re.MULTILINE)
        token = re.search(r"^token:\s*[\"']?([^\"'\s#]+)", config, re.MULTILINE)
        if server:
            out["server_endpoint"] = server.group(1)
        if token:
            out["server_token"] = token.group(1)
    return out


def _sources(module_id: str, exclude_node: str = "") -> List[Dict[str, Any]]:
    """Settings already in use elsewhere: other nodes in Node Ops and SD profiles from the provisioning dashboard."""
    found: List[Dict[str, Any]] = []
    latest_save: Dict[str, str] = {}
    for preset in _load_presets():
        if preset.get("module") == module_id:
            node = str(preset.get("node", ""))
            latest_save[node] = max(latest_save.get(node, ""), str(preset.get("saved_at", "")))

    node_inputs = _load_node_provisioning().get("node_inputs", {})
    for node_name, modules in sorted(node_inputs.items()):
        if node_name == exclude_node or not isinstance(modules, dict):
            continue
        values = _clean_inputs(module_id, modules.get(module_id))
        if any(values.values()):
            found.append({
                "source": f"node:{node_name}",
                "origin": "node-ops",
                "label": node_name,
                "saved_at": latest_save.get(node_name, ""),
                "fields": _field_summary(module_id, values),
            })

    for name, profile in sorted(_load_sd_option_profiles().items()):
        values = _sd_profile_inputs(module_id, profile)
        if values:
            found.append({
                "source": f"sd:{name}",
                "origin": "provisioning-dashboard",
                "label": name,
                "saved_at": str(profile.get("updated_at", "")),
                "fields": _field_summary(module_id, values),
            })
    return found


def _resolve_source(module_id: str, source: str) -> Tuple[str, Dict[str, str]]:
    kind, _, ref = str(source or "").partition(":")
    if kind == "preset":
        preset = next((p for p in _load_presets() if p.get("id") == ref and p.get("module") == module_id), None)
        if preset is None:
            raise KeyError(source)
        return str(preset.get("name") or f"saved on {preset.get('node', '?')}"), dict(preset["inputs"])
    if kind == "node":
        values = _clean_inputs(module_id, _load_node_provisioning().get("node_inputs", {}).get(ref, {}).get(module_id))
        if not any(values.values()):
            raise KeyError(source)
        return f"Node Ops: {ref}", values
    if kind == "sd":
        profile = _load_sd_option_profiles().get(ref)
        values = _sd_profile_inputs(module_id, profile) if profile else {}
        if not values:
            raise KeyError(source)
        return f"Provisioning dashboard: {ref}", values
    raise KeyError(source)


def _merge_source_into_node(state: Dict[str, Any], node_name: str, module_id: str, values: Dict[str, str]) -> Dict[str, str]:
    """Overlay the source's non-empty values on the node's saved inputs; everything it leaves out is kept."""
    node_inputs = state.setdefault("node_inputs", {})
    merged = _merge_node_profile_inputs(node_inputs.get(node_name), {module_id: {k: v for k, v in values.items() if v}})
    node_inputs[node_name] = merged
    return merged.get(module_id, {})


def _module_exists(module_id: str) -> bool:
    return module_id in _profile_map()
