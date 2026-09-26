from __future__ import annotations

import base64
import binascii
import json
import os
import logging
import subprocess
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from core import *
from launch import maybe_reexec_with_sudo
from routes_openwrt import register_openwrt_routes
from routes_pages import register_page_routes
from routes_settings import register_settings_routes

# Setup logs directory
LOG_DIR = APPDATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "callback_site.log"

# Configure logging with rotation when the log file path is writable.
log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log_handler = None
try:
    log_handler = RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=10,  # Keep 10 rotated files
    )
    log_handler.setFormatter(log_formatter)
except OSError:
    log_handler = None

# Get root logger and configure
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if log_handler is not None:
    root_logger.addHandler(log_handler)

# Also log to console
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

NODE_REPORTS_DIR = APPDATA_DIR / "node_reports"
NODE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

TEST_TERMINAL_JOBS: Dict[str, Dict[str, Any]] = {}
TEST_TERMINAL_LOCK = threading.Lock()
PROVISION_JOBS: Dict[str, Dict[str, Any]] = {}
PROVISION_JOBS_LOCK = threading.Lock()
TERMINAL_CHECKS: Dict[str, Dict[str, str]] = {
    "identity": {
        "label": "Identity + Kernel",
        "description": "Validate hostname/user/kernel baseline",
        "command": "hostname && whoami && uname -a",
    },
    "cloud_init": {
        "label": "Cloud-Init State",
        "description": "Check cloud-init completion and status",
        "command": "cloud-init status --long || cloud-init status",
    },
    "service_baseline": {
        "label": "Service Baseline",
        "description": "Quick system health and networking status",
        "command": "systemctl is-system-running || true; systemctl --failed --no-legend || true; ip -brief address",
    },
    "remote_inventory": {
        "label": "Remote Inventory",
        "description": "Collect hostname, OS, cloud-init, services, network, and storage state",
        "command": (
            'printf "=== identity ===\\n"; '
            'hostname; whoami; id; uname -a; hostnamectl || true; '
            'printf "\\n=== os-release ===\\n"; cat /etc/os-release || true; '
            'printf "\\n=== cloud-init ===\\n"; cloud-init status --long || cloud-init status || true; '
            'printf "\\n=== network ===\\n"; ip -brief address || true; ip route || true; '
            'printf "\\n=== storage ===\\n"; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL || true; df -h || true; '
            'printf "\\n=== services ===\\n"; systemctl is-active ssh || true; systemctl is-active cloud-init-local cloud-init cloud-config cloud-final k3s || true; '
            'printf "\\n=== k3s-config ===\\n"; sudo -n cat /etc/rancher/k3s/config.yaml || true'
        ),
    },
}


def _run_test_terminal_job(job_id: str, cmd: list[str]) -> None:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        with TEST_TERMINAL_LOCK:
            TEST_TERMINAL_JOBS[job_id]["status"] = "failed"
            TEST_TERMINAL_JOBS[job_id]["stderr"] = str(exc)
            TEST_TERMINAL_JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        return

    output_chunks: list[str] = []
    if proc.stdout is not None:
        for line in proc.stdout:
            output_chunks.append(line)
            with TEST_TERMINAL_LOCK:
                TEST_TERMINAL_JOBS[job_id]["stdout"] = "".join(output_chunks)[-20000:]

    proc.wait()
    with TEST_TERMINAL_LOCK:
        TEST_TERMINAL_JOBS[job_id]["exit_code"] = proc.returncode
        TEST_TERMINAL_JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
        TEST_TERMINAL_JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


def _run_node_provision_job(job_id: str, target_node_names: list[str], mode: str, run_ansible: bool) -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    with PROVISION_JOBS_LOCK:
        job = PROVISION_JOBS.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        job["started_at"] = started_at
        job["updated_at"] = started_at

    try:
        node_lookup = {
            item.get("name"): item
            for item in _node_ops_inventory_view()
            if isinstance(item, dict) and item.get("name")
        }
        results: list[Dict[str, Any]] = []
        for node_name in target_node_names:
            node = node_lookup.get(node_name)
            if not node:
                continue
            state = _load_node_provisioning()
            profile_ids = _effective_node_profiles(node, state)
            if mode == "apply":
                missing = _missing_required_inputs(node_name, profile_ids, state)
                if missing:
                    with PROVISION_JOBS_LOCK:
                        job = PROVISION_JOBS.get(job_id)
                        if job:
                            job["status"] = "failed"
                            job["error"] = "missing required provisioning inputs"
                            job["missing_inputs"] = missing
                            job["finished_at"] = datetime.now(timezone.utc).isoformat()
                            job["updated_at"] = job["finished_at"]
                    return
            results.append(_apply_node_provisioning(node_name, profile_ids, mode=mode, run_ansible=run_ansible))

        with PROVISION_JOBS_LOCK:
            job = PROVISION_JOBS.get(job_id)
            if job:
                job["status"] = "completed"
                job["results"] = results
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["updated_at"] = job["finished_at"]
    except Exception as exc:
        with PROVISION_JOBS_LOCK:
            job = PROVISION_JOBS.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["updated_at"] = job["finished_at"]


