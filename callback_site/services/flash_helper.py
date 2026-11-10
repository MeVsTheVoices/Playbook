"""Optional authenticated helper client and Kubernetes Job wake-up."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

logger = logging.getLogger(__name__)
WAKE_LOCK = threading.Lock()


class HelperError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("CALLBACK_FLASH_HELPER_URL", "").strip())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _kubectl(*args: str, stdin: str | None = None) -> str:
    namespace = os.environ.get("CALLBACK_FLASH_HELPER_NAMESPACE", "").strip()
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", namespace):
        raise HelperError("Configure a valid CALLBACK_FLASH_HELPER_NAMESPACE.")
    try:
        result = subprocess.run(
            ["kubectl", "--namespace", namespace, "--request-timeout=10s", *args],
            input=stdin, capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.exception("Flash-helper Kubernetes operation failed.")
        raise HelperError("Cannot contact Kubernetes to wake the flash helper.") from exc
    if result.returncode:
        logger.error("Flash-helper Kubernetes operation failed: %s", result.stderr.strip())
        raise HelperError("Kubernetes rejected the helper lifecycle operation; inspect portal logs and RBAC.")
    return result.stdout


def wake() -> None:
    template = os.environ.get("CALLBACK_FLASH_HELPER_TEMPLATE", "").strip()
    if not template:
        raise HelperError("Flash helper is unavailable. Start it on the configured Linux host or enable its Helm lifecycle.")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?", template):
        raise HelperError("Invalid helper Job-template ConfigMap name.")
    with WAKE_LOCK:
        try:
            config = json.loads(_kubectl("get", "configmap", template, "-o", "json"))
            manifest = json.loads(config["data"]["job.json"])
            name = manifest["metadata"]["name"]
            if manifest.get("kind") != "Job" or not re.fullmatch(r"[a-z0-9][-a-z0-9]{0,62}", name):
                raise ValueError("Invalid Job template.")
            existing = _kubectl("get", "job", name, "--ignore-not-found", "-o", "json").strip()
            if existing:
                job = json.loads(existing)
                terminal = any(condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True"
                               for condition in job.get("status", {}).get("conditions", []))
                if not terminal:
                    return
                _kubectl("delete", "job", name, "--wait=false")
                return
            _kubectl("create", "-f", "-", stdin=json.dumps(manifest))
        except (ValueError, KeyError, TypeError) as exc:
            logger.exception("Invalid flash-helper Job configuration.")
            raise HelperError("Invalid flash-helper Job-template ConfigMap.") from exc


def call(path: str, payload: dict[str, Any] | None = None, *, allow_wake: bool = False) -> dict[str, Any]:
    url = os.environ.get("CALLBACK_FLASH_HELPER_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise HelperError("Invalid CALLBACK_FLASH_HELPER_URL.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HelperError("Configure a valid HTTP(S) CALLBACK_FLASH_HELPER_URL without embedded credentials.")
    try:
        token = Path(os.environ["CALLBACK_FLASH_HELPER_TOKEN_FILE"]).read_text().strip()
    except (OSError, KeyError) as exc:
        raise HelperError("Cannot read CALLBACK_FLASH_HELPER_TOKEN_FILE.") from exc
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise HelperError("The helper token must contain 32-256 URL-safe ASCII characters.")
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(url + path, data=data, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        with build_opener(_NoRedirect).open(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as exc:
        logger.error("Flash helper rejected %s: HTTP %s", path, exc.code)
        try:
            error = json.loads(exc.read()).get("error", f"HTTP {exc.code}")
        except (ValueError, AttributeError):
            error = f"HTTP {exc.code}"
        raise HelperError(f"Flash helper: {error}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        if allow_wake:
            wake()
            raise HelperError("Flash helper is starting; discovery will retry. Check the pinned Linux node if it stays unavailable.") from exc
        raise HelperError("Lost contact with the flash helper. Do not retry a write until its job/disk state is checked.") from exc
    except ValueError as exc:
        raise HelperError("Flash helper returned invalid JSON.") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise HelperError("Flash helper returned an invalid response.")
    return result


def devices() -> list[dict[str, Any]]:
    result = call("/devices", allow_wake=True)
    devices = result.get("devices")
    if not isinstance(devices, list) or any(not isinstance(device, dict) for device in devices):
        raise HelperError("Flash helper returned an invalid device list.")
    return devices


def _image_source(form: dict[str, Any]) -> tuple[str, str]:
    from domain_defaults import IMAGE_CATALOG

    url = str(form.get("image_url") or "").strip()
    default_id = ("network-toolkit-raspios-trixie-arm64-lite" if form.get("profile") == "network-toolkit"
                  else "ubuntu-24.04-pi-arm64")
    entry = next((entry for entry in IMAGE_CATALOG if
                  (entry.get("image_url") == url if url else entry.get("id") == default_id)), {})
    url = url or str(entry.get("image_url") or "")
    checksum = str(form.get("image_sha256") or entry.get("image_sha256") or "").strip()
    try:
        image = urlsplit(url)
    except ValueError as exc:
        raise HelperError("Invalid image URL.") from exc
    if image.scheme != "https" or not image.hostname or image.username or image.password:
        raise HelperError("Helper flashing requires an HTTPS image URL.")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
        raise HelperError("Enter the image's expected SHA-256; helper flashing never skips checksum verification.")
    return url, checksum


def validate_confirmation(form: dict[str, Any]) -> None:
    if form.get("confirm_device") != form.get("device") or not form.get("device_identity"):
        raise HelperError("Refresh devices and explicitly confirm the selected disk before flashing.")
    if not any(device.get("path") == form["device"] and device.get("identity") == form["device_identity"]
               for device in call("/devices")["devices"]):
        raise HelperError("Disk identity changed or the disk was removed. Refresh and confirm again.")
    if form.get("image_file") or form.get("workspace"):
        raise HelperError("Helper mode accepts HTTPS image URLs, not portal-local image files or workspaces.")
    _image_source(form)


def run_prepare_job(job_id: str, form: dict[str, Any]) -> None:
    from services.images import _record_image_history
    from services.sd_prepare import JOBS, JOBS_LOCK, _finish_job
    from services.ssh_keys import _archive_ssh_public_key

    try:
        _archive_ssh_public_key(job_id, form, form["ssh_public_key"].strip())
        payload = {key: value for key, value in form.items() if key not in {
            "image_file", "workspace", "ssh_key_name", "config_errors",
        }}
        payload["job_id"] = job_id
        payload["image_url"], payload["image_sha256"] = _image_source(form)
        call("/jobs", payload)
        while True:
            job = call(f"/jobs/{job_id}")["job"]
            if not isinstance(job, dict) or job.get("status") not in {"queued", "running", "completed", "failed"}:
                raise HelperError("Flash helper returned an invalid job.")
            with JOBS_LOCK:
                JOBS[job_id].update({key: value for key, value in job.items() if key in {
                    "status", "stdout", "stderr", "exit_code", "started_at", "finished_at",
                }})
            if job["status"] in {"completed", "failed"}:
                exit_code = _finish_job(job_id, job.get("stdout", ""), job.get("stderr", ""),
                                        job.get("exit_code", 1), hostname=form["hostname"])
                _record_image_history(form, job.get("stdout", ""), exit_code)
                return
            time.sleep(1)
    except (HelperError, OSError, ValueError, KeyError, TypeError) as exc:
        logger.exception("Remote flash job failed: %s", job_id)
        error = str(exc) if isinstance(exc, HelperError) else "Flash helper operation failed; inspect portal/helper logs."
        with JOBS_LOCK:
            JOBS[job_id].update(status="failed", exit_code=1, stderr=(
                f"{error} A disconnected writer may still be active; "
                "do not remove the disk or submit another flash until checked."
            ))
