from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from services.settings import _load_app_settings, _save_app_settings
from services.storage import APPDATA_DIR
from services.tailscale_client import get_tailscale_framework_status
from tailscale_policy import build_policy


def register_settings_routes(app: Any, *, log_file: Path, root_logger: logging.Logger, logger: logging.Logger) -> None:
    settings_api = Blueprint("settings_api", __name__)

    @settings_api.get("/api/settings")
    def settings_get() -> Any:
        settings = _load_app_settings()
        settings["tailscale_api_key_set"] = bool(str(settings.get("tailscale_api_key", "")).strip())
        settings["tailscale_api_key"] = ""
        settings["log_file"] = str(log_file)
        settings["verbose_logging"] = root_logger.level == logging.DEBUG
        settings["appdata_dir"] = str(APPDATA_DIR)
        return jsonify({"ok": True, "settings": settings})

    @settings_api.post("/api/logging/verbose")
    def logging_verbose() -> Any:
        payload = request.get_json(silent=True) or {}
        enable = payload.get("enable", False)
        if enable:
            root_logger.setLevel(logging.DEBUG)
            logger.info("Verbose logging enabled")
            msg = "Verbose logging enabled"
        else:
            root_logger.setLevel(logging.INFO)
            logger.info("Verbose logging disabled")
            msg = "Verbose logging disabled"
        return jsonify({"ok": True, "message": msg, "verbose_logging": enable})

    @settings_api.get("/api/logging/tail")
    def logging_tail() -> Any:
        raw_lines = request.args.get("lines", default="50")
        try:
            lines = int(raw_lines)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "lines must be an integer"}), 400

        # Guard against negative or unbounded slicing requests.
        lines = max(1, min(lines, 5000))
        try:
            with open(log_file, "r") as handle:
                log_lines = handle.readlines()[-lines:]
            return jsonify({"ok": True, "log_file": str(log_file), "lines": log_lines})
        except FileNotFoundError:
            return jsonify({"ok": False, "error": "log file not found"}), 404

    @settings_api.post("/api/settings")
    def settings_update() -> Any:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "invalid payload"}), 400
        settings = _save_app_settings(payload)
        response_settings = dict(settings)
        response_settings["tailscale_api_key_set"] = bool(str(settings.get("tailscale_api_key", "")).strip())
        response_settings["tailscale_api_key"] = ""
        return jsonify({"ok": True, "settings": response_settings})

    @settings_api.get("/health")
    def health() -> Any:
        return jsonify({"ok": True})

    @settings_api.get("/api/tailscale/framework-status")
    def tailscale_framework_status() -> Any:
        status = get_tailscale_framework_status()
        return jsonify({"ok": True, "status": status})

    @settings_api.post("/api/tailscale/policy")
    def tailscale_policy() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "errors": ["JSON object required"]}), 400
        result = build_policy(payload)
        return jsonify(result), (200 if result["ok"] else 400)

    app.register_blueprint(settings_api)