def _is_valid_report_id(value: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{12,32}", str(value or "").strip().lower()))


def _decode_node_report_payload(payload: Dict[str, Any]) -> Any:
    raw = payload.pop("node_report", None)
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    report_b64 = str(payload.pop("node_report_b64", "")).strip()
    if not report_b64:
        return None
    try:
        decoded = base64.b64decode(report_b64).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return {"decode_error": True}
    decoded = decoded.strip()
    if not decoded:
        return None
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        return {"text": decoded}


def _store_node_report(report_payload: Any, event: Dict[str, Any]) -> str:
    if report_payload is None:
        return ""

    report_id = uuid.uuid4().hex[:12]
    report_record = {
        "id": report_id,
        "received_at": event.get("received_at", ""),
        "hostname": event.get("hostname", ""),
        "instance_id": event.get("instance_id", ""),
        "profile": event.get("profile", ""),
        "stage": event.get("stage", ""),
        "source_ip": event.get("source_ip", ""),
        "report": report_payload,
    }
    report_file = NODE_REPORTS_DIR / f"{report_id}.json"
    try:
        report_file.write_text(json.dumps(report_record, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Unable to store node report for hostname=%s: %s", event.get("hostname", ""), exc)
        return ""
    return report_id


def _read_node_report(report_id: str) -> Dict[str, Any] | None:
    rid = str(report_id or "").strip().lower()
    if not _is_valid_report_id(rid):
        return None
    report_file = NODE_REPORTS_DIR / f"{rid}.json"
    if not report_file.exists():
        return None
    try:
        payload = json.loads(report_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload

app = Flask(__name__)


@app.context_processor
def inject_runtime_flags() -> Dict[str, Any]:
    kube_state = _detect_kubernetes_runtime(force_refresh=False)
    return {
        "kubernetes_available": bool(kube_state.get("available", False)),
    }


def _catalog_entries_for_ui() -> List[Dict[str, Any]]:
    available_urls = _available_image_urls_from_history(limit=5000)
    return [_catalog_entry_with_availability(entry, available_urls=available_urls) for entry in IMAGE_CATALOG]


register_page_routes(app, catalog_entries_for_ui=_catalog_entries_for_ui)
register_openwrt_routes(app)
register_settings_routes(app, log_file=LOG_FILE, root_logger=root_logger, logger=logger)


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
    options = payload.get("cloud_init_options") if isinstance(payload.get("cloud_init_options"), dict) else {}
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
        "options": options,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_sd_option_profiles(profiles)
    _set_preferred_sd_option_profile(next_name)
    return next_name


@app.get("/api/sd/options-profiles")
def sd_options_profiles_get() -> Any:
    profiles = _load_sd_option_profiles()
    ordered = _ordered_sd_option_profiles(profiles)
    return jsonify({"ok": True, "profiles": ordered})


@app.post("/api/sd/options-profiles")
def sd_options_profiles_save() -> Any:
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", name):
        return jsonify({"ok": False, "error": "invalid profile name"}), 400

    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    description = str(payload.get("description", "")).strip()
    distribution = str(payload.get("distribution", "standard")).strip().lower() or "standard"
    if distribution not in {"standard", "k3s"}:
        distribution = "standard"

    profiles = _load_sd_option_profiles()
    profiles[name] = {
        "name": name,
        "description": description,
        "distribution": distribution,
        "options": options,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_sd_option_profiles(profiles)
    if bool(payload.get("set_preferred", False)):
        _set_preferred_sd_option_profile(name)
    return jsonify({"ok": True, "profile": profiles[name]})


@app.delete("/api/sd/options-profiles/<profile_name>")
def sd_options_profiles_delete(profile_name: str) -> Any:
    profiles = _load_sd_option_profiles()
    target = str(profile_name).strip()
    if target not in profiles:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    profiles.pop(target, None)
    _save_sd_option_profiles(profiles)
    return jsonify({"ok": True})


@app.get("/api/cloud-init/events")
def events() -> Any:
    return jsonify({"events": _read_events(limit=500)})


@app.post("/api/cloud-init/events/clear")
def clear_events() -> Any:
    cleared = _clear_events()
    return jsonify({"ok": True, "cleared": cleared})


def _normalize_stage(payload: Dict[str, Any]) -> str:
    for key in ("stage", "cloud_init_stage", "status", "phase", "event"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return "phone_home"


def _infer_profile_from_text(*values: str) -> str:
    for value in values:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if "pibplus" in text or "pi-bplus" in text or "bplus" in text:
            return "pibplus"
        if "pi5" in text or "pi-5" in text:
            return "pi5"
    return ""


def _extract_payload_profile(payload: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(payload, dict):
        for key in (
            "profile",
            "profile_name",
            "image_profile",
            "rack_profile",
            "role",
            "group",
            "ansible_group",
            "provision_profile",
        ):
            value = str(payload.get(key, "")).strip().lower()
            if value in {"pi5", "pibplus"}:
                return value
            inferred = _infer_profile_from_text(value)
            if inferred:
                return inferred
        for value in payload.values():
            inferred = _extract_payload_profile(value, depth + 1)
            if inferred:
                return inferred
        return ""
    if isinstance(payload, list):
        for item in payload:
            inferred = _extract_payload_profile(item, depth + 1)
            if inferred:
                return inferred
        return ""
    if isinstance(payload, str):
        return _infer_profile_from_text(payload)
    return ""


def _normalize_profile(payload: Dict[str, Any], hostname: str, instance_id: str) -> str:
    payload_profile = _extract_payload_profile(payload)
    if payload_profile:
        return payload_profile

    inferred = _infer_profile_from_text(hostname, instance_id, payload.get("fqdn", ""))
    if inferred:
        return inferred
    return "unknown"


def _normalize_hostname(payload: Dict[str, Any]) -> str:
    for key in ("hostname", "host", "node", "nodename"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _normalize_instance_id(payload: Dict[str, Any]) -> str:
    for key in ("instance_id", "instance", "instanceid"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _recent_event_defaults(hostname: str, instance_id: str, fqdn: str) -> Dict[str, str]:
    for event in _read_events(limit=500):
        if not isinstance(event, dict):
            continue

        matches = False
        if hostname and str(event.get("hostname", "")).strip() == hostname:
            matches = True
        if instance_id and str(event.get("instance_id", "")).strip() == instance_id:
            matches = True
        if fqdn and str(event.get("fqdn", "")).strip() == fqdn:
            matches = True
        if not matches:
            continue

        profile = str(event.get("profile", "")).strip().lower()
        stage = str(event.get("stage", "")).strip()
        return {
            "profile": profile if profile else "",
            "stage": stage if stage else "",
        }

    return {"profile": "", "stage": ""}


@app.post("/api/cloud-init/callback")
def callback() -> Any:
    payload_json = request.get_json(silent=True)
    payload_form = request.form.to_dict(flat=True)
    payload: Dict[str, Any] = payload_json if isinstance(payload_json, dict) and payload_json else payload_form
    payload = _strip_dataset_fields(payload)
    logger.debug(f"Callback received with payload keys: {list(payload.keys())}")

    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    instance_id = _normalize_instance_id(payload)
    hostname = _normalize_hostname(payload)
    fqdn = str(payload.get("fqdn") or payload.get("host_fqdn") or "").strip()
    source_ip, source_ip_source = _resolve_source_ip(payload, hostname=hostname, fqdn=fqdn)
    cert_subject = _header_or_payload(
        payload,
        "X-SSL-Client-Subject",
        "X-SSL-Client-Cert-Subject",
        "X-Client-Cert-Subject",
        "TLS-Client-Subject",
        "SSL-Client-Subject",
        "cert_subject",
    )
    cert_issuer = _header_or_payload(
        payload,
        "X-SSL-Client-Issuer",
        "X-SSL-Client-Cert-Issuer",
        "X-Client-Cert-Issuer",
        "TLS-Client-Issuer",
        "SSL-Client-Issuer",
        "cert_issuer",
    )
    cert_fingerprint = _header_or_payload(
        payload,
        "X-SSL-Client-Fingerprint",
        "X-SSL-Client-Cert-Fingerprint",
        "X-Client-Cert-Fingerprint",
        "TLS-Client-Fingerprint",
        "SSL-Client-Fingerprint",
        "cert_fingerprint",
    )
    cert_blob = _header_or_payload(
        payload,
        "X-SSL-Client-Cert",
        "X-Client-Cert",
        "SSL-Client-Cert",
        "client_cert",
    )

    raw_profile = _extract_payload_profile(payload)
    profile = _normalize_profile(payload, hostname, instance_id)
    profile_source = "payload" if raw_profile else "inferred"
    stage = _normalize_stage(payload)
    recent_defaults = _recent_event_defaults(hostname, instance_id, fqdn)

    if profile == "unknown" and recent_defaults.get("profile") not in {"", "unknown"}:
        profile = str(recent_defaults.get("profile", "")).strip().lower()
        profile_source = "recent_event"
    if stage == "phone_home" and recent_defaults.get("stage"):
        stage = str(recent_defaults.get("stage", "")).strip()

    # Some cloud-init phone-home calls can be empty; ignore these noise callbacks.
    if not payload and not hostname and not instance_id and not cert_subject and not cert_fingerprint:
        logger.info("Callback ignored: empty payload with no identifying fields")
        return jsonify({"ok": True, "ignored": True})

    event = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source_ip": source_ip,
        "source_ip_source": source_ip_source,
        "forwarded_for": forwarded_for,
        "remote_addr": request.remote_addr,
        "user_agent": request.headers.get("User-Agent", ""),
        "instance_id": instance_id,
        "hostname": hostname,
        "fqdn": fqdn,
        "profile": profile,
        "raw_profile": raw_profile,
        "profile_source": profile_source,
        "stage": stage,
        "cert_subject": cert_subject,
        "cert_issuer": cert_issuer,
        "cert_fingerprint": cert_fingerprint,
        "cert_present": bool(cert_subject or cert_fingerprint or cert_blob),
        "cert_header_present": bool(cert_blob),
        "raw": payload,
    }

    node_report_payload = _decode_node_report_payload(payload)
    try:
        node_report_id = _store_node_report(node_report_payload, event)
    except OSError as exc:
        logger.warning("Node report storage raised unexpectedly for hostname=%s: %s", hostname, exc)
        node_report_id = ""
    event["node_report_id"] = node_report_id
    event["node_report_present"] = bool(node_report_id)
    
    # Log hardware check data if present
    if event.get("stage") == "hardware-check" and payload.get("hardware"):
        hardware_data = payload.get("hardware")
        logger.info(f"Hardware check reported: hostname={event.get('hostname')}, hardware={hardware_data}")
    
    _trigger_initial_callback_closer(event)
    deduped = _store_callback_event(event)
    return jsonify({"ok": True, "deduped": deduped})


@app.get("/api/cloud-init/reports/<report_id>")
def cloud_init_report_get(report_id: str) -> Any:
    report = _read_node_report(report_id)
    if report is None:
        return jsonify({"ok": False, "error": "report not found"}), 404
    return jsonify({"ok": True, "report": report})


@app.get("/cloud-init/reports/<report_id>")
def cloud_init_report_page(report_id: str) -> Any:
    report = _read_node_report(report_id)
    if report is None:
        return jsonify({"ok": False, "error": "report not found"}), 404
    return render_template("report.html", report_id=report_id, report=report)


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
def sd_prepare() -> Any:
    prereq_errors = _ensure_prereqs()
    if prereq_errors:
        return jsonify({"ok": False, "errors": prereq_errors}), 400

    payload_raw = request.get_json(silent=True) or {}
    payload = _sanitize_sd_prepare_payload(payload_raw if isinstance(payload_raw, dict) else {})
    required = ["profile", "hostname", "device", "ssh_public_key", "callback_url"]
    missing = [k for k in required if not str(payload.get(k, "")).strip()]
    if missing:
        return jsonify({"ok": False, "errors": [f"Missing fields: {', '.join(missing)}"]}), 400

    if payload["profile"] not in {"pi5", "pibplus"}:
        return jsonify({"ok": False, "errors": ["profile must be pi5 or pibplus"]}), 400

    distribution = str(payload.get("distribution", "standard")).strip().lower() or "standard"
    if distribution not in {"standard", "k3s"}:
        return jsonify({"ok": False, "errors": ["distribution must be standard or k3s"]}), 400
    payload["distribution"] = distribution
    payload["sd_options_profile"] = _auto_generate_sd_option_profile(payload)

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
                "cloud_init_options": payload.get("cloud_init_options", {}),
            },
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    thread = threading.Thread(target=_run_prepare_job, args=(job_id, payload), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})


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


TERMINAL_IP_SOURCES = {"source_ip", "callback"}


def _terminal_preferred_host_for_node(node: Dict[str, Any]) -> str:
    callback_ip = str(node.get("callback_source_ip", "") or "").strip()
    if _is_usable_callback_ip(callback_ip):
        return callback_ip
    return str(node.get("address", "") or "").strip()


def _terminal_alias_connect_host_map(nodes: list[Dict[str, Any]]) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "") or "").strip()
        preferred = _terminal_preferred_host_for_node(node)
        if not node_name or not preferred:
            continue
        for alias in _node_aliases(node_name):
            alias_key = str(alias or "").strip().lower()
            if alias_key:
                alias_map[alias_key] = preferred
    return alias_map


def _terminal_event_connect_host(event: Dict[str, Any], alias_map: Dict[str, str]) -> str:
    hostname = str(event.get("hostname", "") or "").strip().lower()
    fqdn = str(event.get("fqdn", "") or "").strip().lower()
    for alias in (hostname, fqdn):
        if alias and alias in alias_map:
            return alias_map[alias]

    source_ip = str(event.get("source_ip", "") or "").strip()
    if _is_usable_callback_ip(source_ip):
        return source_ip
    return ""


def _append_terminal_target(
    targets: list[Dict[str, Any]],
    seen: set[str],
    *,
    host: str,
    source: str,
    event: Dict[str, Any],
    connect_host: str = "",
) -> None:
    value = str(host or "").strip()
    if not value:
        return
    lowered = value.lower()
    if lowered in {"unknown", "none", "-"}:
        return
    if lowered in seen:
        return
    seen.add(lowered)

    hostname = str(event.get("hostname", "") or "").strip()
    fqdn = str(event.get("fqdn", "") or "").strip()
    profile = str(event.get("profile", "") or "").strip()
    target_connect_host = str(connect_host or value).strip()
    targets.append(
        {
            "host": value,
            "connect_host": target_connect_host,
            "source": source,
            "hostname": hostname,
            "fqdn": fqdn,
            "instance_id": str(event.get("instance_id", "") or "").strip(),
            "profile": profile,
            "last_seen": str(event.get("received_at", "") or "").strip(),
            "usable_callback_ip": _is_usable_callback_ip(target_connect_host) if source in TERMINAL_IP_SOURCES else None,
            "label_hint": hostname or fqdn or profile,
        }
    )


def _test_terminal_targets(limit: int = 2000) -> list[Dict[str, Any]]:
    events = _read_events(limit=limit)
    nodes = _node_ops_inventory_view()
    targets: list[Dict[str, Any]] = []
    seen: set[str] = set()
    alias_map = _terminal_alias_connect_host_map(nodes)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "") or "").strip()
        node_address = _terminal_preferred_host_for_node(node)
        callback_ip = str(node.get("callback_source_ip", "") or "").strip()
        if not node_name or not node_address:
            continue
        normalized_source = str(node.get("address_source", "") or "inventory").strip() or "inventory"
        if callback_ip and node_address == callback_ip and _is_usable_callback_ip(callback_ip):
            source = "source_ip"
        elif normalized_source == "callback":
            source = "source_ip"
        else:
            source = normalized_source
        event_like = {
            "hostname": node_name,
            "fqdn": "",
            "profile": str(node.get("callback_profile", "") or "").strip(),
            "instance_id": str(node.get("callback_instance_id", "") or "").strip(),
            "received_at": str(node.get("last_callback", "") or "").strip(),
        }
        _append_terminal_target(targets, seen, host=node_address, source=source, event=event_like, connect_host=node_address)

    for event in events:
        if not isinstance(event, dict):
            continue
        hostname = str(event.get("hostname", "") or "").strip()
        fqdn = str(event.get("fqdn", "") or "").strip()
        source_ip = str(event.get("source_ip", "") or "").strip()
        resolved_connect = _terminal_event_connect_host(event, alias_map)

        _append_terminal_target(
            targets,
            seen,
            host=hostname,
            source="hostname",
            event=event,
            connect_host=resolved_connect or hostname,
        )
        _append_terminal_target(
            targets,
            seen,
            host=fqdn,
            source="fqdn",
            event=event,
            connect_host=resolved_connect or fqdn,
        )
        _append_terminal_target(
            targets,
            seen,
            host=source_ip,
            source="source_ip",
            event=event,
            connect_host=source_ip,
        )

    return targets


def _resolve_terminal_host(host: str, limit: int = 2000) -> str:
    raw = str(host or "").strip()
    if not raw:
        return ""
    if _normalize_ip(raw):
        return raw

    lowered = raw.lower()
    nodes = _node_ops_inventory_view()
    alias_map = _terminal_alias_connect_host_map(nodes)
    from_nodes = alias_map.get(lowered, "")
    if from_nodes:
        return from_nodes

    for event in _read_events(limit=limit):
        if not isinstance(event, dict):
            continue
        event_connect = _terminal_event_connect_host(event, alias_map)
        if not event_connect:
            continue
        hostname = str(event.get("hostname", "") or "").strip().lower()
        fqdn = str(event.get("fqdn", "") or "").strip().lower()
        if lowered not in {hostname, fqdn}:
            continue
        return event_connect
    return raw


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


@app.get("/api/nodes/summary")
def nodes_summary() -> Any:
    state = _single_source_of_truth_summary()
    provisioning = state.get("provisioning", {}) if isinstance(state.get("provisioning"), dict) else {}
    nodes = [
        node
        for node in (state.get("nodes", []) if isinstance(state.get("nodes", []), list) else [])
        if isinstance(node, dict) and bool(node.get("in_node_ops_view", False))
    ]
    return jsonify(
        {
            "ok": True,
            "nodes": nodes,
            "config": provisioning.get("config", {}),
            "profiles": NODE_PROVISION_PROFILES,
            "node_inputs": provisioning.get("node_inputs", {}),
            "manual_nodes": provisioning.get("manual_nodes", {}),
            "image_summary": state.get("images", {}).get("summary", {}) if isinstance(state.get("images"), dict) else {},
        }
    )


@app.get("/api/monitor/summary")
def monitor_summary() -> Any:
    state = _single_source_of_truth_summary()
    monitor = state.get("monitor", {}) if isinstance(state.get("monitor"), dict) else {}
    nodes = [
        node
        for node in (state.get("nodes", []) if isinstance(state.get("nodes", []), list) else [])
        if isinstance(node, dict) and bool(node.get("in_monitor_view", False))
    ]
    return jsonify(
        {
            "ok": True,
            "nodes": nodes,
            "profiles": NODE_PROVISION_PROFILES,
            "cloud_init_options": monitor.get("cloud_init_options", []),
            "node_cloud_init": monitor.get("node_cloud_init", {}),
        }
    )


@app.get("/api/state/source-of-truth")
def source_of_truth_summary() -> Any:
    return jsonify({"ok": True, "state": _single_source_of_truth_summary()})


@app.get("/api/kubernetes/summary")
def kubernetes_summary() -> Any:
    return jsonify(_kubernetes_local_summary())


@app.post("/api/nodes/stale/prune")
def prune_stale_nodes() -> Any:
    payload = request.get_json(silent=True) or {}
    if payload and not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    raw_hours = payload.get("max_age_hours", 168)
    raw_dry_run = payload.get("dry_run", False)
    if isinstance(raw_dry_run, str):
        dry_run = raw_dry_run.strip().lower() in {"1", "true", "yes", "on"}
    else:
        dry_run = bool(raw_dry_run)
    try:
        max_age_hours = int(raw_hours)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "max_age_hours must be an integer"}), 400
    if max_age_hours < 1 or max_age_hours > 24 * 365:
        return jsonify({"ok": False, "error": "max_age_hours must be between 1 and 8760"}), 400

    result = _prune_stale_nodes(max_age_hours=max_age_hours, dry_run=dry_run)
    return jsonify(result)


