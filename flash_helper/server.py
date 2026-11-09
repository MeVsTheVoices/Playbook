"""Authenticated, idle-expiring Linux appliance for the existing imaging pipeline."""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from flash_helper.devices import discover, verify
from services.sd_prepare import JOBS, JOBS_LOCK, _run_prepare_job, _sanitize_sd_prepare_payload
from services.installer_config import UEFI_PROFILES, is_installer
from playbook_server import PROFILE as SERVER_PROFILE

logger = logging.getLogger(__name__)


class Lease:
    def __init__(self, idle_seconds: int) -> None:
        if not 30 <= idle_seconds <= 3600:
            raise ValueError("FLASH_IDLE_SECONDS must be between 30 and 3600.")
        self.idle_seconds = idle_seconds
        self.lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.active = False
        self.stopping = False

    def should_stop(self) -> bool:
        with self.lock:
            if not self.active and (self.stopping or time.monotonic() - self.last_activity >= self.idle_seconds):
                self.stopping = True
                return True
            return False


def _validate(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Provide a preparation object.")
    allowed = {
        "profile", "distribution", "hostname", "device", "device_identity", "confirm_device",
        "ssh_public_key", "ssh_public_key_url", "callback_url", "image_url", "image_sha256",
        "sd_options_profile", "pi_only", "cloud_init_options", "job_id",
        "workflow", "catalog_id", "install_authorized",
    }
    if set(payload) - allowed:
        raise ValueError("Unsupported helper fields. Local image paths, workspaces and commands are not accepted.")
    for key in allowed - {"cloud_init_options", "pi_only", "install_authorized"}:
        if key in payload and (not isinstance(payload[key], str) or "\x00" in payload[key]):
            raise ValueError(f"{key} must be a string without NUL characters.")
    if not isinstance(payload.get("cloud_init_options", {}), dict):
        raise ValueError("cloud_init_options must be an object.")
    if payload.get("profile") not in {"pi5", "pi4", "pi3bplus", "network-toolkit", SERVER_PROFILE, *UEFI_PROFILES}:
        raise ValueError("Select a supported provisioning profile.")
    if payload.get("distribution", "standard") not in {"standard", "k3s"}:
        raise ValueError("distribution must be standard or k3s.")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", payload.get("hostname", "")):
        raise ValueError("Invalid node hostname.")
    if not re.fullmatch(r"[a-f0-9]{32}", payload.get("job_id", "")):
        raise ValueError("Invalid job ID.")
    if not payload.get("ssh_public_key") or payload.get("confirm_device") != payload.get("device"):
        raise ValueError("An SSH public key and explicit target confirmation are required.")
    if not re.fullmatch(r"[a-f0-9]{64}", payload.get("device_identity", "")):
        raise ValueError("A fresh device identity is required.")
    for key in ("callback_url", "ssh_public_key_url"):
        if payload.get(key):
            url = urlsplit(payload[key])
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError(f"Invalid {key}.")
    if not payload.get("callback_url"):
        raise ValueError("A callback URL is required.")
    image = urlsplit(payload.get("image_url", ""))
    if image.scheme != "https" or not image.hostname or image.username or image.password:
        raise ValueError("The helper requires an explicit HTTPS image URL.")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", payload.get("image_sha256", "")):
        raise ValueError("The helper requires the expected image SHA-256.")
    form = _sanitize_sd_prepare_payload(payload)
    if form.get("config_errors"):
        raise ValueError("; ".join(form["config_errors"]))
    form.update({key: payload[key] for key in ("device_identity", "confirm_device")})
    form["ssh_public_key_url"] = payload.get("ssh_public_key_url", "")
    return form


def create_app(token: str, lease: Lease) -> Flask:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("The dedicated helper token must contain 32-256 URL-safe ASCII characters.")
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

    @app.before_request
    def authenticate() -> Any:
        if request.path == "/health" and request.method == "GET":
            return None
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            logger.warning("Rejected unauthenticated flash-helper request.")
            return jsonify(ok=False, error="Unauthorized."), 401
        with lease.lock:
            if lease.stopping:
                return jsonify(ok=False, error="Helper is shutting down; reopen discovery."), 503
            lease.last_activity = time.monotonic()
        return None

    @app.get("/health")
    def health() -> Any:
        return jsonify(ok=True)

    @app.get("/devices")
    def devices() -> Any:
        try:
            return jsonify(ok=True, devices=discover())
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            logger.exception("Flash device discovery failed.")
            return jsonify(ok=False, error=str(exc)), 503

    def run(job_id: str, form: dict[str, Any]) -> None:
        try:
            with JOBS_LOCK:
                JOBS[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            verify(form["device"], form["device_identity"])
            if is_installer(form):
                from flash_helper.installer import run as run_installer
                run_installer(job_id, form)
            else:
                _run_prepare_job(job_id, form)
        except Exception:
            # This job boundary must report failures and release the lease even if a worker fails.
            logger.exception("Flash worker failed: %s", job_id)
            with JOBS_LOCK:
                JOBS[job_id].update(status="failed", exit_code=1, stderr="Flash worker failed; inspect helper logs.")
        finally:
            with JOBS_LOCK:
                JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            with lease.lock:
                lease.active = False
                lease.last_activity = time.monotonic()

    @app.post("/jobs")
    def prepare() -> Any:
        try:
            payload = request.get_json(silent=True)
            form = _validate(payload)
            job_id = payload["job_id"]
            with lease.lock:
                if lease.stopping:
                    return jsonify(ok=False, error="Helper expired while reading the request; refresh discovery."), 503
                with JOBS_LOCK:
                    if job_id in JOBS:
                        return jsonify(ok=True, job=JOBS[job_id]), 200
                    if lease.active:
                        return jsonify(ok=False, error="Another flash is active."), 409
                    verify(form["device"], form["device_identity"])
                    JOBS[job_id] = {"id": job_id, "status": "queued", "stdout": "", "stderr": "", "exit_code": None}
                    while len(JOBS) > 20:
                        del JOBS[next(iter(JOBS))]
                lease.active = True
            try:
                threading.Thread(target=run, args=(job_id, form), daemon=False).start()
            except RuntimeError:
                logger.exception("Cannot start flash worker: %s", job_id)
                with JOBS_LOCK:
                    JOBS[job_id].update(status="failed", exit_code=1, stderr="Cannot start flash worker.")
                with lease.lock:
                    lease.active = False
                return jsonify(ok=False, error="Cannot start flash worker."), 503
            return jsonify(ok=True, job_id=job_id), 202
        except ValueError as exc:
            logger.warning("Flash request rejected: %s", exc)
            return jsonify(ok=False, error=str(exc)), 400
        except (OSError, KeyError, subprocess.SubprocessError):
            logger.exception("Flash preflight failed.")
            return jsonify(ok=False, error="Cannot verify target disk. Refresh discovery."), 503

    @app.get("/jobs/<job_id>")
    def job(job_id: str) -> Any:
        with JOBS_LOCK:
            if job_id not in JOBS:
                return jsonify(ok=False, error="Job not found; helper state does not survive restart."), 404
            return jsonify(ok=True, job=JOBS[job_id])

    return app


def main() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("The isolated Linux helper must run as root.")
    if os.environ.get("CALLBACK_FLASH_HELPER_URL"):
        raise RuntimeError("Do not configure the portal helper client inside the helper.")
    token = Path(os.environ["FLASH_TOKEN_FILE"]).read_text().strip()
    lease = Lease(int(os.environ.get("FLASH_IDLE_SECONDS", "180")))
    app = create_app(token, lease)
    # Validate hardware policy before opening the API, even when no reader is attached.
    discover()
    server = make_server(os.environ.get("FLASH_BIND", "127.0.0.1"), int(os.environ.get("FLASH_PORT", "8090")), app, threaded=True)
    def stop(signum: int, frame: Any) -> None:
        # A signal handler must not reacquire a lock held by the interrupted thread.
        lease.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    logger.info("Flash helper listening; idle expiry is %s seconds.", lease.idle_seconds)
    try:
        while not lease.should_stop():
            time.sleep(1)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    logger.info("Flash helper exited without an active writer.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
