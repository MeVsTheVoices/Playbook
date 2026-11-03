"""Image catalog availability and background catalog pulls."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib import parse as urlparse, request as urlrequest

from domain_defaults import IMAGE_CATALOG

from services.images import (
    _append_image_history,
    _compact_path_label,
    _list_image_records,
    _read_image_history,
)
from services.storage import IMAGE_CACHE_DIR, _ensure_image_cache_dir

logger = logging.getLogger(__name__)


CATALOG_PULL_JOBS: Dict[str, Dict[str, Any]] = {}


CATALOG_PULL_JOBS_LOCK = threading.Lock()


def _image_state_summary() -> Dict[str, Any]:
    history = _read_image_history(limit=500)
    available_urls = _available_image_urls_from_history(limit=5000)
    catalog_available = sum(
        1
        for entry in IMAGE_CATALOG
        if _catalog_entry_with_availability(entry, available_urls=available_urls).get("local_available")
    )
    latest = history[0] if history else {}
    latest_label = str(latest.get("label") or latest.get("source_value") or latest.get("download_path") or "-")
    latest_status = str(latest.get("job_status") or latest.get("hash_status") or "-")
    latest_recorded_at = str(latest.get("recorded_at") or "")

    # The newest record is not always the best cache-state signal (for example,
    # records captured before cache path resolution). Prefer the most recent
    # record that has a known local cache path or local-available flag.
    state_entry = latest
    for record in history:
        record_cache_path = str(record.get("download_path") or record.get("cache_path") or "").strip()
        if record_cache_path or bool(record.get("local_available", False)):
            state_entry = record
            break

    latest_available = bool(state_entry.get("local_available", False))
    cache_path = str(state_entry.get("download_path") or state_entry.get("cache_path") or "")
    latest_state_emoji = "🎉" if latest_available else "🕳️"
    latest_state_text = "Cached and ready" if latest_available else "No cached image recorded yet"

    return {
        "total_records": len(history),
        "latest_label": latest_label,
        "latest_status": latest_status,
        "latest_recorded_at": latest_recorded_at,
        "latest_available": latest_available,
        "latest_cache_path": cache_path,
        "latest_cache_path_short": _compact_path_label(cache_path),
        "latest_state_emoji": latest_state_emoji,
        "latest_state_text": latest_state_text,
        "available_catalog_entries": catalog_available,
        "catalog_entries": len(IMAGE_CATALOG),
    }


def _safe_catalog_filename(url: str, entry_id: str) -> str:
    parsed = urlparse.urlparse(url)
    name = Path(parsed.path).name or f"{entry_id}.img"
    safe = re.sub(r"[^A-Za-z0-9._+-]+", "-", name).strip(".-")
    if not safe:
        safe = f"{entry_id}.img"
    return f"{entry_id}-{safe}"


def _catalog_pull_temp_path(target: Path) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=f"{target.name}.", suffix=".part", dir=str(target.parent))
    os.close(fd)
    return Path(temp_name)


def _download_catalog_image(
    url: str,
    target: Path,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> str:
    req = urlrequest.Request(url, headers={"User-Agent": "callback-site-catalog-pull/1.0"})
    digest = hashlib.sha256()
    with urlrequest.urlopen(req, timeout=60) as resp, target.open("wb") as dst:  # nosec B310
        total_bytes_raw = resp.headers.get("Content-Length")
        total_bytes: int | None = None
        if total_bytes_raw:
            try:
                total_bytes = int(total_bytes_raw)
            except (TypeError, ValueError):
                total_bytes = None

        downloaded_bytes = 0
        if on_progress:
            on_progress(downloaded_bytes, total_bytes)

        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
            downloaded_bytes += len(chunk)
            if on_progress:
                on_progress(downloaded_bytes, total_bytes)
    return digest.hexdigest()


def _catalog_entry_with_availability(entry: Dict[str, Any], available_urls: set[str] | None = None) -> Dict[str, Any]:
    enriched = dict(entry)
    image_url = str(entry.get("image_url", "")).strip()
    if not image_url:
        enriched["local_available"] = False
        enriched["cache_path"] = ""
        return enriched

    _ensure_image_cache_dir()
    entry_id = str(entry.get("id", "entry"))
    cache_path = IMAGE_CACHE_DIR / _safe_catalog_filename(image_url, entry_id)
    has_cache_file = cache_path.exists()
    has_history_file = bool(available_urls and image_url in available_urls)
    
    # Also check managed images for any record with this URL that has local_available=True
    has_managed_file = False
    if not has_cache_file and not has_history_file:
        for record in _list_image_records(limit=500):
            if record.get("source_value") == image_url and record.get("local_available"):
                has_managed_file = True
                break
    
    enriched["local_available"] = has_cache_file or has_history_file or has_managed_file
    enriched["cache_path"] = str(cache_path)
    return enriched


def _available_image_urls_from_history(limit: int = 5000) -> set[str]:
    available: set[str] = set()
    for record in _list_image_records(limit=limit):
        if not bool(record.get("local_available", False)):
            continue
        source_value = str(record.get("source_value", "")).strip()
        if source_value.startswith("http://") or source_value.startswith("https://"):
            available.add(source_value)
    return available


def _run_catalog_pull_job(job_id: str, entries: List[Dict[str, Any]], skip_existing: bool) -> None:
    _ensure_image_cache_dir()
    logger.info(f"Catalog pull job started: job_id={job_id}, entries={len(entries)}, skip_existing={skip_existing}")
    
    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            logger.warning(f"Catalog pull job {job_id} not found in CATALOG_PULL_JOBS")
            return
        job["status"] = "running"
        job["started_at"] = datetime.now(timezone.utc).isoformat()
        job["summary"] = {
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "running": len(entries),
            "total": len(entries),
        }

    for entry in entries:
        entry_id = str(entry.get("id", ""))
        label = str(entry.get("label", entry_id))
        image_url = str(entry.get("image_url", "")).strip()
        expected_sha = str(entry.get("image_sha256", "")).strip().lower()
        profile_value = ",".join(entry.get("profiles", [])) if isinstance(entry.get("profiles"), list) else ""
        
        logger.debug(f"Catalog pull: processing entry {entry_id} ({label}) from {image_url[:50]}...")

        item_result: Dict[str, Any] = {
            "id": entry_id,
            "label": label,
            "url": image_url,
            "status": "downloading",
            "download_path": "",
            "observed_sha256": "",
            "expected_sha256": expected_sha,
            "message": "",
            "bytes_downloaded": 0,
            "bytes_total": None,
            "progress_percent": 0,
        }

        with CATALOG_PULL_JOBS_LOCK:
            job = CATALOG_PULL_JOBS.get(job_id)
            if job:
                job.setdefault("results", []).append(item_result)

        filename = _safe_catalog_filename(image_url, entry_id)
        target = IMAGE_CACHE_DIR / filename
        tmp: Path | None = None

        try:
            if target.exists() and skip_existing:
                item_result["status"] = "skipped"
                item_result["download_path"] = str(target)
                item_result["message"] = "already exists"
                continue

            for stale_part in sorted(IMAGE_CACHE_DIR.glob(f"{target.name}*.part")):
                try:
                    stale_part.unlink(missing_ok=True)
                except OSError:
                    logger.warning(f"Unable to remove stale catalog temp file: {stale_part}")

            tmp = _catalog_pull_temp_path(target)

            def _on_item_progress(downloaded: int, total: int | None) -> None:
                percent = 0
                if total and total > 0:
                    percent = int(max(0, min(100, round((downloaded / total) * 100))))
                with CATALOG_PULL_JOBS_LOCK:
                    active_job = CATALOG_PULL_JOBS.get(job_id)
                    if not active_job:
                        return
                    for result in active_job.get("results", []):
                        if result is item_result:
                            result["bytes_downloaded"] = downloaded
                            result["bytes_total"] = total
                            result["progress_percent"] = percent
                            break

            observed_sha = _download_catalog_image(image_url, tmp, on_progress=_on_item_progress)
            tmp.replace(target)

            hash_status = "verified" if expected_sha and observed_sha == expected_sha else "downloaded"
            if expected_sha and observed_sha != expected_sha:
                hash_status = "failed"

            item_result["status"] = "ok" if hash_status != "failed" else "failed"
            item_result["download_path"] = str(target)
            item_result["observed_sha256"] = observed_sha
            item_result["message"] = hash_status
            item_result["progress_percent"] = 100

            _append_image_history(
                {
                    "id": uuid.uuid4().hex[:12],
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "profile": profile_value,
                    "hostname": "catalog-pull",
                    "source": "catalog_pull",
                    "source_value": image_url,
                    "downloaded": True,
                    "download_path": str(target),
                    "hash_status": hash_status,
                    "expected_sha256": expected_sha,
                    "observed_sha256": observed_sha,
                    "job_status": "completed" if hash_status != "failed" else "failed",
                    "exit_code": 0 if hash_status != "failed" else 1,
                    "label": label,
                    "notes": "Manual catalog pull",
                }
            )
        except Exception as exc:
            item_result["status"] = "failed"
            item_result["message"] = str(exc)
            try:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass

        with CATALOG_PULL_JOBS_LOCK:
            job = CATALOG_PULL_JOBS.get(job_id)
            if job:
                results = job.get("results", [])
                failed = sum(1 for r in results if r.get("status") == "failed")
                skipped = sum(1 for r in results if r.get("status") == "skipped")
                ok = sum(1 for r in results if r.get("status") == "ok")
                running = sum(1 for r in results if r.get("status") == "downloading")
                job["summary"] = {
                    "ok": ok,
                    "failed": failed,
                    "skipped": skipped,
                    "running": running,
                    "total": len(entries),
                }

    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            return
        results = job.get("results", [])
        failed = sum(1 for r in results if r.get("status") == "failed")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        ok = sum(1 for r in results if r.get("status") == "ok")
        job["summary"] = {"ok": ok, "failed": failed, "skipped": skipped, "running": 0, "total": len(entries)}
        job["status"] = "completed_with_errors" if failed else "completed"
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(f"Catalog pull job completed: job_id={job_id}, ok={ok}, failed={failed}, skipped={skipped}")
