"""Normalisation helpers for incoming cloud-init callback payloads."""

from __future__ import annotations

from typing import Any, Dict

from k3s_lightweight import ROLE_PROFILES as K3S_ROLE_PROFILES
from network_toolkit import PROFILE as TOOLKIT_PROFILE
from playbook_server import PROFILE as SERVER_PROFILE

from services.events import _read_events


def _normalize_stage(payload: Dict[str, Any]) -> str:
    for key in ("stage", "cloud_init_stage", "status", "phase", "event"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return "phone_home"


def _infer_profile_from_text(*values: str) -> str:
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if TOOLKIT_PROFILE in text or "network_toolkit" in text:
            return TOOLKIT_PROFILE
        if SERVER_PROFILE in text or "playbook_server" in text:
            return SERVER_PROFILE
        if "pi3bplus" in text or "pi-3bplus" in text or "pi3b+" in text or "pi-3b+" in text or "3bplus" in text:
            return "pi3bplus"
        if "pi4" in text or "pi-4" in text or "rpi4" in text:
            return "pi4"
        if "pi5" in text or "pi-5" in text:
            return "pi5"
    return ""


def _extract_payload_profile(payload: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(payload, dict):
        for key in (
            "profile",
            "profile_name",
            "image_profile",
            "rack_profile",
            "role",
            "group",
            "ansible_group",
            "provision_profile",
        ):
            value = str(payload.get(key, "")).strip().lower()
            if value in (*K3S_ROLE_PROFILES, TOOLKIT_PROFILE, SERVER_PROFILE):
                return value
            inferred = _infer_profile_from_text(value)
            if inferred:
                return inferred
        for value in payload.values():
            inferred = _extract_payload_profile(value, depth + 1)
            if inferred:
                return inferred
        return ""
    if isinstance(payload, list):
        for item in payload:
            inferred = _extract_payload_profile(item, depth + 1)
            if inferred:
                return inferred
        return ""
    if isinstance(payload, str):
        return _infer_profile_from_text(payload)
    return ""


def _normalize_profile(payload: Dict[str, Any], hostname: str, instance_id: str) -> str:
    payload_profile = _extract_payload_profile(payload)
    if payload_profile:
        return payload_profile

    inferred = _infer_profile_from_text(hostname, instance_id, payload.get("fqdn", ""))
    if inferred:
        return inferred
    return "unknown"


def _normalize_hostname(payload: Dict[str, Any]) -> str:
    for key in ("hostname", "host", "node", "nodename"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _normalize_instance_id(payload: Dict[str, Any]) -> str:
    for key in ("instance_id", "instance", "instanceid"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _recent_event_defaults(hostname: str, instance_id: str, fqdn: str) -> Dict[str, str]:
    for event in _read_events(limit=500):
        if not isinstance(event, dict):
            continue

        matches = False
        if hostname and str(event.get("hostname", "")).strip() == hostname:
            matches = True
        if instance_id and str(event.get("instance_id", "")).strip() == instance_id:
            matches = True
        if fqdn and str(event.get("fqdn", "")).strip() == fqdn:
            matches = True
        if not matches:
            continue

        profile = str(event.get("profile", "")).strip().lower()
        stage = str(event.get("stage", "")).strip()
        return {
            "profile": profile if profile else "",
            "stage": stage if stage else "",
        }

    return {"profile": "", "stage": ""}