@app.post("/api/monitor/nodes")
def monitor_node_upsert() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    try:
        node = _sanitize_managed_node(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    state = _load_monitor_state()
    managed_nodes = state.setdefault("managed_nodes", {})
    managed_nodes[node["name"]] = node
    _save_monitor_state(state)
    return jsonify({"ok": True, "node": node})


@app.delete("/api/monitor/nodes/<node_name>")
def monitor_node_delete(node_name: str) -> Any:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", node_name):
        return jsonify({"ok": False, "error": "invalid node name"}), 400

    state = _load_monitor_state()
    managed_nodes = state.setdefault("managed_nodes", {})
    if node_name not in managed_nodes:
        return jsonify({"ok": False, "error": "managed node not found"}), 404
    managed_nodes.pop(node_name, None)

    node_cloud_init = state.setdefault("node_cloud_init", {})
    node_cloud_init.pop(node_name, None)
    _save_monitor_state(state)
    return jsonify({"ok": True})


@app.get("/api/monitor/nodes/<node_name>/certificate")
def monitor_node_certificate(node_name: str) -> Any:
    cert = _latest_node_cert(node_name, _read_events(limit=2000))
    return jsonify({"ok": True, "node": node_name, "certificate": cert})


@app.get("/api/monitor/nodes/<node_name>/ping")
def monitor_node_ping(node_name: str) -> Any:
    requested = str(node_name or "").strip()
    if not requested:
        return jsonify({"ok": False, "error": "node name is required"}), 400

    summary = _monitor_nodes_summary()
    node = next((item for item in summary.get("nodes", []) if str(item.get("name", "")) == requested), None)
    if not node:
        return jsonify({"ok": False, "error": "node not found"}), 404

    address = str(node.get("address", "")).strip()
    if not address:
        return jsonify({"ok": False, "error": "node has no address configured"}), 400

    status = _ping_status(address)
    return jsonify(
        {
            "ok": True,
            "node": requested,
            "address": address,
            "reachable": bool(status.get("reachable")),
            "latency_ms": status.get("latency_ms"),
            "error": str(status.get("error", "")),
        }
    )


@app.post("/api/monitor/cloud-init/options")
def monitor_cloud_init_option_upsert() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400

    try:
        option = _sanitize_cloud_init_option(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    state = _load_monitor_state()
    cloud_options = state.setdefault("cloud_init_options", {})
    cloud_options[option["id"]] = option
    _save_monitor_state(state)
    return jsonify({"ok": True, "option": option})


@app.delete("/api/monitor/cloud-init/options/<option_id>")
def monitor_cloud_init_option_delete(option_id: str) -> Any:
    option_key = str(option_id).strip().lower()
    state = _load_monitor_state()
    cloud_options = state.setdefault("cloud_init_options", {})
    if option_key not in cloud_options:
        return jsonify({"ok": False, "error": "option not found"}), 404

    cloud_options.pop(option_key, None)
    node_cloud_init = state.setdefault("node_cloud_init", {})
    for node_name, selected in list(node_cloud_init.items()):
        if not isinstance(selected, list):
            continue
        node_cloud_init[node_name] = [v for v in selected if str(v) != option_key]

    _save_monitor_state(state)
    return jsonify({"ok": True})


@app.post("/api/monitor/nodes/<node_name>/cloud-init")
def monitor_node_cloud_init_assign(node_name: str) -> Any:
    payload = request.get_json(silent=True) or {}
    option_ids = payload.get("option_ids") or []
    if not isinstance(option_ids, list):
        return jsonify({"ok": False, "error": "option_ids must be an array"}), 400

    state = _load_monitor_state()
    cloud_options = state.setdefault("cloud_init_options", {})
    valid_ids = [str(v).strip().lower() for v in option_ids if str(v).strip().lower() in cloud_options]
    node_cloud_init = state.setdefault("node_cloud_init", {})
    node_cloud_init[node_name] = valid_ids
    _save_monitor_state(state)
    return jsonify({"ok": True, "node": node_name, "option_ids": valid_ids})


@app.get("/api/nodes/profile-segments")
def nodes_profile_segments() -> Any:
    return jsonify({"ok": True, "segments": _load_profile_segments()})


@app.post("/api/nodes/profile-segments")
def nodes_profile_segments_upsert() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    profile_id = str(payload.get("profile_id", "")).strip()
    if not profile_id:
        return jsonify({"ok": False, "error": "profile_id is required"}), 400

    try:
        entry = _upsert_profile_segment(profile_id, payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True, "profile_id": profile_id, "segment": entry})


@app.post("/api/nodes/config")
def nodes_config() -> Any:
    payload = request.get_json(silent=True) or {}
    config = _save_node_config(
        {
            "username": str(payload.get("username", "")).strip(),
            "password": str(payload.get("password", "")),
            "webhook_url": str(payload.get("webhook_url", "")).strip(),
            "verify_tls": bool(payload.get("verify_tls", True)),
        }
    )
    return jsonify(
        {
            "ok": True,
            "config": {
                "username": config.get("username", ""),
                "password_set": bool(config.get("password")),
                "webhook_url": config.get("webhook_url", ""),
                "verify_tls": bool(config.get("verify_tls", True)),
            },
        }
    )


@app.post("/api/nodes/monitor")
def nodes_monitor() -> Any:
    payload = request.get_json(silent=True) or {}
    emit_webhook = bool(payload.get("send_webhook", False))

    nodes = _node_ops_inventory_view()
    statuses: List[Dict[str, Any]] = []
    for node in nodes:
        status = _ping_status(node["address"])
        statuses.append(
            {
                "name": node["name"],
                "group": node["group"],
                "address": node["address"],
                "address_source": node.get("address_source", "inventory"),
                "reachable": status["reachable"],
                "latency_ms": status["latency_ms"],
                "last_callback": str(node.get("last_callback", "")),
                "error": status["error"],
            }
        )

    webhook_result: Dict[str, Any] = {"sent": False}
    config = _load_node_config()
    webhook_url = config.get("webhook_url", "")
    if emit_webhook and webhook_url:
        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reachable": sum(1 for s in statuses if s["reachable"]),
            "unreachable": sum(1 for s in statuses if not s["reachable"]),
            "nodes": statuses,
        }
        result = _send_webhook(webhook_url, summary, verify_tls=bool(config.get("verify_tls", True)))
        webhook_result = {"sent": True, **result}

    return jsonify({"ok": True, "statuses": statuses, "webhook": webhook_result})


