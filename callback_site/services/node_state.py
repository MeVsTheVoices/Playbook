"""Node Ops persistence: monitored nodes, provisioning config, per-module inputs."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from domain_defaults import NODE_PROVISION_PROFILES
from network_toolkit import GROUP as TOOLKIT_GROUP
from playbook_server import GROUP as SERVER_GROUP

from services.storage import (
    BOOT_LOGS_DIR,
    CONNECTION_PROFILES_FILE,
    KEYS_DIR,
    KEY_ARCHIVE_DIR,
    KEY_ARCHIVE_INDEX_FILE,
    MODULE_PRESETS_FILE,
    MONITOR_STATE_FILE,
    NODE_CONFIG_FILE,
    NODE_PROVISION_FILE,
    _atomic_write_text,
    _ensure_data_file,
)


def _default_node_config() -> Dict[str, Any]:
    return {
        "username": "",
        "password": "",
        "webhook_url": "",
        "verify_tls": True,
    }


def _default_node_provisioning() -> Dict[str, Any]:
    return {"node_configs": {}, "results": {}, "node_inputs": {}, "manual_nodes": {}}


def _default_monitor_state() -> Dict[str, Any]:
    return {
        "managed_nodes": {},
        "cloud_init_options": {},
        "node_cloud_init": {},
    }


def _load_monitor_state() -> Dict[str, Any]:
    _ensure_data_file()
    default = _default_monitor_state()
    if not MONITOR_STATE_FILE.exists():
        return default

    try:
        loaded = json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default

    if not isinstance(loaded, dict):
        return default

    managed_nodes = loaded.get("managed_nodes", {})
    cloud_init_options = loaded.get("cloud_init_options", {})
    node_cloud_init = loaded.get("node_cloud_init", {})
    if not isinstance(managed_nodes, dict):
        managed_nodes = {}
    if not isinstance(cloud_init_options, dict):
        cloud_init_options = {}
    if not isinstance(node_cloud_init, dict):
        node_cloud_init = {}

    return {
        "managed_nodes": managed_nodes,
        "cloud_init_options": cloud_init_options,
        "node_cloud_init": node_cloud_init,
    }


def _save_monitor_state(state: Dict[str, Any]) -> None:
    _ensure_data_file()
    _atomic_write_text(MONITOR_STATE_FILE, json.dumps(state, indent=2))


def _sanitize_managed_node(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_name = str(payload.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", raw_name):
        raise ValueError("node name must use only letters, numbers, dot, dash, or underscore")

    raw_group = str(payload.get("group", "")).strip()
    group = raw_group or "custom"
    address = str(payload.get("address", "")).strip() or raw_name
    notes = str(payload.get("notes", "")).strip()
    return {
        "name": raw_name,
        "group": group,
        "address": address,
        "notes": notes,
        "enabled": bool(payload.get("enabled", True)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _sanitize_cloud_init_option(payload: Dict[str, Any]) -> Dict[str, Any]:
    option_id = str(payload.get("id", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]+", option_id):
        raise ValueError("option id must use only lowercase letters, numbers, dash, or underscore")

    label = str(payload.get("label", "")).strip() or option_id
    description = str(payload.get("description", "")).strip()
    raw_fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    allowed_types = {"text", "number", "bool", "select", "secret"}
    fields: List[Dict[str, Any]] = []

    for raw in raw_fields:
        if not isinstance(raw, dict):
            continue
        field_id = str(raw.get("id", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", field_id):
            continue
        field_type = str(raw.get("type", "text")).strip().lower()
        if field_type not in allowed_types:
            field_type = "text"
        field = {
            "id": field_id,
            "label": str(raw.get("label", "")).strip() or field_id,
            "type": field_type,
            "placeholder": str(raw.get("placeholder", "")).strip(),
            "default": str(raw.get("default", "")).strip(),
            "required": bool(raw.get("required", False)),
        }
        fields.append(field)

    return {
        "id": option_id,
        "label": label,
        "description": description,
        "fields": fields,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _node_aliases(name: str) -> set[str]:
    raw = str(name or "").strip().lower()
    if not raw:
        return set()
    aliases = {raw}
    if "." in raw:
        aliases.add(raw.split(".", 1)[0])
    return aliases


def _latest_node_cert(node_name: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    aliases = _node_aliases(node_name)
    for event in events:
        host_aliases = _node_aliases(str(event.get("hostname", "")))
        fqdn_aliases = _node_aliases(str(event.get("fqdn", "")))
        if not aliases.intersection(host_aliases.union(fqdn_aliases)):
            continue
        return {
            "received_at": str(event.get("received_at", "")),
            "cert_present": bool(event.get("cert_present", False)),
            "cert_subject": str(event.get("cert_subject", "")),
            "cert_issuer": str(event.get("cert_issuer", "")),
            "cert_fingerprint": str(event.get("cert_fingerprint", "")),
            "source_ip": str(event.get("source_ip", "")),
        }

    return {
        "received_at": "",
        "cert_present": False,
        "cert_subject": "",
        "cert_issuer": "",
        "cert_fingerprint": "",
        "source_ip": "",
    }


def _load_node_config() -> Dict[str, Any]:
    _ensure_data_file()
    default = _default_node_config()
    if not NODE_CONFIG_FILE.exists():
        return default
    try:
        loaded = json.loads(NODE_CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
    default.update({k: loaded.get(k, default[k]) for k in default})
    return default


def _save_node_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = _load_node_config()
    for key in ("username", "password", "webhook_url", "verify_tls"):
        if key in payload:
            config[key] = payload[key]
    _ensure_data_file()
    _atomic_write_text(NODE_CONFIG_FILE, json.dumps(config, indent=2))
    return config


def _default_profiles_for_group(group: str) -> List[str]:
    if group == TOOLKIT_GROUP:
        return ["baseline", "tailscale", "network-toolkit"]
    if group == SERVER_GROUP:
        return ["playbook-server"]
    defaults = ["baseline", "tailscale", "telegraf"]
    if group == "rpi5_active":
        defaults.append("docker")
    return defaults


def _load_node_provisioning() -> Dict[str, Any]:
    _ensure_data_file()
    default = _default_node_provisioning()
    if not NODE_PROVISION_FILE.exists():
        return default
    try:
        loaded = json.loads(NODE_PROVISION_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default

    if not isinstance(loaded, dict):
        return default
    node_configs = loaded.get("node_configs", {})
    results = loaded.get("results", {})
    node_inputs = loaded.get("node_inputs", {})
    manual_nodes = loaded.get("manual_nodes", {})
    if not isinstance(node_configs, dict):
        node_configs = {}
    if not isinstance(results, dict):
        results = {}
    if not isinstance(node_inputs, dict):
        node_inputs = {}
    if not isinstance(manual_nodes, dict):
        manual_nodes = {}
    return {"node_configs": node_configs, "results": results, "node_inputs": node_inputs, "manual_nodes": manual_nodes}


def _save_node_provisioning(state: Dict[str, Any]) -> None:
    _ensure_data_file()
    _atomic_write_text(NODE_PROVISION_FILE, json.dumps(state, indent=2))


def _reset_node_ops_settings() -> Dict[str, Any]:
    def _purge_files(path: Path) -> int:
        if not path.exists() or not path.is_dir():
            return 0
        removed = 0
        for child in path.iterdir():
            if child.is_file():
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    _ensure_data_file()
    node_cfg = _default_node_config()
    node_prov = _default_node_provisioning()
    _atomic_write_text(NODE_CONFIG_FILE, json.dumps(node_cfg, indent=2))
    _atomic_write_text(NODE_PROVISION_FILE, json.dumps(node_prov, indent=2))
    # Saved configurations hold the same secrets as the node inputs they were taken from.
    _atomic_write_text(MODULE_PRESETS_FILE, "[]")
    # Connection history and boot logs describe the nodes being reset.
    _atomic_write_text(CONNECTION_PROFILES_FILE, json.dumps({"nodes": {}}, indent=2))
    removed_boot_logs = sum(1 for _ in BOOT_LOGS_DIR.rglob("*.json")) if BOOT_LOGS_DIR.exists() else 0
    shutil.rmtree(BOOT_LOGS_DIR, ignore_errors=True)

    deleted_ssh_files = _purge_files(KEYS_DIR)
    deleted_archive_files = _purge_files(KEY_ARCHIVE_DIR)
    if KEY_ARCHIVE_INDEX_FILE.exists():
        try:
            KEY_ARCHIVE_INDEX_FILE.unlink()
        except OSError:
            pass
    return {
        "node_config": True,
        "node_provisioning": True,
        "connection_profiles": True,
        "boot_logs_removed": removed_boot_logs,
        "deleted_ssh_files": deleted_ssh_files,
        "deleted_archive_files": deleted_archive_files,
        "deleted_archive_index": not KEY_ARCHIVE_INDEX_FILE.exists(),
    }


def _profile_map() -> Dict[str, Dict[str, Any]]:
    return {p["id"]: p for p in NODE_PROVISION_PROFILES}


def _profile_input_specs(profile_id: str) -> List[Dict[str, Any]]:
    profile = _profile_map().get(profile_id, {})
    specs = profile.get("input_specs")
    if isinstance(specs, list):
        return [s for s in specs if isinstance(s, dict)]
    return []


def _sanitize_node_profile_inputs(payload: Dict[str, Any], profile_ids: List[str]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for profile_id in profile_ids:
        raw_inputs = payload.get(profile_id, {})
        if not isinstance(raw_inputs, dict):
            continue
        allowed = set(_profile_input_spec_map(profile_id))
        cleaned: Dict[str, str] = {}
        for key, value in raw_inputs.items():
            k = str(key)
            if k not in allowed:
                continue
            cleaned[k] = str(value).strip()
        out[profile_id] = cleaned
    return out


def _node_profile_inputs(node_name: str, state: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    node_inputs = state.get("node_inputs", {})
    raw = node_inputs.get(node_name, {}) if isinstance(node_inputs, dict) else {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for profile_id, values in raw.items():
        if isinstance(values, dict):
            out[str(profile_id)] = {str(k): str(v) for k, v in values.items()}
    return out


CONFIGURED_SECRET_MARKER = "__CONFIGURED__"  # keep in sync with nodes.html


def _profile_input_spec_map(profile_id: str) -> Dict[str, Dict[str, Any]]:
    return {str(spec.get("id", "")): spec for spec in _profile_input_specs(profile_id)}


def _redact_node_profile_inputs(node_inputs: Any) -> Dict[str, Dict[str, Dict[str, str]]]:
    redacted: Dict[str, Dict[str, Dict[str, str]]] = {}
    if not isinstance(node_inputs, dict):
        return redacted
    for node_name, raw_profiles in node_inputs.items():
        if not isinstance(raw_profiles, dict):
            continue
        profiles: Dict[str, Dict[str, str]] = {}
        for profile_id, raw_values in raw_profiles.items():
            if not isinstance(raw_values, dict):
                continue
            specs = _profile_input_spec_map(str(profile_id))
            profiles[str(profile_id)] = {
                str(field_id): (
                    CONFIGURED_SECRET_MARKER
                    if str(value) and specs.get(str(field_id), {}).get("sensitive")
                    else str(value)
                )
                for field_id, value in raw_values.items()
            }
        redacted[str(node_name)] = profiles
    return redacted


def _merge_node_profile_inputs(existing: Any, sanitized: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    # A blank sensitive field keeps its saved value, since the UI never echoes secrets back.
    existing_profiles = existing if isinstance(existing, dict) else {}
    merged = {str(pid): dict(values) for pid, values in existing_profiles.items() if isinstance(values, dict)}
    for profile_id, values in sanitized.items():
        specs = _profile_input_spec_map(profile_id)
        combined = merged.setdefault(profile_id, {})
        for field_id, value in values.items():
            if value or not specs.get(field_id, {}).get("sensitive"):
                combined[field_id] = value
    return merged


def _missing_required_inputs(node_name: str, profile_ids: List[str], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    saved_inputs = _node_profile_inputs(node_name, state)
    profile_by_id = _profile_map()
    missing: List[Dict[str, Any]] = []

    for profile_id in profile_ids:
        specs = _profile_input_specs(profile_id)
        required_specs = [s for s in specs if bool(s.get("required", False))]
        if not required_specs:
            continue

        profile_inputs = saved_inputs.get(profile_id, {})
        saved_role = str(profile_inputs.get("node_role", "")).strip().lower()
        missing_fields = []
        for spec in required_specs:
            field_id = str(spec.get("id", ""))
            if saved_role and saved_role in (spec.get("optional_for_roles") or []):
                continue
            value = str(profile_inputs.get(field_id, "")).strip()
            if not value:
                missing_fields.append(
                    {
                        "id": field_id,
                        "label": str(spec.get("label", field_id)),
                        "placeholder": str(spec.get("placeholder", "")),
                        "sensitive": bool(spec.get("sensitive", False)),
                    }
                )

        if missing_fields:
            missing.append(
                {
                    "profile_id": profile_id,
                    "profile_label": str(profile_by_id.get(profile_id, {}).get("label", profile_id)),
                    "fields": missing_fields,
                }
            )
    return missing


def _redact_profile_inputs(profile_id: str, inputs: Dict[str, str]) -> Dict[str, str]:
    specs = _profile_input_spec_map(profile_id)
    out: Dict[str, str] = {}
    for key, value in inputs.items():
        spec = specs.get(key, {})
        if bool(spec.get("sensitive", False)):
            out[key] = "***"
        else:
            out[key] = value
    return out


def _effective_node_profiles(node: Dict[str, Any], state: Dict[str, Any]) -> List[str]:
    node_configs = state.get("node_configs", {})
    selected = node_configs.get(node["name"])
    if isinstance(selected, list) and selected:
        return [str(v) for v in selected]
    return _default_profiles_for_group(node.get("group", ""))


def _profiles_to_run(node: Dict[str, Any], state: Dict[str, Any], profile_filter: List[str] | None) -> List[str]:
    """The node's selected modules, or the named ones when a filter is given."""
    selected = _effective_node_profiles(node, state)
    if not profile_filter:
        return selected
    # A module asked for by name runs even when it is not ticked for the node, if it applies to the node's group.
    group = str(node.get("group", "") or "")
    profile_by_id = _profile_map()
    return [pid for pid in profile_filter if pid in selected or group in (profile_by_id.get(pid, {}).get("groups") or [])]


def _latest_node_check(node_name: str, state: Dict[str, Any]) -> Dict[str, Any]:
    node_results = state.get("results", {}).get(node_name, {})
    if not isinstance(node_results, dict):
        return {"status": "not-run", "checked_at": "", "mode": ""}

    latest: Dict[str, Any] = {"status": "not-run", "checked_at": "", "mode": ""}
    for result in node_results.values():
        if not isinstance(result, dict):
            continue
        checked_at = str(result.get("checked_at", ""))
        if checked_at and checked_at >= str(latest.get("checked_at", "")):
            latest = {
                "status": str(result.get("status", "not-run")),
                "checked_at": checked_at,
                "mode": str(result.get("mode", "")),
            }
    return latest
