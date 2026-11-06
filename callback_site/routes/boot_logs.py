"""Boot log endpoints: list, read and pull a node's logs."""

from __future__ import annotations

from typing import Any

from flask import jsonify, request

from services.boot_logs import _fetch_boot_logs, _list_boot_logs, _read_boot_log, _safe_name, _store_pushed_boot_log

MAX_PUSH_CHARS = 2_000_000


def register_boot_log_routes(app: Any) -> None:
    @app.post("/api/cloud-init/boot-log")
    def boot_log_push() -> Any:
        payload = request.get_json(silent=True) or {}
        name = _safe_name(str(payload.get("hostname", "")))
        text = payload.get("log")
        if not name or not isinstance(text, str) or not text.strip():
            return jsonify({"ok": False, "error": "hostname and log are required"}), 400
        if len(text) > MAX_PUSH_CHARS:
            return jsonify({"ok": False, "error": "log too large"}), 413
        meta = _store_pushed_boot_log(
            name,
            text,
            stage=str(payload.get("stage", "")).strip()[:40],
            instance_id=str(payload.get("instance_id", "")).strip()[:120],
            source=str(request.remote_addr or ""),
        )
        return jsonify({"ok": True, "id": meta["id"]})

    @app.get("/api/nodes/<node_name>/boot-logs")
    def boot_logs_list(node_name: str) -> Any:
        if not _safe_name(node_name):
            return jsonify({"ok": False, "error": "invalid node name"}), 400
        return jsonify({"ok": True, "logs": _list_boot_logs(node_name)})

    @app.get("/api/nodes/<node_name>/boot-logs/<log_id>")
    def boot_logs_read(node_name: str, log_id: str) -> Any:
        if not _safe_name(node_name) or not _safe_name(log_id):
            return jsonify({"ok": False, "error": "invalid name"}), 400
        text = _read_boot_log(node_name, log_id)
        if text is None:
            return jsonify({"ok": False, "error": "boot log not found"}), 404
        return jsonify({"ok": True, "text": text})

    @app.post("/api/nodes/<node_name>/boot-logs/fetch")
    def boot_logs_fetch(node_name: str) -> Any:
        if not _safe_name(node_name):
            return jsonify({"ok": False, "error": "invalid node name"}), 400
        meta = _fetch_boot_logs(node_name, trigger="manual")
        return jsonify({"ok": meta.get("status") == "ok", "log": meta})