@app.get("/api/nodes/profiles")
def nodes_profiles() -> Any:
    state = _load_node_provisioning()
    return jsonify(
        {
            "ok": True,
            "profiles": NODE_PROVISION_PROFILES,
            "node_configs": state.get("node_configs", {}),
            "node_inputs": state.get("node_inputs", {}),
            "manual_nodes": state.get("manual_nodes", {}),
        }
    )


@app.post("/api/nodes/manual-nodes")
def nodes_manual_node_upsert() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("name", "")).strip()
    if not node_name:
        return jsonify({"ok": False, "error": "name is required"}), 400
    if not re.fullmatch(r"[A-Za-z0-9._-]+", node_name):
        return jsonify({"ok": False, "error": "invalid node name"}), 400

    group = str(payload.get("group", "manual")).strip() or "manual"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", group):
        return jsonify({"ok": False, "error": "invalid group"}), 400

    certificate = str(payload.get("certificate", "")).strip()
    if not certificate:
        return jsonify({"ok": False, "error": "certificate is required"}), 400

    state = _load_node_provisioning()
    manual_nodes = state.setdefault("manual_nodes", {})
    manual_nodes[node_name] = {
        "name": node_name,
        "group": group,
        "address": str(payload.get("address", "")).strip(),
        "username": str(payload.get("username", "")).strip(),
        "password": str(payload.get("password", "")),
        "default_shell": str(payload.get("default_shell", "")).strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    manual_nodes[node_name]["certificate"] = certificate
    _save_node_provisioning(state)

    redacted = dict(manual_nodes[node_name])
    redacted["password_set"] = bool(redacted.get("password"))
    redacted.pop("password", None)
    return jsonify({"ok": True, "node": redacted})


@app.delete("/api/nodes/manual-nodes/<node_name>")
def nodes_manual_node_delete(node_name: str) -> Any:
    name = str(node_name or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "node name is required"}), 400
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return jsonify({"ok": False, "error": "invalid node name"}), 400

    state = _load_node_provisioning()
    manual_nodes = state.setdefault("manual_nodes", {})
    if name not in manual_nodes:
        return jsonify({"ok": False, "error": "manual node not found"}), 404

    manual_nodes.pop(name, None)
    node_configs = state.setdefault("node_configs", {})
    node_inputs = state.setdefault("node_inputs", {})
    node_results = state.setdefault("results", {})
    node_configs.pop(name, None)
    node_inputs.pop(name, None)
    node_results.pop(name, None)
    _save_node_provisioning(state)
    return jsonify({"ok": True, "node": name})


