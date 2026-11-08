"""Image history records and the image cache."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from services.ssh_keys import _resolve_download_path
from services.storage import IMAGE_HISTORY_FILE, _atomic_write_text, _ensure_image_history_file


def _append_image_history(entry: Dict[str, Any]) -> None:
    _ensure_image_history_file()
    line = json.dumps(entry, separators=(',', ':'))
    try:
        with IMAGE_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except PermissionError:
        existing_lines: List[str] = []
        if IMAGE_HISTORY_FILE.exists():
            try:
                existing_lines = IMAGE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
            except OSError:
                existing_lines = []
        _atomic_write_text(IMAGE_HISTORY_FILE, "\n".join([*existing_lines, line]) + "\n")


def _image_record_id(entry: Dict[str, Any], raw_line: str = "") -> str:
    seed = raw_line.strip() or json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _normalize_image_record(entry: Dict[str, Any], raw_line: str = "") -> Dict[str, Any]:
    record = dict(entry)
    record.setdefault("id", _image_record_id(record, raw_line=raw_line))
    record.setdefault("label", record.get("source_value") or record.get("download_path") or record.get("hostname") or "Retrieved image")
    record.setdefault("notes", "")
    record.setdefault("updated_at", record.get("recorded_at", ""))
    resolved_path = _resolve_download_path(str(record.get("download_path", "")))
    is_available = bool(resolved_path)
    if resolved_path:
        record["download_path"] = str(resolved_path)
    record["local_available"] = is_available
    record["availability"] = "available" if is_available else "missing"
    return record


def _write_image_history(entries: List[Dict[str, Any]]) -> None:
    _ensure_image_history_file()
    # Rewrite in one shot so history stays coherent even if the process gets bonked.
    rendered = "".join(json.dumps(entry, separators=(",", ":")) + "\n" for entry in entries)
    _atomic_write_text(IMAGE_HISTORY_FILE, rendered)


def _list_image_records(limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_image_history_file()
    lines = IMAGE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    entries: List[Dict[str, Any]] = []
    for line in selected:
        try:
            entries.append(_normalize_image_record(json.loads(line), raw_line=line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries


def _find_image_record(record_id: str) -> Dict[str, Any] | None:
    for record in _list_image_records(limit=1000):
        if record.get("id") == record_id:
                        return record
    return None


def _upsert_image_record(payload: Dict[str, Any], record_id: str | None = None) -> Dict[str, Any]:
    entries = _list_image_records(limit=5000)
    now = datetime.now(timezone.utc).isoformat()
    normalized = {
        "id": record_id or payload.get("id") or uuid.uuid4().hex[:12],
        "recorded_at": str(payload.get("recorded_at", "")).strip() or now,
        "updated_at": now,
        "profile": str(payload.get("profile", "")).strip(),
        "hostname": str(payload.get("hostname", "")).strip(),
        "source": str(payload.get("source", "")).strip(),
        "source_value": str(payload.get("source_value", "")).strip(),
        "downloaded": bool(payload.get("downloaded", False)),
        "download_path": str(payload.get("download_path", "")).strip(),
        "hash_status": str(payload.get("hash_status", "")).strip(),
        "expected_sha256": str(payload.get("expected_sha256", "")).strip().lower(),
        "observed_sha256": str(payload.get("observed_sha256", "")).strip().lower(),
        "job_status": str(payload.get("job_status", "")).strip(),
        "exit_code": payload.get("exit_code"),
        "label": str(payload.get("label", "")).strip() or "Retrieved image",
        "notes": str(payload.get("notes", "")).strip(),
    }

    replaced = False
    for idx, entry in enumerate(entries):
        if entry.get("id") == normalized["id"]:
            existing = dict(entry)
            existing.update(normalized)
            entries[idx] = existing
            replaced = True
            normalized = existing
            break

    if not replaced:
        entries.append(normalized)

    _write_image_history(entries)
    return normalized


def _delete_image_record(record_id: str) -> bool:
    entries = _list_image_records(limit=5000)
    kept = [entry for entry in entries if entry.get("id") != record_id]
    if len(kept) == len(entries):
        return False
    _write_image_history(kept)
    return True


def _read_image_history(limit: int = 200) -> List[Dict[str, Any]]:
    return _list_image_records(limit=limit)


def _compact_path_label(value: str) -> str:
    text = str(value or "").strip()
    if not text or "/" not in text:
        return text
    parts = [part for part in text.split("/") if part]
    if len(parts) < 2:
        return text
    return f"{parts[-2]}/{parts[-1]}"


def _record_image_history(form: Dict[str, Any], stdout: str, exit_code: int) -> None:
    image_url = str(form.get("image_url", "")).strip()
    image_file = str(form.get("image_file", "")).strip()
    expected_sha = str(form.get("image_sha256", "")).strip().lower()

    download_match = re.search(r"Download complete:\s*(.+)", stdout)
    cache_match = re.search(r"Cached image:\s*(.+)", stdout)
    source_match = re.search(r"Image source:\s*(.+)", stdout)
    verified_match = re.search(r"Image hash verified:\s*([A-Fa-f0-9]{64})", stdout)
    fail_expected_match = re.search(r"Expected:\s*([A-Fa-f0-9]{64})", stdout)
    fail_actual_match = re.search(r"Actual:\s*([A-Fa-f0-9]{64})", stdout)

    if verified_match:
        hash_status = "verified"
    elif "Image hash verification failed" in stdout:
        hash_status = "failed"
    elif "Hash verification skipped" in stdout:
        hash_status = "skipped"
    elif expected_sha:
        hash_status = "provided"
    else:
        hash_status = "unknown"

    source = "image_url" if image_url else "image_file" if image_file else "default"
    source_value = image_url or image_file or (source_match.group(1).strip() if source_match else "")
    record = {
        "id": uuid.uuid4().hex[:12],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "profile": str(form.get("profile", "")).strip(),
        "hostname": str(form.get("hostname", "")).strip(),
        "source": source,
        "source_value": source_value,
        "downloaded": bool(download_match),
        "download_path": cache_match.group(1).strip() if cache_match else image_file or (download_match.group(1).strip() if download_match else ""),
        "hash_status": hash_status,
        "expected_sha256": (fail_expected_match.group(1).lower() if fail_expected_match else expected_sha),
        "observed_sha256": (
            verified_match.group(1).lower()
            if verified_match
            else fail_actual_match.group(1).lower()
            if fail_actual_match
            else ""
        ),
        "job_status": "completed" if exit_code == 0 else "failed",
        "exit_code": exit_code,
    }
    _append_image_history(record)
