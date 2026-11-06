"""Boot log endpoints: list, read and pull a node's logs."""

from __future__ import annotations

from typing import Any

from flask import jsonify

from services.boot_logs import _fetch_boot_logs, _list_boot_logs, _read_boot_log, _safe_name


def register_boot_log_routes(app: Any) -> None:
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
