"""Catalog pull and image cache management."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib import request as urlrequest
from urllib import error as urlerror

logger = logging.getLogger(__name__)


def _safe_catalog_filename(url: str, entry_id: str) -> str:
    """Generate a safe local filename from a URL and entry ID."""
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:8]
    sanitized = "".join(c if c.isalnum() or c in "-_." else "_" for c in entry_id)
    return f"{sanitized}-{url_hash}.img.xz"


def _download_catalog_image(url: str, target: Path) -> str:
    """Download catalog image and return SHA256 hex digest."""
    logger.info(f"Downloading catalog image: {url[:60]}... to {target.name}")
    hash_obj = hashlib.sha256()
    
    try:
        with urlrequest.urlopen(url, timeout=60) as response:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                target.write_bytes(target.read_bytes() + chunk if target.exists() else b"")
                hash_obj.update(chunk)
    except (urlerror.URLError, Exception) as e:
        logger.error(f"Download failed for {url}: {e}")
        raise
    
    digest = hash_obj.hexdigest()
    logger.info(f"Download complete: {target.name}, SHA256={digest[:16]}...")
    return digest


def _catalog_entry_with_availability(
    entry: Dict[str, Any], available_urls: set[str] | None = None
) -> Dict[str, Any]:
    """Enrich a catalog entry with local availability info."""
    image_url = str(entry.get("image_url", "")).strip()
    entry_id = str(entry.get("id", ""))
    
    if not image_url:
        return {**entry, "local_available": False, "cache_path": ""}
    
    if available_urls is None:
        available_urls = set()
    
    filename = _safe_catalog_filename(image_url, entry_id)
    
    # Import here to avoid circular imports
    from core import IMAGE_CACHE_DIR
    
    cache_path = IMAGE_CACHE_DIR / filename
    is_available = image_url in available_urls or cache_path.exists()
    
    return {
        **entry,
        "local_available": is_available,
        "cache_path": str(cache_path),
    }


def _available_image_urls_from_history(limit: int = 5000) -> set[str]:
    """Get set of successfully downloaded image URLs from history."""
    from core import _read_image_history
    
    urls = set()
    for record in _read_image_history(limit=limit):
        if record.get("status") == "ok" and record.get("image_url"):
            urls.add(record["image_url"])
    return urls


def _run_catalog_pull_job(
    job_id: str, entries: List[Dict[str, Any]], skip_existing: bool
) -> None:
    """Execute catalog pull job in background."""
    from core import (
        CATALOG_PULL_JOBS,
        CATALOG_PULL_JOBS_LOCK,
        IMAGE_CACHE_DIR,
        _ensure_image_cache_dir,
        _append_image_history,
    )
    
    _ensure_image_cache_dir()
    logger.info(f"Catalog pull job started: job_id={job_id}, entries={len(entries)}, skip_existing={skip_existing}")
    
    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            logger.warning(f"Catalog pull job {job_id} not found in CATALOG_PULL_JOBS")
            return
        job["status"] = "running"
        job["started_at"] = datetime.now(timezone.utc).isoformat()

    for entry in entries:
        entry_id = str(entry.get("id", ""))
        label = str(entry.get("label", entry_id))
        image_url = str(entry.get("image_url", "")).strip()
        expected_sha = str(entry.get("image_sha256", "")).strip().lower()
        
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
        }

        with CATALOG_PULL_JOBS_LOCK:
            job = CATALOG_PULL_JOBS.get(job_id)
            if job:
                job.setdefault("results", []).append(item_result)

        filename = _safe_catalog_filename(image_url, entry_id)
        target = IMAGE_CACHE_DIR / filename

        try:
            if target.exists() and skip_existing:
                item_result["status"] = "skipped"
                item_result["download_path"] = str(target)
                item_result["message"] = "already exists"
                continue

            tmp = target.with_suffix(target.suffix + ".part")
            observed_sha = _download_catalog_image(image_url, tmp)
            tmp.rename(target)

            ok = (not expected_sha) or (observed_sha.lower() == expected_sha)
            item_result["status"] = "ok" if ok else "failed"
            item_result["observed_sha256"] = observed_sha
            item_result["download_path"] = str(target)
            item_result["message"] = "ok" if ok else f"SHA mismatch: {observed_sha[:16]}... != {expected_sha[:16]}..."

            _append_image_history(
                {
                    "id": entry_id,
                    "label": label,
                    "image_url": image_url,
                    "status": item_result["status"],
                    "observed_sha256": observed_sha,
                    "expected_sha256": expected_sha,
                    "download_path": str(target),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        except Exception as e:
            item_result["status"] = "failed"
            item_result["message"] = str(e)
            logger.error(f"Catalog pull failed for {entry_id}: {e}")

        # Update job summary after each item
        with CATALOG_PULL_JOBS_LOCK:
            job = CATALOG_PULL_JOBS.get(job_id)
            if job:
                results = job.get("results", [])
                running = sum(1 for r in results if r.get("status") == "downloading")
                failed = sum(1 for r in results if r.get("status") == "failed")
                skipped = sum(1 for r in results if r.get("status") == "skipped")
                ok = sum(1 for r in results if r.get("status") == "ok")
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
