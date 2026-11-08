"""Boot log endpoints: list, read and pull a node's logs."""

from __future__ import annotations

from typing import Any
import logging

from flask import jsonify, request

from services.boot_logs import _fetch_boot_logs, _list_boot_logs, _read_boot_log, _safe_name, _store_pushed_boot_log
from services.node_archive import archived_nodes, node_key
from services.node_inventory import _node_ops_inventory_view
from services.storage import BOOT_LOGS_DIR

MAX_PUSH_CHARS = 2_000_000
logger = logging.getLogger(__name__)


def register_boot_log_routes(app: Any) -> None:
    @app.get("/api/nodes/boot-log-hosts")
    def boot_log_hosts() -> Any:
        hidden = {node_key(item["name"]) for item in archived_nodes()}
        names = {item["name"] for item in _node_ops_inventory_view()}
        names.update(item["name"] for item in archived_nodes())
        if BOOT_LOGS_DIR.exists():
            names.update(path.name for path in BOOT_LOGS_DIR.iterdir() if path.is_dir() and _safe_name(path.name))
        return jsonify(ok=True, hosts=[{"name": name, "archived": node_key(name) in hidden} for name in sorted(names)])

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
        if node_key(node_name) in {node_key(item["name"]) for item in archived_nodes()}:
            return jsonify(ok=False, error="Restore the node in Settings before collecting new logs."), 409
        payload = request.get_json(silent=True) if request.is_json else {}
        if not isinstance(payload, dict):
            return jsonify(ok=False, error="Invalid log collection payload."), 400
        route_id = payload.get("route_id")
        if route_id is not None and (not isinstance(route_id, str) or not route_id.strip()):
            return jsonify(ok=False, error="route_id must be a non-empty string."), 400
        try:
            meta = _fetch_boot_logs(node_name, trigger="manual", route_id=route_id)
        except ValueError as exc:
            logger.warning("Boot log route rejected for %s: %s", node_name, exc)
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify({"ok": meta.get("status") == "ok", "log": meta})