@app.post("/api/nodes/node-configs")
def nodes_node_configs() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("node", "")).strip()
    profile_ids = payload.get("profile_ids") or []

    if not node_name:
        return jsonify({"ok": False, "error": "node is required"}), 400
    if not isinstance(profile_ids, list):
        return jsonify({"ok": False, "error": "profile_ids must be an array"}), 400

    known_nodes = {n["name"] for n in _node_ops_inventory_view()}
    if node_name not in known_nodes:
        return jsonify({"ok": False, "error": "node not found in active node set"}), 404

    valid_profile_ids = [str(pid) for pid in profile_ids if str(pid) in _profile_map()]
    state = _load_node_provisioning()
    node_configs = state.setdefault("node_configs", {})
    node_configs[node_name] = valid_profile_ids
    _save_node_provisioning(state)
    return jsonify({"ok": True, "node": node_name, "profile_ids": valid_profile_ids})


@app.post("/api/nodes/node-inputs")
def nodes_node_inputs() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("node", "")).strip()
    raw_inputs = payload.get("profile_inputs") or {}

    if not node_name:
        return jsonify({"ok": False, "error": "node is required"}), 400
    if not isinstance(raw_inputs, dict):
        return jsonify({"ok": False, "error": "profile_inputs must be an object"}), 400

    nodes = _node_ops_inventory_view()
    node = next((n for n in nodes if n.get("name") == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "node not found in active node set"}), 404

    state = _load_node_provisioning()
    profile_ids = _effective_node_profiles(node, state)
    sanitized = _sanitize_node_profile_inputs(raw_inputs, profile_ids=profile_ids)

    node_inputs = state.setdefault("node_inputs", {})
    node_inputs[node_name] = sanitized
    _save_node_provisioning(state)
    return jsonify({"ok": True, "node": node_name})


