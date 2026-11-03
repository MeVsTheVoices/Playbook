"""SSH key generation and download endpoints."""

from __future__ import annotations

import subprocess
from typing import Any

from flask import jsonify, request, send_file

from services.ssh_keys import _generate_or_load_ssh_key, _is_safe_key_name, _list_ssh_keys
from services.storage import KEYS_DIR


def register_ssh_routes(app: Any) -> None:
    @app.post("/api/ssh/generate")
    def ssh_generate() -> Any:
        payload = request.get_json(silent=True) or {}
        key_name = str(payload.get("key_name", "rack-bootstrap-ed25519")).strip()
        comment = str(payload.get("comment", "rack-bootstrap")).strip()
        mode = str(payload.get("mode", "kload")).strip().lower()

        if not key_name:
            return jsonify({"ok": False, "errors": ["key_name is required"]}), 400
        if not _is_safe_key_name(key_name):
            return jsonify({"ok": False, "errors": ["key_name may contain only letters, numbers, dot, dash, and underscore"]}), 400
        if mode not in {"generate", "kload"}:
            return jsonify({"ok": False, "errors": ["mode must be generate or kload"]}), 400

        try:
            result = _generate_or_load_ssh_key(key_name, comment=comment, mode=mode)
        except FileNotFoundError:
            return jsonify({"ok": False, "errors": ["ssh-keygen command not found on host"]}), 500
        except FileExistsError as exc:
            return jsonify({"ok": False, "errors": [str(exc)]}), 409
        except ValueError as exc:
            return jsonify({"ok": False, "errors": [str(exc)]}), 400
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            return jsonify({"ok": False, "errors": [f"ssh-keygen failed: {stderr}"]}), 500

        return jsonify({"ok": True, **result})

    @app.get("/api/ssh/download/<key_name>/<kind>")
    def ssh_download(key_name: str, kind: str) -> Any:
        if not _is_safe_key_name(key_name):
            return jsonify({"ok": False, "error": "invalid key name"}), 400
        if kind not in {"private", "public"}:
            return jsonify({"ok": False, "error": "kind must be private or public"}), 400

        file_name = key_name if kind == "private" else f"{key_name}.pub"
        path = KEYS_DIR / file_name
        if not path.exists() or not path.is_file():
            return jsonify({"ok": False, "error": "key file not found"}), 404

        return send_file(path, as_attachment=True, download_name=file_name)

    @app.get("/api/ssh/keys")
    def ssh_keys_list() -> Any:
        return jsonify({"ok": True, "keys": _list_ssh_keys()})
