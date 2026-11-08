from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from services import helm

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
                return jsonify(ok=True, configurations=helm.configurations(), examples=helm.EXAMPLES)
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

    app.register_blueprint(api)
