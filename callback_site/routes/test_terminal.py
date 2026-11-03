"""Test-terminal endpoints (named remote checks)."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from flask import jsonify, request

from services.ssh_keys import _is_safe_key_name, _seed_known_host, _ssh_private_key_preflight
from services.storage import KEYS_DIR, KNOWN_HOSTS_FILE
from services.terminal_checks import (
    TERMINAL_CHECKS,
    TEST_TERMINAL_JOBS,
    TEST_TERMINAL_LOCK,
    _resolve_terminal_host,
    _run_test_terminal_job,
    _test_terminal_targets,
)

logger = logging.getLogger(__name__)


def register_test_terminal_routes(app: Any) -> None:
    @app.get("/api/test-terminal/checks")
    def test_terminal_checks() -> Any:
        checks = []
        for check_id, item in TERMINAL_CHECKS.items():
            checks.append(
                {
                    "id": check_id,
                    "label": item["label"],
                    "description": item["description"],
                }
            )
        return jsonify({"ok": True, "checks": checks})

    @app.get("/api/test-terminal/targets")
    def test_terminal_targets() -> Any:
        return jsonify({"ok": True, "targets": _test_terminal_targets(limit=2000)})

    @app.post("/api/test-terminal/start")
    def test_terminal_start() -> Any:
        payload = request.get_json(silent=True) or {}
        host = str(payload.get("host", payload.get("hostname", ""))).strip()
        resolved_host = _resolve_terminal_host(host)
        username = str(payload.get("username", "jdizzle")).strip() or "jdizzle"
        key_name = str(payload.get("key_name", "rack-bootstrap-ed25519")).strip() or "rack-bootstrap-ed25519"
        check_id = str(payload.get("check_id", "identity")).strip() or "identity"
        port = int(payload.get("port", 22) or 22)

        if not host:
            return jsonify({"ok": False, "error": "host is required"}), 400
        if not _is_safe_key_name(key_name):
            return jsonify({"ok": False, "error": "invalid key_name"}), 400
        if port < 1 or port > 65535:
            return jsonify({"ok": False, "error": "invalid port"}), 400
        if check_id not in TERMINAL_CHECKS:
            return jsonify({"ok": False, "error": "invalid check_id"}), 400

        key_path = KEYS_DIR / key_name
        if not key_path.exists():
            return jsonify({"ok": False, "error": f"key not found: {key_name}"}), 404

        _seed_known_host(resolved_host, port)

        key_preflight_error = _ssh_private_key_preflight(str(key_path))
        if key_preflight_error:
            return jsonify({"ok": False, "error": key_preflight_error}), 400

        selected_check = TERMINAL_CHECKS[check_id]
        remote_command = selected_check["command"]

        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={KNOWN_HOSTS_FILE}",
            "-o",
            "ConnectTimeout=8",
            "-i",
            str(key_path),
            "-p",
            str(port),
            f"{username}@{resolved_host}",
            remote_command,
        ]

        job_id = uuid.uuid4().hex
        with TEST_TERMINAL_LOCK:
            TEST_TERMINAL_JOBS[job_id] = {
                "id": job_id,
                "status": "running",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "command": cmd,
                "requested_host": host,
                "resolved_host": resolved_host,
                "check_id": check_id,
                "check_label": selected_check["label"],
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }

        thread = threading.Thread(target=_run_test_terminal_job, args=(job_id, cmd), daemon=True)
        thread.start()
        logger.info(
            "Started terminal job: id=%s, host=%s, resolved_host=%s, user=%s, check_id=%s",
            job_id,
            host,
            resolved_host,
            username,
            check_id,
        )
        return jsonify({"ok": True, "job_id": job_id})

    @app.get("/api/test-terminal/jobs/<job_id>")
    def test_terminal_job_status(job_id: str) -> Any:
        with TEST_TERMINAL_LOCK:
            job = TEST_TERMINAL_JOBS.get(job_id)
            if not job:
                return jsonify({"ok": False, "error": "job not found"}), 404
            return jsonify({"ok": True, "job": job})
