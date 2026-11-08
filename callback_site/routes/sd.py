"""SD-card prepare, device and option-profile endpoints."""

from __future__ import annotations

import queue
import re
import threading
import uuid
import ipaddress
from urllib.parse import urlsplit, urlunsplit, quote
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict

from flask import g, jsonify, request, Response, stream_with_context

from k3s_lightweight import ROLE_PROFILES as K3S_ROLE_PROFILES
from network_toolkit import PROFILE as TOOLKIT_PROFILE
from services.ssh_keys import _is_safe_key_name
from services.storage import KEYS_DIR

from services.k3s_config import _check_longform
from services.sd_devices import (
    DEVICE_SUBSCRIBERS,
    DEVICE_SUBSCRIBERS_LOCK,
    _device_event_message,
    _ensure_device_monitor_started,
    _list_sd_devices,
)
from services.sd_prepare import (
    JOBS,
    JOBS_LOCK,
    _ensure_prereqs,
    _redact_sd_secret_options,
    _run_prepare_job,
    _sanitize_sd_prepare_payload,
)
from services.sd_profiles import (
    _load_sd_option_profiles,
    _ordered_sd_option_profiles,
    _save_sd_option_profiles,
    _set_preferred_sd_option_profile,
)
from services.sd_profile_sessions import (
    SESSION_COOKIE, SD_SECRET_OPTIONS, _sd_session_id,
    _selected_sd_session_profile, _select_sd_session_profile, _session_sd_profile,
    _resolve_sd_session_secrets, _retain_sd_session_secrets, _forget_sd_session_profile,
    _load_sd_session,
)


def _generated_sd_option_profile_name(hostname: str) -> str:
    host = re.sub(r"[^a-z0-9-]+", "-", str(hostname or "").strip().lower()).strip("-")
    host = host[:32] or "node"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"generated-{host}-{stamp}"


