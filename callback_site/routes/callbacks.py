"""Cloud-init callback, event and report endpoints."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from flask import jsonify, render_template, request

from services.boot_logs import _schedule_boot_log_fetch
from services.callback_payload import (
    _extract_payload_profile,
    _normalize_hostname,
    _normalize_instance_id,
    _normalize_profile,
    _normalize_stage,
    _recent_event_defaults,
)
from services.events import _clear_events, _read_events, _store_callback_event
from services.node_archive import active_nodes
from services.network import _header_or_payload, _resolve_source_ip
from services.node_views import _trigger_initial_callback_closer
from services.reports import _decode_node_report_payload, _read_node_report, _store_node_report
from services.sd_prepare import _strip_dataset_fields

logger = logging.getLogger(__name__)
INITIAL_CALLBACK_LOCK = threading.Lock()


def register_callback_routes(app: Any) -> None:
    @app.get("/api/cloud-init/events")
    def events() -> Any:
        return jsonify({"events": active_nodes(_read_events(limit=500))})

    @app.post("/api/cloud-init/events/clear")
    def clear_events() -> Any:
        cleared = _clear_events()
        return jsonify({"ok": True, "cleared": cleared})

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
            "SSL_CLIENT_S_DN",
            "SSL_CLIENT_SUBJECT_DN",
            "SSL_CLIENT_CERT_SUBJECT",
            "cert_subject",
        )
        cert_issuer = _header_or_payload(
            payload,
            "X-SSL-Client-Issuer",
            "X-SSL-Client-Cert-Issuer",
            "X-Client-Cert-Issuer",
            "TLS-Client-Issuer",
            "SSL-Client-Issuer",
            "SSL_CLIENT_I_DN",
            "SSL_CLIENT_ISSUER_DN",
            "SSL_CLIENT_CERT_ISSUER",
            "cert_issuer",
        )
        cert_fingerprint = _header_or_payload(
            payload,
            "X-SSL-Client-Fingerprint",
            "X-SSL-Client-Cert-Fingerprint",
            "X-Client-Cert-Fingerprint",
            "TLS-Client-Fingerprint",
            "SSL-Client-Fingerprint",
            "SSL_CLIENT_SHA256",
            "SSL_CLIENT_CERT_SHA256",
            "cert_fingerprint",
        )
        cert_blob = _header_or_payload(
            payload,
            "X-SSL-Client-Cert",
            "X-Client-Cert",
            "SSL-Client-Cert",
            "SSL_CLIENT_CERT",
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
    
        # Publish the callback address before a worker reads inventory; serialize first-callback scheduling.
        with INITIAL_CALLBACK_LOCK:
            deduped = _store_callback_event(event)
            job_id = _trigger_initial_callback_closer(event) if not deduped else ""
        _schedule_boot_log_fetch(event)
        return jsonify({"ok": True, "deduped": deduped, "closer_job_id": job_id})

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