def _merged_telegraf_inputs(state: Dict[str, Any], node_name: str, raw_inputs: Any) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    saved_node_inputs = state.get("node_inputs", {}).get(node_name, {}) if isinstance(state.get("node_inputs", {}), dict) else {}

    if isinstance(saved_node_inputs, dict):
        saved_telegraf = saved_node_inputs.get("telegraf", {})
        if isinstance(saved_telegraf, dict):
            merged.update({str(k): str(v) for k, v in saved_telegraf.items()})

    if isinstance(raw_inputs, dict):
        candidate = raw_inputs.get("telegraf")
        if isinstance(candidate, dict):
            merged.update({str(k): str(v) for k, v in candidate.items()})
        else:
            direct_keys = {
                "influx_endpoint",
                "influx_bucket",
                "influx_org",
                "influx_access_key",
                "influx_secret_key",
            }
            direct_values = {k: str(v) for k, v in raw_inputs.items() if str(k) in direct_keys}
            if direct_values:
                merged.update(direct_values)

    return merged


@app.post("/api/nodes/telegraf/test")
def nodes_telegraf_test() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("node", "")).strip()
    raw_inputs = payload.get("profile_inputs") or {}

    if not node_name:
        return jsonify({"ok": False, "error": "node is required"}), 400

    nodes = _node_ops_inventory_view()
    node = next((n for n in nodes if n.get("name") == node_name), None)
    if not node:
        return jsonify({"ok": False, "error": "node not found in active node set"}), 404

    state = _load_node_provisioning()
    telegraf_inputs = _merged_telegraf_inputs(state, node_name, raw_inputs)

    check = _telegraf_influx_dummy_check(telegraf_inputs)
    status_code = 200 if check.get("ok") else 400
    return jsonify({"ok": bool(check.get("ok")), "node": node_name, "result": check}), status_code


