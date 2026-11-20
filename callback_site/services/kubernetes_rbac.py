"""Playbook-managed ClusterRoleBindings for Tailscale identities reaching the API server proxy.

In auth mode the Tailscale operator impersonates the caller's tailnet identity, so a
binding for ``Group tag:homelab-controller`` or ``User someone@example.com`` decides
what that caller may do. Only bindings labelled as managed by Playbook can be changed
or deleted here; system and hand-made bindings are never touched.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from services import helm
from services.tailscale_operator import _kubectl

API_GROUP = "rbac.authorization.k8s.io"
MANAGED_LABEL = "app.kubernetes.io/managed-by"
MANAGED_VALUE = "playbook"
ALLOWED_ROLES = ("view", "edit", "admin", "cluster-admin")
SUBJECT_KINDS = ("Group", "User")
MAX_SUBJECTS = 20

_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_SUBJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]{0,252}$")


def _validated_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("Binding name must be lowercase letters, digits, '-' or '.', at most 253 characters.")
    if name.startswith("system:") or name.startswith("system-"):
        raise ValueError("Binding names starting with 'system' are reserved for Kubernetes.")
    return name


def _validated_subjects(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("Add at least one Group or User subject.")
    if len(raw) > MAX_SUBJECTS:
        raise ValueError(f"At most {MAX_SUBJECTS} subjects per binding.")
    subjects: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each subject needs a kind and a name.")
        kind = str(item.get("kind") or "").strip()
        name = str(item.get("name") or "").strip()
        if kind not in SUBJECT_KINDS:
            raise ValueError("Subject kind must be Group or User.")
        if not _SUBJECT_RE.match(name):
            raise ValueError(f"Invalid subject name: {name!r}. Use a Tailscale tag, group or login such as tag:homelab-controller.")
        if name.startswith("system:"):
            raise ValueError("Kubernetes system: identities cannot be bound here.")
        entry = {"apiGroup": API_GROUP, "kind": kind, "name": name}
        if entry not in subjects:
            subjects.append(entry)
    return subjects


def _manifest(name: str, role: str, subjects: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "apiVersion": f"{API_GROUP}/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name, "labels": {MANAGED_LABEL: MANAGED_VALUE}},
        "roleRef": {"apiGroup": API_GROUP, "kind": "ClusterRole", "name": role},
        "subjects": subjects,
    }


def _row(item: Dict[str, Any]) -> Dict[str, Any]:
    metadata = item.get("metadata") or {}
    role_ref = item.get("roleRef") or {}
    return {
        "name": str(metadata.get("name", "")),
        "role": str(role_ref.get("name", "")),
        "subjects": [
            {"kind": str(s.get("kind", "")), "name": str(s.get("name", ""))}
            for s in (item.get("subjects") or []) if isinstance(s, dict)
        ],
        "created_at": str(metadata.get("creationTimestamp", "")),
    }


def _get(connection: List[str], name: str) -> Dict[str, Any] | None:
    try:
        data = json.loads(helm._command([*_kubectl(), *connection, "get", "clusterrolebinding", name, "--ignore-not-found", "-o", "json"], timeout=30) or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("kubectl returned non-JSON output") from exc
    return data if isinstance(data, dict) and data else None


def _is_managed(item: Dict[str, Any]) -> bool:
    labels = (item.get("metadata") or {}).get("labels") or {}
    return labels.get(MANAGED_LABEL) == MANAGED_VALUE


def list_bindings() -> Dict[str, Any]:
    connection = helm._helm_connection()
    output = helm._command(
        [*_kubectl(), *connection, "get", "clusterrolebindings", "-l", f"{MANAGED_LABEL}={MANAGED_VALUE}", "-o", "json"],
        timeout=30,
    )
    try:
        items = json.loads(output or "{}").get("items") or []
    except (json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError("kubectl returned non-JSON output") from exc
    rows = sorted((_row(item) for item in items if isinstance(item, dict)), key=lambda row: row["name"])
    return {"bindings": rows, "roles": list(ALLOWED_ROLES), "subject_kinds": list(SUBJECT_KINDS)}


def save_binding(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create or update a managed binding. roleRef is immutable, so a role change recreates the binding."""
    name = _validated_name(name)
    role = str(payload.get("role") or "").strip()
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ALLOWED_ROLES)}.")
    subjects = _validated_subjects(payload.get("subjects"))
    connection = helm._helm_connection()
    existing = _get(connection, name)
    if existing and not _is_managed(existing):
        raise PermissionError(f"ClusterRoleBinding {name} exists but is not managed by Playbook; it was left unchanged.")
    if existing and (existing.get("roleRef") or {}).get("name") != role:
        helm._command([*_kubectl(), *connection, "delete", "clusterrolebinding", name], timeout=60)
    manifest = _manifest(name, role, subjects)
    with tempfile.TemporaryDirectory(prefix="playbook-rbac-") as directory:
        path = Path(directory) / "binding.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        helm._command([*_kubectl(), *connection, "apply", "-f", str(path)], timeout=60)
    return {"binding": _row(manifest), "created": existing is None}


def delete_binding(name: str) -> None:
    name = _validated_name(name)
    connection = helm._helm_connection()
    existing = _get(connection, name)
    if existing is None:
        raise KeyError(f"ClusterRoleBinding {name} not found.")
    if not _is_managed(existing):
        raise PermissionError(f"ClusterRoleBinding {name} is not managed by Playbook; it was left unchanged.")
    helm._command([*_kubectl(), *connection, "delete", "clusterrolebinding", name], timeout=60)
