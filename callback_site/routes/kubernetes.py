"""Optional Kubernetes module: cluster page, summary and cleanup endpoints, Helm and the Tailscale operator."""

from __future__ import annotations

from typing import Any

from flask import jsonify, render_template, request

from routes.helm import register_helm_routes
from services.kubernetes import (
    NODE_NAME_RE,
    _clear_stale_nodes,
    _clear_stale_pods,
    _kubernetes_local_summary,
)


def _clear_request_options() -> tuple[int, bool, Any]:
    """(min_minutes, dry_run, error response or None) for the cluster cleanup endpoints; dry run is the default."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return 0, True, (jsonify({"ok": False, "error": "invalid payload"}), 400)
    try:
        min_minutes = int(payload.get("min_minutes", 10))
    except (TypeError, ValueError):
        return 0, True, (jsonify({"ok": False, "error": "min_minutes must be an integer"}), 400)
    if min_minutes < 0 or min_minutes > 10080:
        return 0, True, (jsonify({"ok": False, "error": "min_minutes must be between 0 and 10080"}), 400)
    return min_minutes, payload.get("dry_run", True) is not False, None


def _single_removal_response(result: dict) -> Any:
    if not result.get("ok") and "removed" not in result:
        return jsonify(result), 502
    if result.get("removed"):
        return jsonify(result)
    if result.get("failed"):
        return jsonify(result), 502
    skipped = result.get("skipped") or [{}]
    reason = str(skipped[0].get("reason", "not eligible"))
    return jsonify({**result, "ok": False, "error": reason}), 404 if reason == "not found" else 409


def register_kubernetes_routes(app: Any) -> None:
    @app.get("/kubernetes")
    def kubernetes_page() -> str:
        return render_template("kubernetes.html")

    @app.get("/api/kubernetes/summary")
    def kubernetes_summary() -> Any:
        return jsonify(_kubernetes_local_summary())

    @app.post("/api/kubernetes/nodes/clear")
    def kubernetes_clear_nodes() -> Any:
        min_minutes, dry_run, error = _clear_request_options()
        if error:
            return error
        result = _clear_stale_nodes(min_minutes=min_minutes, dry_run=dry_run)
        return jsonify(result), 200 if result.get("ok") or "removed" in result else 502

    @app.delete("/api/kubernetes/nodes/<name>")
    def kubernetes_remove_node(name: str) -> Any:
        if not NODE_NAME_RE.fullmatch(name) or len(name) > 253:
            return jsonify({"ok": False, "error": "invalid node name"}), 400
        return _single_removal_response(_clear_stale_nodes(min_minutes=0, dry_run=False, names=[name]))

    @app.post("/api/kubernetes/pods/clear")
    def kubernetes_clear_pods() -> Any:
        min_minutes, dry_run, error = _clear_request_options()
        if error:
            return error
        result = _clear_stale_pods(min_minutes=min_minutes, dry_run=dry_run)
        return jsonify(result), 200 if result.get("ok") or "removed" in result else 502

    @app.delete("/api/kubernetes/pods/<namespace>/<name>")
    def kubernetes_remove_pod(namespace: str, name: str) -> Any:
        for value in (namespace, name):
            if not NODE_NAME_RE.fullmatch(value) or len(value) > 253:
                return jsonify({"ok": False, "error": "invalid pod name"}), 400
        return _single_removal_response(_clear_stale_pods(min_minutes=0, dry_run=False, targets=[(namespace, name)]))

    register_helm_routes(app)