@app.post("/api/nodes/provision")
def nodes_provision() -> Any:
    payload = request.get_json(silent=True) or {}
    node_name = str(payload.get("node", "")).strip()
    mode = str(payload.get("mode", "check")).strip().lower()
    run_ansible = bool(payload.get("run_ansible", True))

    if mode not in {"check", "apply"}:
        return jsonify({"ok": False, "error": "mode must be check or apply"}), 400

    nodes = _node_ops_inventory_view()
    if node_name:
        target_nodes = [n for n in nodes if n["name"] == node_name]
        if not target_nodes:
            return jsonify({"ok": False, "error": "node not found in active node set"}), 404
    else:
        target_nodes = nodes

    state = _load_node_provisioning()
    missing_by_node: List[Dict[str, Any]] = []

    for node in target_nodes:
        profile_ids = _effective_node_profiles(node, state)
        missing = _missing_required_inputs(node["name"], profile_ids, state)
        if missing and mode == "apply":
            missing_by_node.append(
                {
                    "node": node["name"],
                    "profiles": missing,
                }
            )

    if missing_by_node:
        return jsonify(
            {
                "ok": False,
                "error": "missing required provisioning inputs",
                "missing_inputs": missing_by_node,
            }
        ), 400

    if run_ansible:
        job_id = uuid.uuid4().hex
        with PROVISION_JOBS_LOCK:
            PROVISION_JOBS[job_id] = {
                "id": job_id,
                "status": "queued",
                "mode": mode,
                "run_ansible": run_ansible,
                "target_nodes": [node["name"] for node in target_nodes],
                "results": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        thread = threading.Thread(
            target=_run_node_provision_job,
            args=(job_id, [node["name"] for node in target_nodes], mode, run_ansible),
            daemon=True,
        )
        thread.start()
        return jsonify({"ok": True, "mode": mode, "run_ansible": run_ansible, "job_id": job_id, "status": "queued"}), 202

    results: List[Dict[str, Any]] = []
    for node in target_nodes:
        profile_ids = _effective_node_profiles(node, state)
        results.append(_apply_node_provisioning(node["name"], profile_ids, mode=mode, run_ansible=run_ansible))

    return jsonify({"ok": True, "mode": mode, "run_ansible": run_ansible, "results": results})


@app.get("/api/nodes/provision/jobs/<job_id>")
def node_provision_job_status(job_id: str) -> Any:
    with PROVISION_JOBS_LOCK:
        job = PROVISION_JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify({"ok": True, "job": job})


@app.post("/api/settings/reset")
def settings_reset() -> Any:
    payload = request.get_json(silent=True) or {}
    scope = str(payload.get("scope", "node_ops")).strip().lower()

    if scope != "node_ops":
        return jsonify({"ok": False, "error": "scope must be node_ops"}), 400

    reset_result = _reset_node_ops_settings()
    return jsonify({"ok": True, "scope": scope, "reset": reset_result})


@app.get("/api/sd/jobs/<job_id>")
def sd_job(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.get("/api/sd/image-catalog")
def sd_image_catalog() -> Any:
    return jsonify({"ok": True, "entries": _catalog_entries_for_ui()})


@app.get("/api/sd/catalog")
def sd_catalog() -> Any:
    return jsonify({"ok": True, "entries": _catalog_entries_for_ui()})


@app.post("/api/sd/catalog-pull")
def sd_catalog_pull() -> Any:
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("catalog_ids") or []
    skip_existing = bool(payload.get("skip_existing", True))

    if not isinstance(raw_ids, list):
        return jsonify({"ok": False, "error": "catalog_ids must be an array"}), 400

    requested_ids = [str(v).strip() for v in raw_ids if str(v).strip()]
    if not requested_ids:
        return jsonify({"ok": False, "error": "at least one catalog id is required"}), 400

    by_id = {str(e.get("id", "")): e for e in IMAGE_CATALOG}
    selected: List[Dict[str, Any]] = []
    missing: List[str] = []
    no_url: List[str] = []
    for catalog_id in requested_ids:
        entry = by_id.get(catalog_id)
        if not entry:
            missing.append(catalog_id)
            continue
        if not str(entry.get("image_url", "")).strip():
            no_url.append(catalog_id)
            continue
        selected.append(entry)

    if not selected:
        return jsonify(
            {
                "ok": False,
                "error": "no downloadable catalog entries selected",
                "missing_ids": missing,
                "missing_url_ids": no_url,
            }
        ), 400

    _ensure_image_cache_dir()
    job_id = uuid.uuid4().hex
    with CATALOG_PULL_JOBS_LOCK:
        CATALOG_PULL_JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "storage_dir": str(IMAGE_CACHE_DIR),
            "requested_ids": requested_ids,
            "accepted_ids": [str(e.get("id", "")) for e in selected],
            "missing_ids": missing,
            "missing_url_ids": no_url,
            "skip_existing": skip_existing,
            "results": [],
            "summary": {"ok": 0, "failed": 0, "skipped": 0, "total": 0},
        }

    thread = threading.Thread(target=_run_catalog_pull_job, args=(job_id, selected, skip_existing), daemon=True)
    thread.start()
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "accepted_ids": [str(e.get("id", "")) for e in selected],
            "missing_ids": missing,
            "missing_url_ids": no_url,
            "storage_dir": str(IMAGE_CACHE_DIR),
        }
    )


