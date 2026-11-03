"""Image catalog and image library endpoints."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from flask import jsonify, request

from domain_defaults import IMAGE_CATALOG

from services.image_catalog import (
    CATALOG_PULL_JOBS,
    CATALOG_PULL_JOBS_LOCK,
    _available_image_urls_from_history,
    _catalog_entry_with_availability,
    _run_catalog_pull_job,
)
from services.images import (
    _delete_image_record,
    _find_image_record,
    _read_image_history,
    _upsert_image_record,
)
from services.storage import IMAGE_CACHE_DIR, _ensure_image_cache_dir


def catalog_entries_for_ui() -> List[Dict[str, Any]]:
    available_urls = _available_image_urls_from_history(limit=5000)
    return [_catalog_entry_with_availability(entry, available_urls=available_urls) for entry in IMAGE_CATALOG]


def register_image_routes(app: Any) -> None:
    @app.get("/api/sd/image-catalog")
    def sd_image_catalog() -> Any:
        return jsonify({"ok": True, "entries": catalog_entries_for_ui()})

    @app.get("/api/sd/catalog")
    def sd_catalog() -> Any:
        return jsonify({"ok": True, "entries": catalog_entries_for_ui()})

    @app.post("/api/sd/catalog-pull")
    def sd_catalog_pull() -> Any:
        payload = request.get_json(silent=True) or {}
        raw_ids = payload.get("catalog_ids") or []
        skip_existing = bool(payload.get("skip_existing", True))

        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "catalog_ids must be an array"}), 400

        requested_ids = [str(v).strip() for v in raw_ids if str(v).strip()]
        if not requested_ids:
            return jsonify({"ok": False, "error": "at least one catalog id is required"}), 400

        by_id = {str(e.get("id", "")): e for e in IMAGE_CATALOG}
        selected: List[Dict[str, Any]] = []
        missing: List[str] = []
        no_url: List[str] = []
        for catalog_id in requested_ids:
            entry = by_id.get(catalog_id)
            if not entry:
                missing.append(catalog_id)
                continue
            if not str(entry.get("image_url", "")).strip():
                no_url.append(catalog_id)
                continue
            selected.append(entry)

        if not selected:
            return jsonify(
                {
                    "ok": False,
                    "error": "no downloadable catalog entries selected",
                    "missing_ids": missing,
                    "missing_url_ids": no_url,
                }
            ), 400

        _ensure_image_cache_dir()
        job_id = uuid.uuid4().hex
        with CATALOG_PULL_JOBS_LOCK:
            CATALOG_PULL_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "storage_dir": str(IMAGE_CACHE_DIR),
                "requested_ids": requested_ids,
                "accepted_ids": [str(e.get("id", "")) for e in selected],
                "missing_ids": missing,
                "missing_url_ids": no_url,
                "skip_existing": skip_existing,
                "results": [],
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "total": 0},
            }

        thread = threading.Thread(target=_run_catalog_pull_job, args=(job_id, selected, skip_existing), daemon=True)
        thread.start()
        return jsonify(
            {
                "ok": True,
                "job_id": job_id,
                "accepted_ids": [str(e.get("id", "")) for e in selected],
                "missing_ids": missing,
                "missing_url_ids": no_url,
                "storage_dir": str(IMAGE_CACHE_DIR),
            }
        )

    @app.get("/api/sd/catalog-pull/jobs/<job_id>")
    def sd_catalog_pull_job(job_id: str) -> Any:
        with CATALOG_PULL_JOBS_LOCK:
            job = CATALOG_PULL_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify({"ok": True, "job": job})

    @app.get("/api/sd/images")
    def sd_images() -> Any:
        return jsonify({"ok": True, "entries": _read_image_history(limit=500)})

    @app.post("/api/sd/images")
    def sd_images_create() -> Any:
        payload = request.get_json(silent=True) or {}
        record = _upsert_image_record(payload)
        return jsonify({"ok": True, "record": record}), 201

    @app.put("/api/sd/images/<record_id>")
    def sd_images_update(record_id: str) -> Any:
        if not _find_image_record(record_id):
            return jsonify({"ok": False, "error": "image record not found"}), 404
        payload = request.get_json(silent=True) or {}
        payload["id"] = record_id
        record = _upsert_image_record(payload, record_id=record_id)
        return jsonify({"ok": True, "record": record})

    @app.delete("/api/sd/images/<record_id>")
    def sd_images_delete(record_id: str) -> Any:
        deleted = _delete_image_record(record_id)
        if not deleted:
            return jsonify({"ok": False, "error": "image record not found"}), 404
        return jsonify({"ok": True})
