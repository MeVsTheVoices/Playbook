"""K3s config text checks and the long-form field checker."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Any, Callable, Dict, List
from urllib import parse as urlparse

import yaml

from domain_defaults import K3S_API_ENDPOINT_DEFAULT

from services.node_state import _profile_input_spec_map


def _normalize_k3s_node_name(node_name: str | None) -> str:
    candidate = str(node_name or "").strip()
    if not candidate:
        candidate = socket.gethostname().strip() or "rack-node"
    candidate = candidate.strip("._ ")
    candidate = re.sub(r"[^A-Za-z0-9-]+", "-", candidate)
    candidate = re.sub(r"-+", "-", candidate).strip("-")
    if not candidate:
        candidate = "rack-node"
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", candidate):
        candidate = "rack-node"
    return candidate


def _default_k3s_config(node_name: str | None = None) -> str:
    host = os.getenv("K3S_SERVER_HOST", "").strip() or urlparse.urlparse(K3S_API_ENDPOINT_DEFAULT).hostname
    node_name = _normalize_k3s_node_name(node_name)
    return f"# pragma: k3s-config-v1\nserver: https://{host}:6443\nnode-name: {node_name}\n"


LONGFORM_CHECK_MAX_CHARS = 65536


_K3S_LIST_KEYS = ("node-label", "node-taint", "kubelet-arg", "tls-san")
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_TRUE_TEXT = {"true", "1", "yes", "on"}


def _outside_tailscale(value: Any) -> bool:
    try:
        return ipaddress.ip_address(str(value).strip()) not in _TAILSCALE_NET
    except ValueError:
        return False


def _check_k3s_config(text: str) -> Dict[str, List[str]]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        return {"errors": [f"YAML syntax error{where}: {problem}"], "warnings": []}
    if loaded is None:
        return {"errors": [], "warnings": []}
    if not isinstance(loaded, dict):
        return {"errors": ["Config must be a YAML mapping of 'key: value' lines."], "warnings": []}

    errors: List[str] = []
    warnings: List[str] = []
    server = loaded.get("server")
    if server is not None and not re.fullmatch(r"https://(\[[0-9a-fA-F:]+\]|[^\s/:\[\]]+):\d+/?", str(server)):
        errors.append("'server' must look like https://host:6443.")
    for key in _K3S_LIST_KEYS:
        if key in loaded and not isinstance(loaded[key], (str, list)):
            errors.append(f"'{key}' must be a string or a list.")
    labels = loaded.get("node-label")
    for label in labels if isinstance(labels, list) else [labels]:
        if str(label or "").startswith("node-role.kubernetes.io/"):
            errors.append("'node-label' cannot set node-role.kubernetes.io/* labels (K3s rejects them); use role=control-plane.")
    if str(loaded.get("secrets-encryption", "")).lower() == "true":
        warnings.append("'secrets-encryption' is on, but the current cluster has encryption disabled; joining nodes will fail.")
    if server is not None and str(loaded.get("cluster-init", "")).lower() in _TRUE_TEXT:
        errors.append("'cluster-init' starts a new cluster, but 'server' joins an existing one. Remove 'cluster-init' on a joining node.")
    if server is not None and not str(loaded.get("token") or "").strip():
        warnings.append("'server' is set but 'token' is not; the node cannot join without the cluster token.")
    for key in ("node-ip", "advertise-address"):
        if key in loaded and _outside_tailscale(loaded[key]):
            warnings.append(
                f"'{key}' is not a Tailscale address (100.64.0.0/10). The rack joins over Tailscale, and etcd peer certificates "
                "are tied to the address a server advertises, so a LAN or NAT address breaks control-plane joins."
            )
    return {"errors": errors, "warnings": warnings}


LONGFORM_CHECKS: Dict[str, Callable[[str], Dict[str, List[str]]]] = {"k3s_config": _check_k3s_config}


def _check_longform(kind: str, text: Any) -> Dict[str, Any]:
    check = LONGFORM_CHECKS.get(kind)
    if check is None:
        return {"ok": False, "errors": [f"Unknown check kind: {kind}"], "warnings": []}
    body = str(text or "")
    if len(body) > LONGFORM_CHECK_MAX_CHARS:
        return {"ok": False, "errors": [f"Config is longer than {LONGFORM_CHECK_MAX_CHARS} characters."], "warnings": []}
    result = check(body)
    return {"ok": not result["errors"], **result}


def _longform_input_errors(profile_id: str, values: Dict[str, str]) -> List[str]:
    specs = _profile_input_spec_map(profile_id)
    errors: List[str] = []
    for field_id, value in values.items():
        spec = specs.get(field_id, {})
        if spec.get("check") and value:
            label = spec.get("label", field_id)
            errors.extend(f"{label}: {err}" for err in _check_longform(spec["check"], value)["errors"])
    return errors
