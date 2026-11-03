"""Operator settings (rack name, tailnet) stored in app_settings.json."""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from domain_defaults import TAILSCALE_POLICY_DEFAULTS
from tailscale_policy import board_info as tailscale_board_info

from services.storage import APP_SETTINGS_FILE, _atomic_write_text, _ensure_data_file


def _default_app_settings() -> Dict[str, Any]:
    return {
        "rack_name": "homelab",
        "tailscale_tailnet": str(TAILSCALE_POLICY_DEFAULTS.get("tailnet", "")).strip(),
        "tailscale_api_key": "",
    }


def _sanitize_rack_name(value: str) -> str:
    raw = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    return cleaned[:40] or "homelab"


def _sanitize_tailscale_tailnet(value: str) -> str:
    return str(value or "").strip().lower()


def _sanitize_tailscale_api_key(value: str) -> str:
    return str(value or "").strip()


def _load_app_settings() -> Dict[str, Any]:
    _ensure_data_file()
    defaults = _default_app_settings()
    if not APP_SETTINGS_FILE.exists():
        return defaults

    try:
        loaded = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defaults

    if not isinstance(loaded, dict):
        return defaults

    rack_name = _sanitize_rack_name(str(loaded.get("rack_name", defaults["rack_name"])))
    tailscale_tailnet = _sanitize_tailscale_tailnet(str(loaded.get("tailscale_tailnet", defaults["tailscale_tailnet"])))
    tailscale_api_key = _sanitize_tailscale_api_key(str(loaded.get("tailscale_api_key", defaults["tailscale_api_key"])))
    return {
        "rack_name": rack_name,
        "tailscale_tailnet": tailscale_tailnet,
        "tailscale_api_key": tailscale_api_key,
    }


def _save_app_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = _load_app_settings()
    if "rack_name" in payload:
        settings["rack_name"] = _sanitize_rack_name(str(payload.get("rack_name", "")))
    if "tailscale_tailnet" in payload:
        settings["tailscale_tailnet"] = _sanitize_tailscale_tailnet(str(payload.get("tailscale_tailnet", "")))
    if "tailscale_api_key" in payload:
        settings["tailscale_api_key"] = _sanitize_tailscale_api_key(str(payload.get("tailscale_api_key", "")))
    _ensure_data_file()
    _atomic_write_text(APP_SETTINGS_FILE, json.dumps(settings, indent=2))
    return settings


def _tailscale_policy_defaults() -> Dict[str, Any]:
    settings = _load_app_settings()
    rack_name = _sanitize_rack_name(str(settings.get("rack_name", "homelab")))
    defaults = dict(TAILSCALE_POLICY_DEFAULTS)
    defaults["rack_name"] = rack_name
    # The nodes advertise tag:homelab (SD script and Ansible group_vars), so the policy must use the same tag.
    # It used to be derived from the rack name, which produced a tag no node carried.
    defaults["tailnet"] = _sanitize_tailscale_tailnet(str(settings.get("tailscale_tailnet", defaults.get("tailnet", "")))) or str(defaults.get("tailnet", "")).strip()
    defaults["tailscale_api_key_set"] = bool(str(settings.get("tailscale_api_key", "")).strip())
    defaults["boards"] = tailscale_board_info()
    return defaults
