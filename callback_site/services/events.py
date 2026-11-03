"""Callback event store: append, dedupe, backfill and lookup."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from k3s_lightweight import ROLE_PROFILES as K3S_ROLE_PROFILES

from services.network import _is_transit_hop_ip, _normalize_ip
from services.node_state import _node_aliases
from services.storage import DATA_FILE, _ensure_data_file

logger = logging.getLogger(__name__)


def _append_event(event: Dict[str, Any]) -> None:
    _ensure_data_file()
    hostname = event.get("hostname", "unknown")
    stage = event.get("stage", "unknown")
    instance_id = event.get("instance_id", "unknown")
    logger.info(f"Callback received: hostname={hostname}, stage={stage}, instance_id={instance_id}, source_ip={event.get('source_ip')}")
    with DATA_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def _event_matches_identity(event: Dict[str, Any], hostname: str, instance_id: str, fqdn: str) -> bool:
    if hostname and str(event.get("hostname", "")).strip() == hostname:
        return True
    if instance_id and str(event.get("instance_id", "")).strip() == instance_id:
        return True
    if fqdn and str(event.get("fqdn", "")).strip() == fqdn:
        return True
    return False


def _parse_event_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _callback_event_fingerprint(event: Dict[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    seed = {
        "hostname": str(event.get("hostname", "")).strip(),
        "instance_id": str(event.get("instance_id", "")).strip(),
        "fqdn": str(event.get("fqdn", "")).strip(),
        "profile": str(event.get("profile", "")).strip().lower(),
        "stage": str(event.get("stage", "")).strip(),
        "source_ip": str(event.get("source_ip", "")).strip(),
        "source_ip_source": str(event.get("source_ip_source", "")).strip(),
        "cert_subject": str(event.get("cert_subject", "")).strip(),
        "cert_fingerprint": str(event.get("cert_fingerprint", "")).strip(),
        "raw": raw,
    }
    return json.dumps(seed, sort_keys=True, separators=(",", ":"))


def _is_duplicate_callback_event(event: Dict[str, Any], within_seconds: int = 15) -> bool:
    hostname = str(event.get("hostname", "")).strip()
    instance_id = str(event.get("instance_id", "")).strip()
    fqdn = str(event.get("fqdn", "")).strip()
    if not any((hostname, instance_id, fqdn)):
        return False

    current_ts = _parse_event_time(str(event.get("received_at", "")))
    current_fp = _callback_event_fingerprint(event)

    for prior in _read_events(limit=120):
        if not isinstance(prior, dict):
            continue
        if not _event_matches_identity(prior, hostname, instance_id, fqdn):
            continue
        if _callback_event_fingerprint(prior) != current_fp:
            continue
        prior_ts = _parse_event_time(str(prior.get("received_at", "")))
        if current_ts is None or prior_ts is None:
            return True
        delta = abs((current_ts - prior_ts).total_seconds())
        if delta <= within_seconds:
            return True
    return False


def _clear_events() -> int:
    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    DATA_FILE.write_text("", encoding="utf-8")
    return len(lines)


def _read_events(limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    events: List[Dict[str, Any]] = []
    for line in selected:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    events.reverse()
    return events


def _event_matches_node_aliases(event: Dict[str, Any], aliases: set[str]) -> bool:
    host = str(event.get("hostname", "")).strip().lower()
    fqdn = str(event.get("fqdn", "")).strip().lower()
    event_aliases: set[str] = set()
    event_aliases.update(_node_aliases(host))
    event_aliases.update(_node_aliases(fqdn))
    return bool(event_aliases.intersection(aliases))


def _keys_matching_aliases(mapping: Dict[str, Any], aliases: set[str]) -> List[str]:
    matched: List[str] = []
    for key in mapping.keys():
        key_aliases = _node_aliases(str(key or ""))
        if key_aliases.intersection(aliases):
            matched.append(str(key))
    return matched


def _backfill_event_profile(hostname: str, instance_id: str, fqdn: str, profile: str) -> int:
    profile_value = str(profile or "").strip().lower()
    if profile_value not in K3S_ROLE_PROFILES:
        return 0

    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0

    updated = 0
    rewritten: List[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        event_profile = str(event.get("profile", "")).strip().lower()
        if event_profile and event_profile != "unknown":
            rewritten.append(line)
            continue

        if not _event_matches_identity(event, hostname, instance_id, fqdn):
            rewritten.append(line)
            continue

        event["profile"] = profile_value
        event["profile_source"] = event.get("profile_source") or "backfilled"
        rewritten.append(json.dumps(event, separators=(",", ":")))
        updated += 1

    if updated:
        DATA_FILE.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return updated


def _backfill_event_source_ip(hostname: str, instance_id: str, fqdn: str, source_ip: str, source_ip_source: str) -> int:
    ip_value = _normalize_ip(source_ip)
    if not ip_value or _is_transit_hop_ip(ip_value):
        return 0

    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0

    updated = 0
    rewritten: List[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        if not _event_matches_identity(event, hostname, instance_id, fqdn):
            rewritten.append(line)
            continue

        current_ip = _normalize_ip(str(event.get("source_ip", "")))
        current_source = str(event.get("source_ip_source", "")).strip().lower()
        if current_ip == ip_value:
            rewritten.append(line)
            continue

        if current_source in {"remote_addr", ""} or _is_transit_hop_ip(current_ip):
            event["source_ip"] = ip_value
            event["source_ip_source"] = source_ip_source or "backfilled"
            rewritten.append(json.dumps(event, separators=(",", ":")))
            updated += 1
            continue

        rewritten.append(line)

    if updated:
        DATA_FILE.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return updated


def _backfill_event_fields(hostname: str, instance_id: str, fqdn: str, event_fields: Dict[str, Any]) -> int:
    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0

    incoming = {
        "hostname": str(event_fields.get("hostname", "")).strip(),
        "instance_id": str(event_fields.get("instance_id", "")).strip(),
        "fqdn": str(event_fields.get("fqdn", "")).strip(),
        "profile": str(event_fields.get("profile", "")).strip().lower(),
        "raw_profile": str(event_fields.get("raw_profile", "")).strip().lower(),
        "profile_source": str(event_fields.get("profile_source", "")).strip(),
        "stage": str(event_fields.get("stage", "")).strip(),
        "cert_subject": str(event_fields.get("cert_subject", "")).strip(),
        "cert_issuer": str(event_fields.get("cert_issuer", "")).strip(),
        "cert_fingerprint": str(event_fields.get("cert_fingerprint", "")).strip(),
        "node_report_id": str(event_fields.get("node_report_id", "")).strip(),
        "source_ip_source": str(event_fields.get("source_ip_source", "")).strip(),
    }
    incoming_cert_present = bool(event_fields.get("cert_present", False))

    updated = 0
    rewritten: List[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rewritten.append(line)
            continue

        if not _event_matches_identity(event, hostname, instance_id, fqdn):
            rewritten.append(line)
            continue

        changed = False

        for key in ("hostname", "instance_id", "fqdn", "cert_subject", "cert_issuer", "cert_fingerprint", "node_report_id"):
            current = str(event.get(key, "")).strip()
            if not current and incoming.get(key):
                event[key] = incoming[key]
                changed = True

        current_profile = str(event.get("profile", "")).strip().lower()
        if incoming["profile"] in K3S_ROLE_PROFILES and current_profile in {"", "unknown"}:
            event["profile"] = incoming["profile"]
            changed = True

        current_raw_profile = str(event.get("raw_profile", "")).strip().lower()
        if not current_raw_profile and incoming["raw_profile"]:
            event["raw_profile"] = incoming["raw_profile"]
            changed = True

        current_profile_source = str(event.get("profile_source", "")).strip()
        if not current_profile_source and incoming["profile_source"]:
            event["profile_source"] = incoming["profile_source"]
            changed = True

        current_stage = str(event.get("stage", "")).strip()
        if incoming["stage"] and current_stage in {"", "phone_home"}:
            event["stage"] = incoming["stage"]
            changed = True

        current_source_ip_source = str(event.get("source_ip_source", "")).strip()
        if not current_source_ip_source and incoming["source_ip_source"]:
            event["source_ip_source"] = incoming["source_ip_source"]
            changed = True

        if incoming_cert_present and not bool(event.get("cert_present", False)):
            event["cert_present"] = True
            changed = True

        if changed:
            rewritten.append(json.dumps(event, separators=(",", ":")))
            updated += 1
        else:
            rewritten.append(line)

    if updated:
        DATA_FILE.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
    return updated


def _store_callback_event(event: Dict[str, Any]) -> bool:
    hostname = str(event.get("hostname", "")).strip()
    instance_id = str(event.get("instance_id", "")).strip()
    fqdn = str(event.get("fqdn", "")).strip()
    profile = str(event.get("profile", "")).strip().lower()
    source_ip = str(event.get("source_ip", "")).strip()
    source_ip_source = str(event.get("source_ip_source", "")).strip()

    deduped = _is_duplicate_callback_event(event)
    if not deduped:
        _append_event(event)

    _backfill_event_profile(hostname, instance_id, fqdn, profile)
    _backfill_event_source_ip(hostname, instance_id, fqdn, source_ip, source_ip_source)
    _backfill_event_fields(hostname, instance_id, fqdn, event)
    return deduped