@app.get("/api/sd/catalog-pull/jobs/<job_id>")
def sd_catalog_pull_job(job_id: str) -> Any:
    with CATALOG_PULL_JOBS_LOCK:
        job = CATALOG_PULL_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


@app.get("/api/sd/images")
def sd_images() -> Any:
    return jsonify({"ok": True, "entries": _read_image_history(limit=500)})


@app.post("/api/sd/images")
def sd_images_create() -> Any:
    payload = request.get_json(silent=True) or {}
    record = _upsert_image_record(payload)
    return jsonify({"ok": True, "record": record}), 201


@app.put("/api/sd/images/<record_id>")
def sd_images_update(record_id: str) -> Any:
    if not _find_image_record(record_id):
        return jsonify({"ok": False, "error": "image record not found"}), 404
    payload = request.get_json(silent=True) or {}
    payload["id"] = record_id
    record = _upsert_image_record(payload, record_id=record_id)
    return jsonify({"ok": True, "record": record})


@app.delete("/api/sd/images/<record_id>")
def sd_images_delete(record_id: str) -> Any:
    deleted = _delete_image_record(record_id)
    if not deleted:
        return jsonify({"ok": False, "error": "image record not found"}), 404
    return jsonify({"ok": True})


if __name__ == "__main__":
    maybe_reexec_with_sudo()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8088"))
    app.run(host=host, port=port, debug=False, threaded=True)