def _auto_generate_sd_option_profile(payload: Dict[str, Any]) -> str:
    existing_name = str(payload.get("sd_options_profile", "")).strip()
    if existing_name:
        _set_preferred_sd_option_profile(existing_name)
        return existing_name

    distribution = str(payload.get("distribution", "standard")).strip().lower() or "standard"
    options = _redact_sd_secret_options(payload.get("cloud_init_options"))
    hostname = str(payload.get("hostname", "")).strip() or "node"
    base_name = _generated_sd_option_profile_name(hostname)

    profiles = _load_sd_option_profiles()
    next_name = base_name
    suffix = 2
    while next_name in profiles:
        next_name = f"{base_name}-{suffix}"
        suffix += 1

    profiles[next_name] = {
        "name": next_name,
        "description": f"Auto-generated from SD prep request for {hostname}",
        "distribution": distribution if distribution in {"standard", "k3s"} else "standard",
        "pi_only": bool(payload.get("pi_only", False)),
        "options": options,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload.get("profile") == TOOLKIT_PROFILE:
        profiles[next_name]["profile"] = TOOLKIT_PROFILE
    _save_sd_option_profiles(profiles)
    _set_preferred_sd_option_profile(next_name)
    return next_name


def register_sd_routes(app: Any) -> None:
    @app.after_request
    def sd_session_cookie(response: Response) -> Response:
        if getattr(g, "sd_profile_new_session", False):
            response.set_cookie(SESSION_COOKIE, _sd_session_id(), httponly=True, samesite="Strict", secure=request.is_secure)
        if hasattr(g, "sd_profile_session_id"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def sd_storage_errors(handler: Any) -> Any:
        @wraps(handler)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                return handler(*args, **kwargs)
            except (OSError, RuntimeError):
                app.logger.error("Cloud-init configuration storage failed; check data-volume availability and session-file integrity.")
                message = "Unable to read or save cloud-init configuration. Check the data volume and session files."
                return jsonify(ok=False, error=message, errors=[message]), 500
        return wrapped

    @app.get("/api/sd/options-profiles")
    @sd_storage_errors
    def sd_options_profiles_get() -> Any:
        profiles = _load_sd_option_profiles()
        ordered = _ordered_sd_option_profiles(profiles)
        return jsonify({"ok": True, "profiles": [_session_sd_profile(profile) for profile in ordered], "selected_profile": _selected_sd_session_profile(), "ssh_key_name": _load_sd_session()["selected_ssh_key"]})

    @app.post("/api/sd/options-profiles/session")
    @sd_storage_errors
    def sd_options_profile_session() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not payload or any(key not in {"name", "ssh_key_name"} or not isinstance(value, str) for key, value in payload.items()):
            return jsonify(ok=False, error="Provide a template name or SSH-key name as a string."), 400
        name = payload["name"].strip() if "name" in payload else None
        if name and name not in _load_sd_option_profiles():
            return jsonify(ok=False, error="Template not found."), 404
        ssh_key = payload.get("ssh_key_name")
        if ssh_key and (not _is_safe_key_name(ssh_key) or not (KEYS_DIR / f"{ssh_key}.pub").is_file()):
            return jsonify(ok=False, error="Saved SSH public key not found."), 404
        _select_sd_session_profile(name, ssh_key)
        return jsonify(ok=True)

    @app.post("/api/sd/options-profiles")
    @sd_storage_errors
    def sd_options_profiles_save() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(ok=False, error="Invalid template payload."), 400
        name = str(payload.get("name", "")).strip()
        if not name:
            return jsonify({"ok": False, "error": "name is required"}), 400
        if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", name):
            return jsonify({"ok": False, "error": "invalid profile name"}), 400

        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        if any(key in options and not isinstance(options[key], str) for key in SD_SECRET_OPTIONS):
            return jsonify(ok=False, error="Credential fields must be strings."), 400
        clear = payload.get("clear_secret_options", [])
        if not isinstance(clear, list) or any(not isinstance(key, str) or key not in SD_SECRET_OPTIONS for key in clear):
            return jsonify(ok=False, error="Invalid clear_secret_options."), 400
        try:
            resolved_options = _resolve_sd_session_secrets(str(payload.get("secret_source_profile") or name), options)
        except ValueError as exc:
            app.logger.warning("Cloud-init credential save rejected: %s", exc)
            return jsonify(ok=False, error=str(exc)), 400
        description = str(payload.get("description", "")).strip()
        distribution = str(payload.get("distribution", "standard")).strip().lower() or "standard"
        if distribution not in {"standard", "k3s"}:
            distribution = "standard"
        pi_only = bool(payload.get("pi_only", False))

        profiles = _load_sd_option_profiles()
        profiles[name] = {
            "name": name,
            "description": description,
            "distribution": distribution,
            "pi_only": pi_only,
            "options": _redact_sd_secret_options(options),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if payload.get("profile") == TOOLKIT_PROFILE:
            profiles[name]["profile"] = TOOLKIT_PROFILE
        _save_sd_option_profiles(profiles)
        _retain_sd_session_secrets(name, resolved_options, clear)
        if bool(payload.get("set_preferred", False)):
            _set_preferred_sd_option_profile(name)
        return jsonify({"ok": True, "profile": _session_sd_profile(profiles[name])})

    @app.delete("/api/sd/options-profiles/<profile_name>")
    @sd_storage_errors
    def sd_options_profiles_delete(profile_name: str) -> Any:
        profiles = _load_sd_option_profiles()
        target = str(profile_name).strip()
        if target not in profiles:
            return jsonify({"ok": False, "error": "profile not found"}), 404
        profiles.pop(target, None)
        _save_sd_option_profiles(profiles)
        _forget_sd_session_profile(target)
        return jsonify({"ok": True})

    @app.get("/api/sd/devices")
    def sd_devices() -> Any:
        _ensure_device_monitor_started()
        prereq_errors = _ensure_prereqs()
        return jsonify({"devices": _list_sd_devices(), "errors": prereq_errors})

    @app.get("/api/sd/devices/webhook")
    def sd_device_webhook() -> Response:
        _ensure_device_monitor_started()

        def event_stream() -> Any:
            sub: queue.Queue[str] = queue.Queue(maxsize=4)
            with DEVICE_SUBSCRIBERS_LOCK:
                DEVICE_SUBSCRIBERS.append(sub)

            try:
                initial_devices = _list_sd_devices()
                yield _device_event_message(initial_devices)
                while True:
                    try:
                        yield sub.get(timeout=20)
                    except queue.Empty:
                        yield "event: ping\\ndata: {}\\n\\n"
            finally:
                with DEVICE_SUBSCRIBERS_LOCK:
                    if sub in DEVICE_SUBSCRIBERS:
                        DEVICE_SUBSCRIBERS.remove(sub)

        return Response(
            stream_with_context(event_stream()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/sd/prepare")
    @sd_storage_errors
    def sd_prepare() -> Any:
        prereq_errors = _ensure_prereqs()
        if prereq_errors:
            return jsonify({"ok": False, "errors": prereq_errors}), 400

        payload_raw = request.get_json(silent=True)
        if not isinstance(payload_raw, dict):
            return jsonify(ok=False, errors=["Invalid preparation payload."]), 400
        options = payload_raw.get("cloud_init_options")
        if isinstance(options, dict):
            try:
                payload_raw["cloud_init_options"] = _resolve_sd_session_secrets(str(payload_raw.get("sd_options_profile") or ""), options)
            except ValueError as exc:
                app.logger.warning("Cloud-init credential restore rejected: %s", exc)
                return jsonify(ok=False, errors=[str(exc)]), 400
        payload = _sanitize_sd_prepare_payload(payload_raw if isinstance(payload_raw, dict) else {})
        # Derive the endpoint locally; never accept an arbitrary key-fetch URL.
        payload.pop("ssh_public_key_url", None)
        key_name = str(payload.get("ssh_key_name") or "").strip()
        if key_name:
            if not _is_safe_key_name(key_name):
                return jsonify({"ok": False, "errors": ["Invalid SSH key name."]}), 400
            path = KEYS_DIR / f"{key_name}.pub"
            try:
                public_key = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                return jsonify({"ok": False, "errors": ["Selected SSH public key not found."]}), 400
            except OSError:
                app.logger.exception("Unable to read selected SSH public key")
                return jsonify({"ok": False, "errors": ["Unable to read selected SSH public key."]}), 500
            if not public_key or public_key != str(payload.get("ssh_public_key") or "").strip():
                return jsonify({"ok": False, "errors": ["Selected website key does not match the SSH public key. Reload the key."]}), 400
            try:
                callback = urlsplit(str(payload.get("callback_url") or ""))
                host = callback.hostname or ""
                callback.port
                if callback.scheme not in {"http", "https"} or not host or callback.username or callback.password:
                    raise ValueError("Invalid callback URL.")
                if host.lower() == "localhost" or host.lower().endswith(".localhost"):
                    raise ValueError("The node cannot fetch a website key from localhost.")
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    address = None
                if address is not None and (address.is_loopback or address.is_unspecified):
                    raise ValueError("Use a callback host address reachable from the node, not loopback.")
            except ValueError as exc:
                return jsonify({"ok": False, "errors": [str(exc)]}), 400
            payload["ssh_public_key_url"] = urlunsplit((
                callback.scheme, callback.netloc, f"/api/ssh/public/{quote(key_name, safe='')}", "", "",
            ))
        required = ["profile", "hostname", "device", "ssh_public_key", "callback_url"]
        missing = [k for k in required if not str(payload.get(k, "")).strip()]
        if missing:
            return jsonify({"ok": False, "errors": [f"Missing fields: {', '.join(missing)}"]}), 400

        sd_profiles = (*K3S_ROLE_PROFILES, TOOLKIT_PROFILE)
        if payload["profile"] not in sd_profiles:
            return jsonify({"ok": False, "errors": [f"profile must be one of: {', '.join(sd_profiles)}"]}), 400

        distribution = str(payload.get("distribution", "standard")).strip().lower() or "standard"
        if distribution not in {"standard", "k3s"}:
            return jsonify({"ok": False, "errors": ["distribution must be standard or k3s"]}), 400
        payload["distribution"] = distribution
        if payload.get("config_errors"):
            errors = payload["config_errors"]
            if payload["profile"] != TOOLKIT_PROFILE:
                errors = [f"K3s lightweight options: {err}" for err in errors]
            return jsonify({"ok": False, "errors": errors}), 400
        if distribution == "k3s":
            k3s_check = _check_longform("k3s_config", payload["cloud_init_options"].get("k3s_config", ""))
            if not k3s_check["ok"]:
                return jsonify({"ok": False, "errors": [f"K3s config: {err}" for err in k3s_check["errors"]]}), 400
        payload["sd_options_profile"] = _auto_generate_sd_option_profile(payload)
        _retain_sd_session_secrets(payload["sd_options_profile"], payload["cloud_init_options"])

        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "request": {
                    "profile": payload["profile"],
                    "distribution": payload["distribution"],
                    "hostname": payload["hostname"],
                    "device": payload["device"],
                    "callback_url": payload["callback_url"],
                    "image_sha256": str(payload.get("image_sha256", "")).strip(),
                    "sd_options_profile": str(payload.get("sd_options_profile", "")).strip(),
                    "cloud_init_options": _redact_sd_secret_options(payload.get("cloud_init_options")),
                },
                "stdout": "",
                "stderr": "",
                "exit_code": None,
            }

        thread = threading.Thread(target=_run_prepare_job, args=(job_id, payload), daemon=True)
        thread.start()
        return jsonify({"ok": True, "job_id": job_id})

    @app.get("/api/sd/jobs")
    def sd_jobs() -> Any:
        with JOBS_LOCK:
            jobs = list(JOBS.values())

        active_jobs: List[Dict[str, Any]] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            status = str(job.get("status", "")).strip().lower()
            if status in {"queued", "running"}:
                active_jobs.append(job)

        active_jobs.sort(
            key=lambda job: str(job.get("created_at") or job.get("started_at") or "").lower(),
            reverse=True,
        )
        return jsonify({"ok": True, "jobs": active_jobs})

    @app.get("/api/sd/jobs/<job_id>")
    def sd_job(job_id: str) -> Any:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify({"ok": True, "job": job})
