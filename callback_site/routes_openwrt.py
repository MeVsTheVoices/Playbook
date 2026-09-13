from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from core import *


def register_openwrt_routes(app: Any) -> None:
    openwrt = Blueprint("openwrt", __name__)

    @openwrt.post("/api/openwrt/inspect")
    def openwrt_inspect() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            result = _inspect_openwrt_probe(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({"ok": True, **result})

    @openwrt.post("/api/openwrt/push-key")
    def openwrt_push_key() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            host = str(payload.get("host", "")).strip()
            port = int(payload.get("port", 22) or 22)
            username = str(payload.get("username", "root")).strip() or "root"
            password = str(payload.get("password", ""))
            key_name = str(payload.get("key_name", "")).strip()
            if not host:
                raise ValueError("host is required")
            if not key_name:
                raise ValueError("key_name is required")

            key_info = _resolve_openwrt_ssh_key(key_name)
            result = _push_ssh_key_to_target(
                host=host,
                port=port,
                username=username,
                password=password,
                public_key=key_info["public_key"],
                public_key_path=key_info["public_key_path"],
                key_name=key_name,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({"ok": True, **result})

    app.register_blueprint(openwrt)