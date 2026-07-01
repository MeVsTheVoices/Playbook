from __future__ import annotations

import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from core import *

# Setup logs directory
LOG_DIR = Path(os.getenv("CALLBACK_APPDATA_DIR", ".")) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "callback_site.log"

# Configure logging with rotation
log_handler = RotatingFileHandler(
    str(LOG_FILE),
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=10,  # Keep 10 rotated files
)
log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log_handler.setFormatter(log_formatter)

# Get root logger and configure
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(log_handler)

# Also log to console
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.get("/")
def index() -> str:
    _ensure_device_monitor_started()
    events = _read_events(limit=200)
    rack_name = _load_app_settings().get("rack_name", "homelab")
    option_profiles = _load_sd_option_profiles()
    return render_template(
        "index.html",
        events=events,
        image_history=_read_image_history(limit=150),
        image_summary=_image_state_summary(),
        hostname_prefix=f"{rack_name}-",
        sd_option_profiles=option_profiles,
        devices=_list_sd_devices(),
        callback_url_default=_default_callback_url(),
        image_catalog=IMAGE_CATALOG,
    )


@app.get("/tailscale-acl")
def tailscale_acl() -> str:
    return render_template("tailscale_acl.html")


@app.get("/fragments/tailscale-acl/overview")
def tailscale_acl_overview_fragment() -> str:
    return render_template("fragments/tailscale_acl_overview.html")


@app.get("/fragments/tailscale-acl/inputs")
def tailscale_acl_inputs_fragment() -> str:
    return render_template("fragments/tailscale_acl_inputs.html", defaults=_tailscale_policy_defaults())


@app.get("/fragments/tailscale-acl/output")
def tailscale_acl_output_fragment() -> str:
    return render_template("fragments/tailscale_acl_output.html")


@app.get("/fragments/tailscale-acl/apply-guide")
def tailscale_acl_apply_guide_fragment() -> str:
    return render_template("fragments/tailscale_acl_apply_guide.html")


@app.get("/nodes")
def nodes_page() -> str:
    return render_template("nodes.html")


@app.get("/monitor")
def monitor_page() -> str:
    return render_template("monitor.html")


@app.get("/images")
def images_page() -> str:
    image_history = _read_image_history(limit=500)
    return render_template("images.html", image_catalog=IMAGE_CATALOG, image_history=image_history, image_records=image_history)


@app.get("/settings")
def settings_page() -> str:
    return render_template("settings.html")


@app.get("/api/settings")
def settings_get() -> Any:
    settings = _load_app_settings()
    settings["log_file"] = str(LOG_FILE)
    settings["verbose_logging"] = root_logger.level == logging.DEBUG
    return jsonify({"ok": True, "settings": settings})


@app.post("/api/logging/verbose")
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


@app.get("/api/logging/tail")
def logging_tail() -> Any:
    lines = request.args.get("lines", "50", type=int)
    try:
        with open(LOG_FILE, "r") as f:
            log_lines = f.readlines()[-lines:]
        return jsonify({"ok": True, "log_file": str(LOG_FILE), "lines": log_lines})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "log file not found"}), 404


@app.get("/api/sd/options-profiles")
def sd_options_profiles_get() -> Any:
    profiles = _load_sd_option_profiles()
    ordered = [profiles[name] for name in sorted(profiles)]
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

    profiles = _load_sd_option_profiles()
    profiles[name] = {
        "name": name,
        "description": description,
        "options": options,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_sd_option_profiles(profiles)
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


@app.post("/api/settings")
def settings_update() -> Any:
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "invalid payload"}), 400
    settings = _save_app_settings(payload)
    return jsonify({"ok": True, "settings": settings})


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True})


@app.get("/api/cloud-init/events")
def events() -> Any:
    return jsonify({"events": _read_events(limit=500)})


@app.post("/api/cloud-init/events/clear")
def clear_events() -> Any:
    cleared = _clear_events()
    return jsonify({"ok": True, "cleared": cleared})


@app.get("/fragments/cloud-init/event-rows")
def cloud_init_event_rows() -> str:
    return render_template("fragments/callback_rows.html", events=_read_events(limit=200))


