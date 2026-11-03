"""Hardware report storage for the per-node report pages."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import uuid
from typing import Any, Dict

from services.storage import APPDATA_DIR

logger = logging.getLogger(__name__)

NODE_REPORTS_DIR = APPDATA_DIR / "node_reports"
NODE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _is_valid_report_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{12,32}", str(value or "").strip().lower()))


def _decode_node_report_payload(payload: Dict[str, Any]) -> Any:
    raw = payload.pop("node_report", None)
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    report_b64 = str(payload.pop("node_report_b64", "")).strip()
    if not report_b64:
        return None
    try:
        decoded = base64.b64decode(report_b64).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return {"decode_error": True}
    decoded = decoded.strip()
    if not decoded:
        return None
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        return {"text": decoded}


def _store_node_report(report_payload: Any, event: Dict[str, Any]) -> str:
    if report_payload is None:
        return ""

    report_id = uuid.uuid4().hex[:12]
    report_record = {
        "id": report_id,
        "received_at": event.get("received_at", ""),
        "hostname": event.get("hostname", ""),
        "instance_id": event.get("instance_id", ""),
        "profile": event.get("profile", ""),
        "stage": event.get("stage", ""),
        "source_ip": event.get("source_ip", ""),
        "report": report_payload,
    }
    report_file = NODE_REPORTS_DIR / f"{report_id}.json"
    try:
        report_file.write_text(json.dumps(report_record, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Unable to store node report for hostname=%s: %s", event.get("hostname", ""), exc)
        return ""
    return report_id


def _read_node_report(report_id: str) -> Dict[str, Any] | None:
    rid = str(report_id or "").strip().lower()
    if not _is_valid_report_id(rid):
        return None
    report_file = NODE_REPORTS_DIR / f"{rid}.json"
    if not report_file.exists():
        return None
    try:
        payload = json.loads(report_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload
