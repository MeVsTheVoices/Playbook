"""Tailscale Kubernetes operator with its API server proxy (in-process, single replica).

The operator joins the tailnet as ``tag:k8s-operator`` and proxies kubectl traffic on
tcp 443 to the kube-apiserver. In auth mode it impersonates the caller's Tailscale
identity, so access is decided by Kubernetes RBAC (see tailscale_policy.py for the grant).

OAuth credentials are written to a 0600 values file for the Helm run only and are
never stored by the dashboard; the chart keeps them in its own Kubernetes Secret.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import yaml

from services import helm
from services.settings import _load_app_settings

CHART_REPO = "https://pkgs.tailscale.com/helmcharts"
CHART = "tailscale-operator"
RELEASE = "tailscale-operator"
NAMESPACE = "tailscale"
DEFAULT_HOSTNAME = "tailscale-operator"
READERS_GROUP = "tailnet-readers"
READERS_BINDING = "tailnet-readers-view"
PROXY_MODES = {"auth": "true", "noauth": "noauth"}
INSTALL_TIMEOUT_SECONDS = 300

_HOSTNAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9]{6,64}$")
_CLIENT_SECRET_RE = re.compile(r"^tskey-client-[A-Za-z0-9-]{8,200}$")

READERS_BINDING_MANIFEST = {
    "apiVersion": "rbac.authorization.k8s.io/v1",
    "kind": "ClusterRoleBinding",
    "metadata": {"name": READERS_BINDING, "labels": {"app.kubernetes.io/managed-by": "playbook"}},
    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "view"},
    "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": READERS_GROUP}],
}


def _kubectl() -> list[str]:
    return ["kubectl"] if shutil.which("kubectl") else ["k3s", "kubectl"]


def _tailnet() -> str:
    return str(_load_app_settings().get("tailscale_tailnet", "") or "").strip()


def _kubeconfig_command(hostname: str) -> str:
    return f"tailscale configure kubeconfig {hostname}"


def _json_or_none(args: list[str]) -> Dict[str, Any] | None:
    try:
        data = json.loads(helm._command(args, timeout=30))
    except (RuntimeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def operator_status() -> Dict[str, Any]:
    """What is installed, never the OAuth values."""
    tailnet = _tailnet()
    status: Dict[str, Any] = {
        "installed": False, "release": RELEASE, "namespace": NAMESPACE, "tailnet": tailnet,
        "chart_version": "", "app_version": "", "helm_status": "", "proxy_mode": "", "hostname": "",
        "ready_replicas": 0, "replicas": 0, "readers_binding": False, "proxy_url": "", "kubeconfig_command": "",
        "error": "",
    }
    try:
        connection = helm._helm_connection()
    except RuntimeError as exc:
        status["error"] = str(exc)
        return status
    try:
        listed = json.loads(helm._command(["helm", "list", *connection, "--namespace", NAMESPACE, "--filter", f"^{RELEASE}$", "--output", "json"], timeout=30))
    except (RuntimeError, json.JSONDecodeError) as exc:
        status["error"] = str(exc)
        return status
    if not isinstance(listed, list):
        listed = []
    entry = next((item for item in listed if isinstance(item, dict) and item.get("name") == RELEASE), None)
    if entry:
        chart = str(entry.get("chart", ""))
        status.update(
            installed=True,
            helm_status=str(entry.get("status", "")),
            chart_version=chart.removeprefix(f"{CHART}-"),
            app_version=str(entry.get("app_version", "")),
        )
        values = _json_or_none(["helm", "get", "values", RELEASE, *connection, "--namespace", NAMESPACE, "--output", "json"]) or {}
        proxy = values.get("apiServerProxyConfig") if isinstance(values.get("apiServerProxyConfig"), dict) else {}
        operator = values.get("operatorConfig") if isinstance(values.get("operatorConfig"), dict) else {}
        mode = str(proxy.get("mode", "") or "").lower()
        status["proxy_mode"] = {"true": "auth", "noauth": "noauth"}.get(mode, "off")
        status["hostname"] = str(operator.get("hostname", "") or DEFAULT_HOSTNAME)
        deployment = _json_or_none([*_kubectl(), *connection, "get", "deployment", "operator", "-n", NAMESPACE, "-o", "json"]) or {}
        spec, state = deployment.get("spec") or {}, deployment.get("status") or {}
        status["replicas"] = int(spec.get("replicas", 0) or 0)
        status["ready_replicas"] = int(state.get("readyReplicas", 0) or 0)
        if status["proxy_mode"] != "off":
            status["kubeconfig_command"] = _kubeconfig_command(status["hostname"])
            if tailnet:
                status["proxy_url"] = f"https://{status['hostname']}.{tailnet}"
    binding = _json_or_none([*_kubectl(), *connection, "get", "clusterrolebinding", READERS_BINDING, "-o", "json"])
    status["readers_binding"] = bool(binding)
    if entry:
        status["live"] = live_checks(connection)
    return status


def _text_or_empty(args: list[str]) -> str:
    try:
        return helm._command(args, timeout=30)
    except RuntimeError:
        return ""


def _pod_check(connection: list[str]) -> Dict[str, Any]:
    pods = _json_or_none([*_kubectl(), *connection, "get", "pods", "-n", NAMESPACE, "-l", "app=operator", "-o", "json"]) or {}
    items = [item for item in pods.get("items") or [] if isinstance(item, dict)]
    if not items:
        return {"found": False}
    pod = max(items, key=lambda item: str((item.get("metadata") or {}).get("creationTimestamp", "")))
    state = pod.get("status") or {}
    containers = [c for c in state.get("containerStatuses") or [] if isinstance(c, dict)]
    return {
        "found": True,
        "name": str((pod.get("metadata") or {}).get("name", "")),
        "node": str((pod.get("spec") or {}).get("nodeName", "")),
        "phase": str(state.get("phase", "")),
        "ready": bool(containers) and all(c.get("ready") for c in containers),
        "restarts": sum(int(c.get("restartCount", 0) or 0) for c in containers),
        "started_at": str(state.get("startTime", "")),
    }


def _tailnet_check(connection: list[str]) -> Dict[str, Any]:
    """Read only the login name and node ID from the operator's tsnet state; keys stay in the cluster."""
    secret = _json_or_none([*_kubectl(), *connection, "get", "secret", "operator", "-n", NAMESPACE, "-o", "json"]) or {}
    data = secret.get("data") if isinstance(secret.get("data"), dict) else {}
    result: Dict[str, Any] = {"logged_in": False, "name": "", "node_id": "", "certificate": False}
    try:
        current = base64.b64decode(data.get("_current-profile", "")).decode().strip()
        profiles = json.loads(base64.b64decode(data.get("_profiles", "")) or b"{}")
    except (ValueError, UnicodeDecodeError):
        return result
    # _current-profile names the state key ("profile-8215"); _profiles is keyed by the bare ID.
    profile = (profiles.get(current) or profiles.get(current.removeprefix("profile-"))) if isinstance(profiles, dict) else None
    if isinstance(profile, dict) and profile.get("NodeID"):
        result.update(logged_in=True, name=str(profile.get("Name", "")), node_id=str(profile.get("NodeID", "")))
    result["certificate"] = any(key.endswith(".crt") for key in data)
    return result


