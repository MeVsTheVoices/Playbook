"""Callback event and image history management."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _append_event(event: Dict[str, Any], data_file: Path) -> None:
    """Append callback event to JSONL file."""
    hostname = event.get("hostname", "unknown")
    stage = event.get("stage", "unknown")
    instance_id = event.get("instance_id", "unknown")
    logger.info(f"Callback received: hostname={hostname}, stage={stage}, instance_id={instance_id}, source_ip={event.get('source_ip')}")
    with data_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def _clear_events(data_file: Path) -> int:
    """Clear all callback events."""
    lines = data_file.read_text(encoding="utf-8").splitlines()
    data_file.write_text("", encoding="utf-8")
    return len(lines)


def _read_events(data_file: Path, limit: int = 200) -> List[Dict[str, Any]]:
    """Read callback events from JSONL file in reverse order."""
    if not data_file.exists():
        return []
    
    lines = data_file.read_text(encoding="utf-8").splitlines()
    events = []
    for line in reversed(lines[-limit:]):
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _append_image_history(entry: Dict[str, Any], image_history_file: Path) -> None:
    """Append image download record to history."""
    image_history_file.parent.mkdir(parents=True, exist_ok=True)
    with image_history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _image_record_id(entry: Dict[str, Any], raw_line: str = "") -> str:
    """Generate unique ID for image record."""
    return entry.get("id") or f"manual-{entry.get('label', 'unknown')}"


def _normalize_image_record(entry: Dict[str, Any], raw_line: str = "") -> Dict[str, Any]:
    """Normalize image record fields."""
    return {
        "id": _image_record_id(entry, raw_line),
        "label": str(entry.get("label", "")).strip(),
        "image_url": str(entry.get("image_url", "")).strip(),
        "status": str(entry.get("status", "unknown")).strip(),
        "observed_sha256": str(entry.get("observed_sha256", "")).strip().lower(),
        "expected_sha256": str(entry.get("expected_sha256", "")).strip().lower(),
        "download_path": str(entry.get("download_path", "")).strip(),
        "created_at": str(entry.get("created_at", "")).strip() or datetime.now(timezone.utc).isoformat(),
    }


def _write_image_history(entries: List[Dict[str, Any]], image_history_file: Path) -> None:
    """Write image history records."""
    image_history_file.parent.mkdir(parents=True, exist_ok=True)
    with image_history_file.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(_normalize_image_record(entry), separators=(",", ":")) + "\n")


def _list_image_records(image_history_file: Path, limit: int = 200) -> List[Dict[str, Any]]:
    """List all managed image records (created manually, not catalog pulls)."""
    if not image_history_file.exists():
        return []
    
    records = {}
    for line in image_history_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entry = json.loads(line)
                record_id = _image_record_id(entry, line)
                records[record_id] = _normalize_image_record(entry, line)
            except json.JSONDecodeError:
                pass
    
    return list(records.values())[-limit:]


def _find_image_record(record_id: str, image_history_file: Path) -> Dict[str, Any] | None:
    """Find single image record by ID."""
    for record in _list_image_records(image_history_file):
        if record["id"] == record_id:
            return record
    return None


def _upsert_image_record(
    payload: Dict[str, Any], record_id: str | None = None, image_history_file: Path = None
) -> Dict[str, Any]:
    """Create or update image record."""
    normalized = _normalize_image_record(payload)
    
    if not record_id:
        record_id = normalized["id"]
    else:
        normalized["id"] = record_id
    
    records = {r["id"]: r for r in _list_image_records(image_history_file, limit=5000)}
    records[record_id] = normalized
    
    _write_image_history(list(records.values()), image_history_file)
    return normalized


def _delete_image_record(record_id: str, image_history_file: Path) -> bool:
    """Delete image record by ID."""
    records = {r["id"]: r for r in _list_image_records(image_history_file, limit=5000)}
    if record_id not in records:
        return False
    
    records.pop(record_id, None)
    _write_image_history(list(records.values()), image_history_file)
    return True


def _read_image_history(image_history_file: Path, limit: int = 200) -> List[Dict[str, Any]]:
    """Read image history in reverse chronological order."""
    if not image_history_file.exists():
        return []
    
    records = []
    for line in reversed(image_history_file.read_text(encoding="utf-8").splitlines()[-limit:]):
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _image_state_summary(image_history_file: Path) -> Dict[str, Any]:
    """Summarize image state from history."""
    records = _read_image_history(image_history_file, limit=500)
    
    return {
        "total": len(records),
        "ok": sum(1 for r in records if r.get("status") == "ok"),
        "failed": sum(1 for r in records if r.get("status") == "failed"),
        "skipped": sum(1 for r in records if r.get("status") == "skipped"),
    }
