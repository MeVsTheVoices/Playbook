"""Read models over nodes and events: monitor summary, source of truth, stale-node pruning."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from domain_defaults import IMAGE_CATALOG_FILE, NODE_PROVISION_PROFILES
from k3s_lightweight import group_for_profile as _k3s_group_for_profile

from services.events import (
    _event_matches_node_aliases,
    _keys_matching_aliases,
    _parse_event_time,
    _read_events,
)
from services.image_catalog import _image_state_summary
from services.images import _read_image_history
from services.node_inventory import _last_callback_by_host, _latest_hardware_by_host, _node_ops_inventory_view
from services.provision_jobs import _start_node_provision_job
from services.node_state import (
    _effective_node_profiles,
    _latest_node_cert,
    _latest_node_check,
    _load_monitor_state,
    _load_node_config,
    _load_node_provisioning,
    _node_aliases,
    _profile_input_specs,
    _redact_node_profile_inputs,
    _save_monitor_state,
    _save_node_provisioning,
)
from services.storage import (
    DATA_FILE,
    IMAGE_HISTORY_FILE,
    INVENTORY_FILE,
    MONITOR_STATE_FILE,
    NODE_PROVISION_FILE,
    _ensure_data_file,
)

logger = logging.getLogger(__name__)


def _prune_stale_nodes(max_age_hours: int = 168, dry_run: bool = False) -> Dict[str, Any]:
    hours = max(1, int(max_age_hours))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    node_inventory = _node_ops_inventory_view()
    stale_nodes: List[Dict[str, Any]] = []
    stale_aliases: set[str] = set()

    for node in node_inventory:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "")).strip()
        last_callback = _parse_event_time(str(node.get("last_callback", "")))
        if not node_name or last_callback is None:
            continue
        if last_callback >= cutoff:
            continue

        stale_aliases.update(_node_aliases(node_name.lower()))
        stale_nodes.append(
            {
                "name": node_name,
                "group": str(node.get("group", "")).strip(),
                "address": str(node.get("address", "")).strip(),
                "last_callback": str(node.get("last_callback", "")).strip(),
                "address_source": str(node.get("address_source", "")).strip(),
            }
        )

    stale_nodes.sort(key=lambda item: str(item.get("name", "")).lower())

    if not stale_nodes:
        return {
            "ok": True,
            "dry_run": bool(dry_run),
            "cutoff": cutoff.isoformat(),
            "max_age_hours": hours,
            "stale_nodes": [],
            "removed": {
                "callbacks": 0,
                "managed_nodes": 0,
                "node_cloud_init": 0,
                "node_configs": 0,
                "node_inputs": 0,
                "node_results": 0,
                "manual_nodes": 0,
            },
        }

    removed_counts = {
        "callbacks": 0,
        "managed_nodes": 0,
        "node_cloud_init": 0,
        "node_configs": 0,
        "node_inputs": 0,
        "node_results": 0,
        "manual_nodes": 0,
    }

    # Plan callback event removals.
    _ensure_data_file()
    existing_lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    kept_lines: List[str] = []
    for line in existing_lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            kept_lines.append(line)
            continue
        if _event_matches_node_aliases(event, stale_aliases):
            removed_counts["callbacks"] += 1
            continue
        kept_lines.append(line)

    # Plan monitor-state removals.
    monitor_state = _load_monitor_state()
    managed_nodes = monitor_state.get("managed_nodes", {}) if isinstance(monitor_state.get("managed_nodes"), dict) else {}
    node_cloud_init = monitor_state.get("node_cloud_init", {}) if isinstance(monitor_state.get("node_cloud_init"), dict) else {}

    # Plan provisioning-state removals.
    node_prov = _load_node_provisioning()
    node_configs = node_prov.get("node_configs", {}) if isinstance(node_prov.get("node_configs"), dict) else {}
    node_inputs = node_prov.get("node_inputs", {}) if isinstance(node_prov.get("node_inputs"), dict) else {}
    node_results = node_prov.get("results", {}) if isinstance(node_prov.get("results"), dict) else {}
    manual_nodes = node_prov.get("manual_nodes", {}) if isinstance(node_prov.get("manual_nodes"), dict) else {}

    managed_keys = _keys_matching_aliases(managed_nodes, stale_aliases)
    cloud_init_keys = _keys_matching_aliases(node_cloud_init, stale_aliases)
    node_config_keys = _keys_matching_aliases(node_configs, stale_aliases)
    node_input_keys = _keys_matching_aliases(node_inputs, stale_aliases)
    node_result_keys = _keys_matching_aliases(node_results, stale_aliases)
    manual_node_keys = _keys_matching_aliases(manual_nodes, stale_aliases)

    removed_counts["managed_nodes"] = len(managed_keys)
    removed_counts["node_cloud_init"] = len(cloud_init_keys)
    removed_counts["node_configs"] = len(node_config_keys)
    removed_counts["node_inputs"] = len(node_input_keys)
    removed_counts["node_results"] = len(node_result_keys)
    removed_counts["manual_nodes"] = len(manual_node_keys)

    if not dry_run:
        rendered = "\n".join(kept_lines)
        DATA_FILE.write_text((rendered + "\n") if kept_lines else "", encoding="utf-8")

        for key in managed_keys:
            managed_nodes.pop(key, None)
        for key in cloud_init_keys:
            node_cloud_init.pop(key, None)
        for key in node_config_keys:
            node_configs.pop(key, None)
        for key in node_input_keys:
            node_inputs.pop(key, None)
        for key in node_result_keys:
            node_results.pop(key, None)
        for key in manual_node_keys:
            manual_nodes.pop(key, None)

        monitor_state["managed_nodes"] = managed_nodes
        monitor_state["node_cloud_init"] = node_cloud_init
        _save_monitor_state(monitor_state)

        node_prov["node_configs"] = node_configs
        node_prov["node_inputs"] = node_inputs
        node_prov["results"] = node_results
        node_prov["manual_nodes"] = manual_nodes
        _save_node_provisioning(node_prov)

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "cutoff": cutoff.isoformat(),
        "max_age_hours": hours,
        "stale_nodes": stale_nodes,
        "removed": removed_counts,
    }


def _monitor_nodes_summary() -> Dict[str, Any]:
    state = _load_monitor_state()
    provision = _load_node_provisioning()
    managed_nodes = state.get("managed_nodes", {}) if isinstance(state.get("managed_nodes", {}), dict) else {}
    node_cloud_init = state.get("node_cloud_init", {}) if isinstance(state.get("node_cloud_init", {}), dict) else {}
    events = _read_events(limit=2000)
    callbacks = _last_callback_by_host()
    hardware_by_host = _latest_hardware_by_host(limit=2000)

    combined: Dict[str, Dict[str, Any]] = {}

    def _cloud_init_ids_for_node(node_name: str) -> List[str]:
        if not isinstance(node_cloud_init, dict):
            return []

        # Prefer exact-name assignment first.
        direct = node_cloud_init.get(node_name)
        if isinstance(direct, list):
            return [str(v) for v in direct]

        # Fall back to alias match (hostname/fqdn/history variations).
        target_aliases = _node_aliases(node_name)
        for key, raw_value in node_cloud_init.items():
            if not isinstance(raw_value, list):
                continue
            if _node_aliases(str(key or "")).intersection(target_aliases):
                return [str(v) for v in raw_value]
        return []

    # Auto-discover nodes from callback events so fresh nodes appear without manual add.
    # This keeps the monitor empty on a brand-new state until the first callback lands.
    for event in events:
        node_name = str(event.get("hostname", "")).strip()
        if not node_name or node_name in combined:
            continue
        event_profile = str(event.get("profile", "")).strip().lower()
        group = _k3s_group_for_profile(event_profile, "discovered")

        address = str(event.get("source_ip", "")).strip() or node_name
        combined[node_name] = {
            "name": node_name,
            "group": group,
            "address": address,
            "notes": "",
            "enabled": True,
            "source": "callback",
        }

    for node_name, raw in managed_nodes.items():
        if not isinstance(raw, dict):
            continue
        name = str(node_name).strip()
        if not name:
            continue
        managed = {
            "name": name,
            "group": str(raw.get("group", "custom")).strip() or "custom",
            "address": str(raw.get("address", name)).strip() or name,
            "notes": str(raw.get("notes", "")).strip(),
            "enabled": bool(raw.get("enabled", True)),
            "source": "managed",
        }
        if name in combined:
            combined[name].update({
                "group": managed["group"],
                "address": managed["address"],
                "notes": managed["notes"],
                "enabled": managed["enabled"],
                "source": "callback+managed",
            })
        else:
            combined[name] = managed

    nodes: List[Dict[str, Any]] = []
    for node in sorted(combined.values(), key=lambda n: str(n.get("name", "")).lower()):
        node_name = str(node.get("name", ""))
        cert = _latest_node_cert(node_name, events)
        cloud_ids = _cloud_init_ids_for_node(node_name)
        enriched = dict(node)
        enriched["last_callback"] = callbacks.get(node_name, "")
        enriched["cert"] = cert
        enriched["cloud_init_option_ids"] = cloud_ids
        enriched["selected_profiles"] = _effective_node_profiles(enriched, provision)

        hw: Dict[str, Any] = {}
        for alias in _node_aliases(node_name):
            hw = hardware_by_host.get(alias, {})
            if hw:
                break
        enriched["hardware"] = hw

        nodes.append(enriched)

    raw_options = state.get("cloud_init_options", {}) if isinstance(state.get("cloud_init_options", {}), dict) else {}
    cloud_init_options = [v for v in raw_options.values() if isinstance(v, dict)]
    cloud_init_options.sort(key=lambda item: str(item.get("label", "")).lower())

    return {
        "nodes": nodes,
        "cloud_init_options": cloud_init_options,
        "node_cloud_init": node_cloud_init,
    }


def _single_source_of_truth_summary() -> Dict[str, Any]:
    monitor_summary = _monitor_nodes_summary()
    provision_state = _load_node_provisioning()
    node_config = _load_node_config()
    nodes_view = _node_ops_inventory_view()
    image_summary = _image_state_summary()
    image_history = _read_image_history(limit=1000)

    latest_image_by_profile: Dict[str, Dict[str, Any]] = {}
    for record in image_history:
        profile_key = str(record.get("profile", "")).strip().lower() or "unknown"
        if profile_key in latest_image_by_profile:
            continue
        latest_image_by_profile[profile_key] = {
            "profile": profile_key,
            "label": str(record.get("label", "")).strip(),
            "source": str(record.get("source", "")).strip(),
            "source_value": str(record.get("source_value", "")).strip(),
            "download_path": str(record.get("download_path", "")).strip(),
            "recorded_at": str(record.get("recorded_at", "")).strip(),
            "local_available": bool(record.get("local_available", False)),
            "hash_status": str(record.get("hash_status", "")).strip(),
            "job_status": str(record.get("job_status", "")).strip(),
        }

    profile_meta: Dict[str, Dict[str, Any]] = {}
    for profile in NODE_PROVISION_PROFILES:
        if not isinstance(profile, dict):
            continue
        pid = str(profile.get("id", "")).strip()
        if not pid:
            continue
        profile_meta[pid] = {
            "id": pid,
            "label": str(profile.get("label", pid)).strip() or pid,
            "description": str(profile.get("description", "")).strip(),
            "tags": [str(tag).strip() for tag in profile.get("tags", []) if str(tag).strip()],
        }

    monitor_nodes = monitor_summary.get("nodes", []) if isinstance(monitor_summary.get("nodes", []), list) else []
    monitor_by_name: Dict[str, Dict[str, Any]] = {}
    for monitor_node in monitor_nodes:
        if not isinstance(monitor_node, dict):
            continue
        monitor_name = str(monitor_node.get("name", "")).strip()
        if monitor_name:
            monitor_by_name[monitor_name] = monitor_node

    node_ops_by_name: Dict[str, Dict[str, Any]] = {}
    for node in nodes_view:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "")).strip()
        if node_name:
            node_ops_by_name[node_name] = node

    normalized_nodes: List[Dict[str, Any]] = []
    all_node_names = sorted(set(node_ops_by_name.keys()).union(monitor_by_name.keys()), key=lambda name: name.lower())

    for node_name in all_node_names:
        node = dict(node_ops_by_name.get(node_name, {}))
        monitor_info = monitor_by_name.get(node_name, {})
        if not node:
            node = {
                "name": node_name,
                "group": str(monitor_info.get("group", "")).strip(),
                "address": str(monitor_info.get("address", "")).strip(),
                "address_source": "monitor",
                "last_callback": str(monitor_info.get("last_callback", "")).strip(),
                "source": str(monitor_info.get("source", "")).strip() or "monitor",
            }

        selected_profiles = _effective_node_profiles(node, provision_state)
        latest_check = _latest_node_check(node_name, provision_state)
        latest_check_steps: List[Dict[str, Any]] = []
        for raw_step in latest_check.get("results", []) if isinstance(latest_check, dict) else []:
            if not isinstance(raw_step, dict):
                continue
            latest_check_steps.append(
                {
                    "profile_id": str(raw_step.get("profile_id", "")).strip(),
                    "status": str(raw_step.get("status", "")).strip(),
                    "changed": bool(raw_step.get("changed", False)),
                    "command": str(raw_step.get("command", "")).strip(),
                    "output": str(raw_step.get("output", "")).strip()[:220],
                }
            )

        callback_profile = str(node.get("callback_profile", "")).strip().lower()
        image_profile_key = callback_profile or (selected_profiles[0] if selected_profiles else "")
        image_for_node = latest_image_by_profile.get(image_profile_key) if image_profile_key else {}
        cert_info = monitor_info.get("cert", {}) if isinstance(monitor_info.get("cert"), dict) else {}
        hardware_info = monitor_info.get("hardware", {}) if isinstance(monitor_info.get("hardware"), dict) else {}
        cloud_init_option_ids = (
            monitor_info.get("cloud_init_option_ids", [])
            if isinstance(monitor_info.get("cloud_init_option_ids"), list)
            else []
        )

        # Keep all existing node-ops fields while adding normalized contract fields.
        node_record = dict(node)
        node_record.update(
            {
                "in_node_ops_view": node_name in node_ops_by_name,
                "in_monitor_view": node_name in monitor_by_name,
                "source": str(node.get("source", "")).strip()
                or str(monitor_info.get("source", "")).strip()
                or ("node_ops" if node_name in node_ops_by_name else "monitor"),
                "callback_profile": callback_profile,
                "selected_profiles": selected_profiles,
                "provision_how": [profile_meta.get(pid, {"id": pid, "label": pid, "description": "", "tags": []}) for pid in selected_profiles],
                "latest_check": latest_check,
                "latest_check_steps": latest_check_steps,
                "cloud_init_option_ids": [str(v) for v in cloud_init_option_ids],
                "image": image_for_node,
                "cert": cert_info,
                "hardware": hardware_info,
            }
        )

        normalized_nodes.append(node_record)

    hardware_nodes = sum(1 for node in monitor_nodes if isinstance(node, dict) and isinstance(node.get("hardware"), dict) and node.get("hardware", {}).get("received_at"))
    node_ops_nodes = sum(1 for node in normalized_nodes if bool(node.get("in_node_ops_view", False)))
    masked_config = {
        "username": str(node_config.get("username", "")).strip(),
        "password_set": bool(node_config.get("password")),
        "webhook_url": str(node_config.get("webhook_url", "")).strip(),
        "verify_tls": bool(node_config.get("verify_tls", True)),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "nodes_in_monitor": len(monitor_nodes),
            "nodes_in_node_ops": node_ops_nodes,
            "nodes_with_hardware": hardware_nodes,
            "catalog_entries": int(image_summary.get("catalog_entries", 0) or 0),
            "catalog_available": int(image_summary.get("available_catalog_entries", 0) or 0),
            "image_history_records": int(image_summary.get("total_records", 0) or 0),
        },
        "images": {
            "summary": image_summary,
            "latest_by_profile": latest_image_by_profile,
        },
        "monitor": {
            "cloud_init_options": monitor_summary.get("cloud_init_options", []),
            "node_cloud_init": monitor_summary.get("node_cloud_init", {}),
        },
        "provisioning": {
            "config": masked_config,
            "node_inputs": _redact_node_profile_inputs(provision_state.get("node_inputs", {})),
            "manual_nodes": provision_state.get("manual_nodes", {}),
        },
        "nodes": normalized_nodes,
        # The Node Ops module forms are built from these specs; node records keep the lean metadata only.
        "profiles": [
            {
                **profile_meta[pid],
                "groups": [str(g) for g in (profile.get("groups") or [])],
                "input_specs": [dict(spec) for spec in _profile_input_specs(pid)],
            }
            for profile in NODE_PROVISION_PROFILES
            if isinstance(profile, dict)
            for pid in [str(profile.get("id", "")).strip()]
            if pid in profile_meta
        ],
        "sources": {
            "callbacks": str(DATA_FILE),
            "monitor_state": str(MONITOR_STATE_FILE),
            "node_provisioning": str(NODE_PROVISION_FILE),
            "image_history": str(IMAGE_HISTORY_FILE),
            "image_catalog": str(IMAGE_CATALOG_FILE),
            "inventory": str(INVENTORY_FILE),
        },
    }


def _trigger_initial_callback_closer(event: Dict[str, Any]) -> str:
    hostname = str(event.get("hostname", "") or "").strip()
    if not hostname:
        return ""

    prior_events = _read_events(limit=2000)
    has_prior_event = any(
        prior.get("received_at") != event.get("received_at")
        and (
            str(prior.get("hostname", "") or "").strip() == hostname
            or str(prior.get("fqdn", "") or "").strip() == hostname
        )
        for prior in prior_events
    )
    if has_prior_event:
        return ""

    profile = str(event.get("profile", "") or "").strip().lower()
    profile_group = _k3s_group_for_profile(profile)

    if not profile_group:
        return ""

    provision_state = _load_node_provisioning()
    node = {"name": hostname, "group": profile_group}
    profile_ids = _effective_node_profiles(node, provision_state)
    if not profile_ids:
        return ""

    logger.info(
        "Auto-triggering initial closer check for hostname=%s profiles=%s stage=%s",
        hostname,
        profile_ids,
        str(event.get("stage", "") or "").strip().lower() or "unknown",
    )
    return _start_node_provision_job([hostname], "check", True, profile_ids)