def _log_check(connection: list[str]) -> Dict[str, Any]:
    base = [*_kubectl(), *connection, "logs", "deployment/operator", "-n", NAMESPACE]
    early = _text_or_empty([*base, "--limit-bytes=1048576"])
    recent = _text_or_empty([*base, "--tail=300"])
    errors: list[str] = []
    for line in recent.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("level") == "error":
            errors.append(str(entry.get("msg") or entry.get("error") or "")[:240])
    return {
        "startup_complete": "Startup complete" in early,
        "proxy_listening": "API server proxy" in early and "listening" in early,
        "dns_timeouts": recent.count("i/o timeout"),
        "recent_errors": len(errors),
        "last_error": errors[-1] if errors else "",
    }


def _network_check(connection: list[str]) -> Dict[str, Any]:
    nodes = _json_or_none([*_kubectl(), *connection, "get", "nodes", "-o", "json"]) or {}
    rows = []
    for node in nodes.get("items") or []:
        meta = (node.get("metadata") if isinstance(node, dict) else None) or {}
        address = str((meta.get("annotations") or {}).get("flannel.alpha.coreos.com/public-ip", ""))
        rows.append({"node": str(meta.get("name", "")), "address": address, "tailscale": _is_tailscale(address)})
    return {"nodes": rows, "ok": bool(rows) and all(row["tailscale"] for row in rows)}


