from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from services import helm, kubernetes_rbac, tailscale_operator

logger = logging.getLogger(__name__)


def register_helm_routes(app: Any) -> None:
    api = Blueprint("helm", __name__)

    @api.get("/api/helm/configurations")
    @api.post("/api/helm/configurations/import")
    @api.put("/api/helm/configurations/<record_id>")
    @api.post("/api/helm/configurations/<record_id>/<action>")
    def configurations(record_id: str = "", action: str = "") -> Any:
        try:
            if request.method == "GET":
                return jsonify(ok=True, configurations=helm.configurations(), examples=helm.EXAMPLES, bundled=helm.BUNDLED)
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("A JSON object is required.")
            if request.path.endswith("/import"):
                result = {"configuration": helm.import_chart(payload)}
            elif request.method == "PUT":
                result = {"configuration": helm.save_configuration(record_id, payload)}
            elif action in {"preview", "deploy"}:
                if action == "deploy" and payload.get("confirm") is not True:
                    raise ValueError("Deployment requires explicit confirmation.")
                result = helm.run_configuration(record_id, payload.get("revision"), deploy=action == "deploy")
            elif action == "dockerbot-token":
                helm.set_dockerbot_token(record_id, payload)
                result = {}
            else:
                return jsonify(ok=False, error="Unknown Helm operation."), 404
            return jsonify(ok=True, **result)
        except ValueError as exc:
            logger.warning("Helm request rejected: %s", exc)
            return jsonify(ok=False, error=str(exc)), 400
        except KeyError as exc:
            return jsonify(ok=False, error=str(exc)), 404
        except (RuntimeError, OSError) as exc:
            logger.error("Helm operation failed: %s", exc)
            return jsonify(ok=False, error=str(exc)), 502

    @api.get("/api/kubernetes/tailscale-operator")
    def operator_status() -> Any:
        return jsonify(ok=True, status=tailscale_operator.operator_status())

    @api.post("/api/kubernetes/tailscale-operator")
    def operator_install() -> Any:
        payload = request.get_json(silent=True)
        try:
            if not isinstance(payload, dict):
                raise ValueError("A JSON object is required.")
            if payload.get("confirm") is not True:
                raise ValueError("Changing the cluster requires explicit confirmation.")
            return jsonify(ok=True, **tailscale_operator.install_operator(payload))
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except (RuntimeError, OSError) as exc:
            # Helm errors never contain the values file contents; the OAuth secret is not logged.
            logger.error("Tailscale operator operation failed: %s", exc)
            return jsonify(ok=False, error=str(exc)), 502

    @api.get("/api/kubernetes/rbac/bindings")
    @api.put("/api/kubernetes/rbac/bindings/<name>")
    @api.delete("/api/kubernetes/rbac/bindings/<name>")
    def rbac_bindings(name: str = "") -> Any:
        try:
            if request.method == "GET":
                return jsonify(ok=True, **kubernetes_rbac.list_bindings())
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("A JSON object is required.")
            if payload.get("confirm") is not True:
                raise ValueError("Changing cluster RBAC requires explicit confirmation.")
            if request.method == "DELETE":
                kubernetes_rbac.delete_binding(name)
                logger.warning("Deleted ClusterRoleBinding %s", name)
                return jsonify(ok=True)
            result = kubernetes_rbac.save_binding(name, payload)
            logger.warning("Saved ClusterRoleBinding %s -> %s", name, result["binding"]["role"])
            return jsonify(ok=True, **result)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except PermissionError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        except KeyError as exc:
            return jsonify(ok=False, error=str(exc.args[0] if exc.args else exc)), 404
        except (RuntimeError, OSError) as exc:
            logger.error("Kubernetes RBAC operation failed: %s", exc)
            return jsonify(ok=False, error=str(exc)), 502

    app.register_blueprint(api)
