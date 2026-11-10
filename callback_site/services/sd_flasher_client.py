"""Client for the host-side SD flasher agent (``sd_flasher.py``).

A containerised dashboard cannot see or write host SD/USB devices. When
``SD_FLASHER_URL`` is set, device listing and SD preparation are delegated to
the agent, which runs as root where the devices exist.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit


class FlasherError(RuntimeError):
    pass


def flasher_url() -> str:
    return os.getenv("SD_FLASHER_URL", "").strip().rstrip("/")


def enabled() -> bool:
    return bool(flasher_url())


def running_in_container() -> bool:
    return os.getenv("CALLBACK_RUNTIME", "").strip().lower() == "container"


def _token() -> str:
    token = os.getenv("SD_FLASHER_TOKEN", "").strip()
    if token:
        return token
    token_file = os.getenv("SD_FLASHER_TOKEN_FILE", "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
    base = flasher_url()
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise FlasherError(f"SD_FLASHER_URL must be an http(s) URL, got {base!r}.")
    token = _token()
    if not token:
        raise FlasherError("SD_FLASHER_URL is set but no SD_FLASHER_TOKEN is configured.")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            with exc:
                detail = json.loads(exc.read().decode("utf-8") or "{}")
        except (ValueError, OSError):
            detail = {}
        errors = detail.get("errors") if isinstance(detail, dict) else None
        message = "; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else f"HTTP {exc.code}"
        if exc.code == 401:
            message = "SD flasher rejected the token; make SD_FLASHER_TOKEN match the agent's token file."
        raise FlasherError(message) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FlasherError(f"SD flasher unreachable at {base}.") from exc
    if not isinstance(body, dict):
        raise FlasherError("SD flasher returned an invalid response.")
    return body


def prereq_errors() -> List[str]:
    try:
        health = _request("GET", "/health", timeout=3)
    except FlasherError as exc:
        return [f"{exc} Start the host flasher with scripts/run_sd_flasher.sh."]
    errors = health.get("errors")
    return [str(e) for e in errors] if isinstance(errors, list) else []


def list_devices() -> List[Dict[str, Any]]:
    devices = _request("GET", "/devices", timeout=5).get("devices")
    return [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []


def submit_job(spec: Dict[str, Any]) -> str:
    job_id = str(_request("POST", "/jobs", spec, timeout=10).get("job_id") or "")
    if not job_id:
        raise FlasherError("SD flasher did not return a job id.")
    return job_id


def get_job(job_id: str) -> Dict[str, Any]:
    job = _request("GET", f"/jobs/{quote(job_id, safe='')}", timeout=5).get("job")
    if not isinstance(job, dict):
        raise FlasherError("SD flasher returned an invalid job.")
    return job