def _is_tailscale(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def live_checks(connection: list[str]) -> Dict[str, Any]:
    """Runtime health: pod, tailnet login, logs and pod network, plus a one-line verdict."""
    pod = _pod_check(connection)
    tailnet = _tailnet_check(connection)
    logs = _log_check(connection)
    network = _network_check(connection)
    if not pod.get("found"):
        verdict, healthy = "No operator pod found.", False
    elif not pod.get("ready"):
        verdict, healthy = f"Operator pod is {pod.get('phase') or 'not ready'}.", False
    elif not network["ok"]:
        verdict, healthy = "Flannel is not using Tailscale on every node; pods on other nodes can't reach the API or DNS.", False
    elif not tailnet["logged_in"]:
        verdict, healthy = "The operator has not joined the tailnet. Check the OAuth client and the tag:k8s-operator policy.", False
    elif not logs["startup_complete"]:
        verdict, healthy = "The operator is still starting.", False
    elif logs["dns_timeouts"]:
        verdict, healthy = "The operator is logging DNS/API timeouts.", False
    else:
        verdict, healthy = "Working: the operator is on the tailnet and the API proxy is listening.", True
    return {
        "healthy": healthy, "verdict": verdict, "pod": pod, "tailnet": tailnet, "logs": logs, "network": network,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _validated(payload: Dict[str, Any]) -> Dict[str, Any]:
    hostname = str(payload.get("hostname") or DEFAULT_HOSTNAME).strip().lower()
    if not _HOSTNAME_RE.match(hostname):
        raise ValueError("Hostname must be a lowercase DNS label (letters, digits and hyphens, at most 63 characters).")
    mode = str(payload.get("proxy_mode") or "auth").strip().lower()
    if mode not in PROXY_MODES:
        raise ValueError("Proxy mode must be 'auth' (Tailscale identity + Kubernetes RBAC) or 'noauth'.")
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    if bool(client_id) != bool(client_secret):
        raise ValueError("Enter both the OAuth client ID and secret, or neither to keep the installed ones.")
    if client_id and not _CLIENT_ID_RE.match(client_id):
        raise ValueError("The OAuth client ID should be letters and digits only.")
    if client_secret and not _CLIENT_SECRET_RE.match(client_secret):
        raise ValueError("The OAuth client secret should start with tskey-client-.")
    return {
        "hostname": hostname, "mode": mode, "client_id": client_id, "client_secret": client_secret,
        "readers_binding": payload.get("readers_binding") is True,
    }


def install_operator(payload: Dict[str, Any]) -> Dict[str, Any]:
    """helm upgrade --install the operator with the API server proxy enabled."""
    options = _validated(payload)
    connection = helm._helm_connection()
    installed = operator_status().get("installed", False)
    if not installed and not options["client_id"]:
        raise ValueError("The first install needs an OAuth client ID and secret (tagged tag:k8s-operator).")
    values: Dict[str, Any] = {
        "apiServerProxyConfig": {"mode": PROXY_MODES[options["mode"]]},
        "operatorConfig": {"hostname": options["hostname"]},
    }
    if options["client_id"]:
        values["oauth"] = {"clientId": options["client_id"], "clientSecret": options["client_secret"]}
    with tempfile.TemporaryDirectory(prefix="ts-operator-") as directory:
        values_file = Path(directory) / "values.yaml"
        fd = os.open(values_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(values, handle)
        args = [
            "helm", "upgrade", "--install", RELEASE, CHART, "--repo", CHART_REPO, *connection,
            "--namespace", NAMESPACE, "--create-namespace", "--values", str(values_file),
            "--wait", "--timeout", f"{INSTALL_TIMEOUT_SECONDS - 30}s", "--output", "json",
        ]
        if installed:
            # Keep the OAuth Secret and any hand-set values; new chart defaults still apply.
            args.append("--reset-then-reuse-values")
        output = helm._command(args, timeout=INSTALL_TIMEOUT_SECONDS)
    try:
        revision = json.loads(output).get("version")
    except (json.JSONDecodeError, AttributeError):
        revision = None
    if options["readers_binding"]:
        bind_readers()
    return {"revision": revision, "status": operator_status()}


def bind_readers() -> None:
    """Bind the tailnet-readers group (from the policy's 'view' access) to the built-in view ClusterRole."""
    connection = helm._helm_connection()
    with tempfile.TemporaryDirectory(prefix="ts-rbac-") as directory:
        manifest = Path(directory) / "binding.json"
        manifest.write_text(json.dumps(READERS_BINDING_MANIFEST), encoding="utf-8")
        helm._command([*_kubectl(), *connection, "apply", "-f", str(manifest)], timeout=60)
