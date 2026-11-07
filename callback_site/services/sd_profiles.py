"""Saved SD-card option profiles and node profile segments."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from network_toolkit import PROFILE as TOOLKIT_PROFILE, SECRET_OPTIONS as TOOLKIT_SECRETS

from services.storage import (
    PROFILE_SEGMENTS_FILE,
    SD_OPTION_PROFILES_FILE,
    SD_OPTION_PROFILE_STATE_FILE,
    _atomic_write_text,
    _ensure_data_file,
)


def _load_sd_option_profiles() -> Dict[str, Dict[str, Any]]:
    _ensure_data_file()
    if not SD_OPTION_PROFILES_FILE.exists():
        return {}
    try:
        loaded = json.loads(SD_OPTION_PROFILES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for name, profile in loaded.items():
        profile_name = str(name).strip()
        if not profile_name or not isinstance(profile, dict):
            continue
        options = profile.get("options") if isinstance(profile.get("options"), dict) else {}
        distribution = str(profile.get("distribution", "standard")).strip().lower() or "standard"
        if distribution not in {"standard", "k3s"}:
            distribution = "standard"
        out[profile_name] = {
            "name": profile_name,
            "description": str(profile.get("description", "")).strip(),
            "distribution": distribution,
            "pi_only": bool(profile.get("pi_only", False)),
            "options": {key: value for key, value in options.items() if key not in TOOLKIT_SECRETS},
            "updated_at": str(profile.get("updated_at", "")).strip(),
        }
        if profile.get("profile") == TOOLKIT_PROFILE:
            out[profile_name]["profile"] = TOOLKIT_PROFILE
    return out


def _save_sd_option_profiles(profiles: Dict[str, Dict[str, Any]]) -> None:
    _ensure_data_file()
    _atomic_write_text(SD_OPTION_PROFILES_FILE, json.dumps(profiles, indent=2))


def _load_sd_option_profile_state() -> Dict[str, Any]:
    _ensure_data_file()
    if not SD_OPTION_PROFILE_STATE_FILE.exists():
        return {}
    try:
        loaded = json.loads(SD_OPTION_PROFILE_STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    preferred_profile = str(loaded.get("preferred_profile", "")).strip()
    return {"preferred_profile": preferred_profile}


def _save_sd_option_profile_state(state: Dict[str, Any]) -> None:
    _ensure_data_file()
    preferred_profile = str(state.get("preferred_profile", "")).strip()
    payload = {"preferred_profile": preferred_profile}
    _atomic_write_text(SD_OPTION_PROFILE_STATE_FILE, json.dumps(payload, indent=2))


def _set_preferred_sd_option_profile(profile_name: str) -> None:
    preferred = str(profile_name or "").strip()
    _save_sd_option_profile_state({"preferred_profile": preferred})


def _ordered_sd_option_profile_names(profiles: Dict[str, Dict[str, Any]]) -> List[str]:
    preferred = str(_load_sd_option_profile_state().get("preferred_profile", "")).strip()

    def _updated_ts(name: str) -> float:
        updated_at = str((profiles.get(name) or {}).get("updated_at", "")).strip()
        if not updated_at:
            return 0.0
        try:
            return datetime.fromisoformat(updated_at).timestamp()
        except ValueError:
            return 0.0

    names = list(profiles.keys())
    names.sort(key=lambda name: (-_updated_ts(name), name.lower()))
    if preferred and preferred in profiles:
        return [preferred] + [name for name in names if name != preferred]
    return names


def _ordered_sd_option_profiles(profiles: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [profiles[name] for name in _ordered_sd_option_profile_names(profiles)]


def _load_profile_segments() -> Dict[str, List[Dict[str, Any]]]:
    _ensure_data_file()
    if not PROFILE_SEGMENTS_FILE.exists():
        return {}
    try:
        loaded = json.loads(PROFILE_SEGMENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}

    out: Dict[str, List[Dict[str, Any]]] = {}
    for profile_id, value in loaded.items():
        pid = str(profile_id).strip()
        if not pid:
            continue
        if isinstance(value, list):
            segments = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                segments.append(
                    {
                        "name": str(item.get("name", "")).strip() or "segment",
                        "config_block": str(item.get("config_block", "")).strip(),
                        "defaults_json": str(item.get("defaults_json", "")).strip(),
                        "galaxy_requirements": [str(v).strip() for v in item.get("galaxy_requirements", []) if str(v).strip()],
                        "updated_at": str(item.get("updated_at", "")).strip(),
                    }
                )
            out[pid] = segments
    return out


def _save_profile_segments(segments: Dict[str, List[Dict[str, Any]]]) -> None:
    _ensure_data_file()
    _atomic_write_text(PROFILE_SEGMENTS_FILE, json.dumps(segments, indent=2))


def _upsert_profile_segment(profile_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    pid = str(profile_id).strip()
    if not pid:
        raise ValueError("profile_id is required")

    segments = _load_profile_segments()
    profile_segments = segments.setdefault(pid, [])
    name = str(payload.get("segment_name", "")).strip() or str(payload.get("name", "")).strip() or "segment"
    if not name:
        raise ValueError("segment_name is required")

    entry = {
        "name": name,
        "config_block": str(payload.get("config_block", "")).strip(),
        "defaults_json": str(payload.get("defaults_json", "")).strip(),
        "galaxy_requirements": [str(v).strip() for v in payload.get("galaxy_requirements", []) if str(v).strip()],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    existing_index = next((idx for idx, item in enumerate(profile_segments) if str(item.get("name", "")).strip() == name), None)
    if existing_index is None:
        profile_segments.append(entry)
    else:
        profile_segments[existing_index] = entry

    _save_profile_segments(segments)
    return entry