@app.post("/api/cloud-init/callback")
def callback() -> Any:
    payload_json = request.get_json(silent=True)
    payload_form = request.form.to_dict(flat=True)
    payload: Dict[str, Any] = payload_json if isinstance(payload_json, dict) and payload_json else payload_form
    payload = _strip_dataset_fields(payload)
    logger.debug(f"Callback received with payload keys: {list(payload.keys())}")

    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    source_ip, source_ip_source = _resolve_source_ip(payload)
    cert_subject = _header_or_payload(
        payload,
        "X-SSL-Client-Subject",
        "X-Client-Cert-Subject",
        "TLS-Client-Subject",
        "cert_subject",
    )
    cert_issuer = _header_or_payload(
        payload,
        "X-SSL-Client-Issuer",
        "X-Client-Cert-Issuer",
        "TLS-Client-Issuer",
        "cert_issuer",
    )
    cert_fingerprint = _header_or_payload(
        payload,
        "X-SSL-Client-Fingerprint",
        "X-Client-Cert-Fingerprint",
        "TLS-Client-Fingerprint",
        "cert_fingerprint",
    )

    event = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source_ip": source_ip,
        "source_ip_source": source_ip_source,
        "forwarded_for": forwarded_for,
        "remote_addr": request.remote_addr,
        "user_agent": request.headers.get("User-Agent", ""),
        "instance_id": payload.get("instance_id") or request.form.get("instance_id"),
        "hostname": payload.get("hostname") or request.form.get("hostname"),
        "fqdn": payload.get("fqdn") or request.form.get("fqdn"),
        "profile": payload.get("profile"),
        "stage": payload.get("stage"),
        "cert_subject": cert_subject,
        "cert_issuer": cert_issuer,
        "cert_fingerprint": cert_fingerprint,
        "cert_present": bool(cert_subject or cert_fingerprint),
        "raw": payload,
    }
    
    # Log hardware check data if present
    if event.get("stage") == "hardware-check" and payload.get("hardware"):
        hardware_data = payload.get("hardware")
        logger.info(f"Hardware check reported: hostname={event.get('hostname')}, hardware={hardware_data}")
    
    _append_event(event)
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

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "request": {
                "profile": payload["profile"],
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


@app.get("/api/nodes/summary")
def nodes_summary() -> Any:
    config = _load_node_config()
    nodes = _node_ops_inventory_view()
    provision = _load_node_provisioning()

    enriched_nodes: List[Dict[str, Any]] = []
    for node in nodes:
        enriched = dict(node)
        selected = _effective_node_profiles(node, provision)
        enriched["selected_profiles"] = selected
        enriched["latest_check"] = _latest_node_check(node["name"], provision)
        enriched_nodes.append(enriched)

    masked = {
        "username": config.get("username", ""),
        "password_set": bool(config.get("password")),
        "webhook_url": config.get("webhook_url", ""),
        "verify_tls": bool(config.get("verify_tls", True)),
    }
    return jsonify(
        {
            "ok": True,
            "nodes": enriched_nodes,
            "config": masked,
            "profiles": NODE_PROVISION_PROFILES,
            "node_inputs": provision.get("node_inputs", {}),
            "image_summary": _image_state_summary(),
        }
    )


@app.get("/api/monitor/summary")
def monitor_summary() -> Any:
    summary = _monitor_nodes_summary()
    return jsonify(
        {
            "ok": True,
            "nodes": summary["nodes"],
            "profiles": NODE_PROVISION_PROFILES,
            "cloud_init_options": summary["cloud_init_options"],
            "node_cloud_init": summary["node_cloud_init"],
        }
    )


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
        }
    )


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
    results: List[Dict[str, Any]] = []
    missing_by_node: List[Dict[str, Any]] = []

    for node in target_nodes:
        profile_ids = _effective_node_profiles(node, state)
        missing = _missing_required_inputs(node["name"], profile_ids, state)
        if missing:
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

    for node in target_nodes:
        profile_ids = _effective_node_profiles(node, state)
        results.append(_apply_node_provisioning(node["name"], profile_ids, mode=mode, run_ansible=run_ansible))

    return jsonify({"ok": True, "mode": mode, "run_ansible": run_ansible, "results": results})


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
    return jsonify({"ok": True, "entries": _read_image_history(limit=300)})


@app.get("/api/sd/catalog")
def sd_catalog() -> Any:
    available_urls = _available_image_urls_from_history(limit=5000)
    entries = [_catalog_entry_with_availability(entry, available_urls=available_urls) for entry in IMAGE_CATALOG]
    return jsonify({"ok": True, "entries": entries})


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


@app.get("/fragments/sd/image-catalog-rows")
def sd_image_catalog_rows() -> str:
    return render_template("fragments/image_catalog_rows.html", image_history=_read_image_history(limit=150))


@app.get("/api/sd/images")
def sd_images() -> Any:
    return jsonify({"ok": True, "entries": _read_image_history(limit=500)})


@app.get("/fragments/sd/images-rows")
def sd_images_rows() -> str:
    return render_template("fragments/managed_image_rows.html", image_records=_read_image_history(limit=500))


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
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8088"))
    app.run(host=host, port=port, debug=False, threaded=True)
