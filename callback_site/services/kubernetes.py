"""Local Kubernetes runtime detection and the cluster summary."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


KUBERNETES_STATUS_LOCK = threading.Lock()


KUBERNETES_STATUS_CACHE: Dict[str, Any] = {
    "checked_at": 0.0,
    "state": {
        "available": False,
        "active_service": "",
        "k3s_binary": "",
        "has_kubeconfig": False,
        "reason": "not checked",
    },
}


_K3S_VERSION_TTL_SECONDS = 600
_K3S_VERSION_CACHE: Dict[str, Any] = {"checked_at": 0.0, "version": ""}


def _local_k3s_version() -> str:
    """The K3s version installed on this machine (e.g. v1.36.4+k3s1), or '' when K3s is not installed."""
    now = time.time()
    with KUBERNETES_STATUS_LOCK:
        if now - float(_K3S_VERSION_CACHE["checked_at"]) < _K3S_VERSION_TTL_SECONDS:
            return str(_K3S_VERSION_CACHE["version"])
    binary = shutil.which("k3s") or "/usr/local/bin/k3s"
    version = ""
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False, timeout=5)
        found = re.search(r"v\d+\.\d+\.\d+\+k3s\d+", str(proc.stdout or ""))
        version = found.group(0) if found else ""
    except Exception:
        version = ""
    with KUBERNETES_STATUS_LOCK:
        _K3S_VERSION_CACHE.update({"checked_at": now, "version": version})
    return version


def _service_is_active(service_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return False
    return proc.returncode == 0 and str(proc.stdout or "").strip() == "active"


def _detect_kubernetes_runtime(force_refresh: bool = False) -> Dict[str, Any]:
    now = time.time()
    with KUBERNETES_STATUS_LOCK:
        cached_at = float(KUBERNETES_STATUS_CACHE.get("checked_at", 0.0) or 0.0)
        if not force_refresh and now - cached_at < 8.0:
            cached = KUBERNETES_STATUS_CACHE.get("state", {})
            return dict(cached) if isinstance(cached, dict) else {
                "available": False,
                "active_service": "",
                "k3s_binary": "",
                "has_kubeconfig": False,
                "reason": "invalid cache",
            }

    k3s_binary = shutil.which("k3s") or ""
    if not k3s_binary:
        candidate = Path("/usr/local/bin/k3s")
        if candidate.exists() and candidate.is_file():
            k3s_binary = str(candidate)

    has_kubeconfig = Path("/etc/rancher/k3s/k3s.yaml").exists()
    active_service = ""
    if _service_is_active("k3s"):
        active_service = "k3s"
    elif _service_is_active("k3s-agent"):
        active_service = "k3s-agent"

    reason = ""
    if active_service:
        reason = f"{active_service} service is active"
    elif k3s_binary:
        reason = "k3s binary found but service is inactive"
    else:
        reason = "k3s service and binary not found"

    state = {
        "available": bool(active_service),
        "active_service": active_service,
        "k3s_binary": k3s_binary,
        "has_kubeconfig": bool(has_kubeconfig),
        "reason": reason,
    }

    with KUBERNETES_STATUS_LOCK:
        KUBERNETES_STATUS_CACHE["checked_at"] = now
        KUBERNETES_STATUS_CACHE["state"] = state

    return state


def _run_kubectl_json(args: List[str]) -> tuple[Dict[str, Any] | None, str]:
    candidate_cmds: List[List[str]] = []
    k3s_cmd = shutil.which("k3s")
    if k3s_cmd:
        candidate_cmds.append([k3s_cmd, "kubectl", *args])
    kubectl_cmd = shutil.which("kubectl")
    if kubectl_cmd:
        candidate_cmds.append([kubectl_cmd, *args])

    if not candidate_cmds:
        return None, "k3s/kubectl command not available"

    errors: List[str] = []
    for cmd in candidate_cmds:
        try:
            proc = subprocess.run(
                [*cmd, "-o", "json"],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
        except Exception as exc:
            errors.append(f"{' '.join(cmd[:2])}: {exc}")
            continue

        if proc.returncode != 0:
            stderr = str(proc.stderr or proc.stdout or "").strip()
            errors.append(stderr or f"command failed ({proc.returncode})")
            continue

        try:
            return json.loads(proc.stdout or "{}"), ""
        except json.JSONDecodeError:
            errors.append("kubectl returned non-JSON output")

    return None, " | ".join(e for e in errors if e)[:1000] or "kubectl command failed"


def _kubernetes_local_summary() -> Dict[str, Any]:
    runtime = _detect_kubernetes_runtime(force_refresh=False)
    summary: Dict[str, Any] = {
        "ok": True,
        "available": bool(runtime.get("available", False)),
        "runtime": runtime,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "nodes_total": 0,
        "nodes_ready": 0,
        "control_plane_nodes": 0,
        "pods_total": 0,
        "pods_running": 0,
        "namespaces_total": 0,
        "jobs_total": 0,
        "jobs_running": 0,
        "longhorn_namespace_present": False,
        "longhorn_pods_total": 0,
        "longhorn_pods_running": 0,
        "longhorn_ready": False,
        "node_rows": [],
        "pod_rows": [],
        "namespace_rows": [],
        "job_rows": [],
        "last_error": "",
    }

    if not summary["available"]:
        return summary

    nodes_doc, nodes_err = _run_kubectl_json(["get", "nodes"])
    pods_doc, pods_err = _run_kubectl_json(["get", "pods", "-A"])
    ns_doc, ns_err = _run_kubectl_json(["get", "namespaces"])
    jobs_doc, jobs_err = _run_kubectl_json(["get", "jobs", "-A"])

    if nodes_doc is None:
        summary["last_error"] = nodes_err
        return summary

    node_items = nodes_doc.get("items", []) if isinstance(nodes_doc, dict) else []
    if not isinstance(node_items, list):
        node_items = []
    summary["nodes_total"] = len(node_items)

    longhorn_namespace_present = False
    if isinstance(ns_doc, dict):
        ns_items_for_longhorn = ns_doc.get("items", []) if isinstance(ns_doc.get("items"), list) else []
        for namespace in ns_items_for_longhorn:
            if not isinstance(namespace, dict):
                continue
            metadata = namespace.get("metadata", {}) if isinstance(namespace.get("metadata"), dict) else {}
            if str(metadata.get("name", "")).strip() == "longhorn-system":
                longhorn_namespace_present = True
                break
    summary["longhorn_namespace_present"] = longhorn_namespace_present

    nodes_ready = 0
    control_plane_nodes = 0
    for item in node_items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
        labels = metadata.get("labels", {}) if isinstance(metadata.get("labels"), dict) else {}
        if "node-role.kubernetes.io/control-plane" in labels or str(labels.get("role", "")) == "control-plane":
            control_plane_nodes += 1

        status = item.get("status", {}) if isinstance(item.get("status"), dict) else {}
        conditions = status.get("conditions", []) if isinstance(status.get("conditions"), list) else []
        is_ready = any(
            isinstance(cond, dict)
            and str(cond.get("type", "")) == "Ready"
            and str(cond.get("status", "")) == "True"
            for cond in conditions
        )
        if is_ready:
            nodes_ready += 1

    summary["nodes_ready"] = nodes_ready
    summary["control_plane_nodes"] = control_plane_nodes

    node_rows: List[Dict[str, Any]] = []
    for item in node_items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {}
        labels = metadata.get("labels", {}) if isinstance(metadata.get("labels"), dict) else {}
        status = item.get("status", {}) if isinstance(item.get("status"), dict) else {}
        conditions = status.get("conditions", []) if isinstance(status.get("conditions"), list) else []
        is_ready = any(
            isinstance(cond, dict)
            and str(cond.get("type", "")) == "Ready"
            and str(cond.get("status", "")) == "True"
            for cond in conditions
        )
        addresses = status.get("addresses", []) if isinstance(status.get("addresses"), list) else []
        internal_ip = ""
        external_ip = ""
        for address in addresses:
            if not isinstance(address, dict):
                continue
            address_type = str(address.get("type", ""))
            address_value = str(address.get("address", ""))
            if address_type == "InternalIP" and not internal_ip:
                internal_ip = address_value
            elif address_type == "ExternalIP" and not external_ip:
                external_ip = address_value
        node_rows.append(
            {
                "name": str(metadata.get("name", "")),
                "ready": is_ready,
                "role": "control-plane" if "node-role.kubernetes.io/control-plane" in labels or str(labels.get("role", "")) == "control-plane" else "worker",
                "internal_ip": internal_ip,
                "external_ip": external_ip,
                "os_image": str(status.get("nodeInfo", {}).get("osImage", "")) if isinstance(status.get("nodeInfo"), dict) else "",
                "kernel": str(status.get("nodeInfo", {}).get("kernelVersion", "")) if isinstance(status.get("nodeInfo"), dict) else "",
                "container_runtime": str(status.get("nodeInfo", {}).get("containerRuntimeVersion", "")) if isinstance(status.get("nodeInfo"), dict) else "",
                "labels": ", ".join(sorted(str(k) for k in labels.keys())[:6]),
            }
        )
    summary["node_rows"] = node_rows

    if isinstance(pods_doc, dict):
        pod_items = pods_doc.get("items", []) if isinstance(pods_doc.get("items"), list) else []
        summary["pods_total"] = len(pod_items)
        pod_rows: List[Dict[str, Any]] = []
        pods_running = 0
        longhorn_pods_total = 0
        longhorn_pods_running = 0
        for pod in pod_items:
            if not isinstance(pod, dict):
                continue
            metadata = pod.get("metadata", {}) if isinstance(pod.get("metadata"), dict) else {}
            status = pod.get("status", {}) if isinstance(pod.get("status"), dict) else {}
            phase = str(status.get("phase", ""))
            if phase == "Running":
                pods_running += 1
            if str(metadata.get("namespace", "")).strip() == "longhorn-system":
                longhorn_pods_total += 1
                if phase == "Running":
                    longhorn_pods_running += 1
            container_statuses = status.get("containerStatuses", []) if isinstance(status.get("containerStatuses"), list) else []
            ready_count = 0
            restart_count = 0
            for container_status in container_statuses:
                if not isinstance(container_status, dict):
                    continue
                if container_status.get("ready") is True:
                    ready_count += 1
                try:
                    restart_count += int(container_status.get("restartCount", 0) or 0)
                except (TypeError, ValueError):
                    continue
            pod_rows.append(
                {
                    "namespace": str(metadata.get("namespace", "")),
                    "name": str(metadata.get("name", "")),
                    "phase": phase,
                    "node": str(status.get("nodeName", "")),
                    "ready_containers": ready_count,
                    "restart_count": restart_count,
                    "created_at": str(metadata.get("creationTimestamp", "")),
                }
            )
        summary["pods_running"] = pods_running
        summary["longhorn_pods_total"] = longhorn_pods_total
        summary["longhorn_pods_running"] = longhorn_pods_running
        summary["longhorn_ready"] = bool(
            longhorn_namespace_present and longhorn_pods_total > 0 and longhorn_pods_running == longhorn_pods_total
        )
        summary["pod_rows"] = pod_rows[:60]

    if isinstance(ns_doc, dict):
        ns_items = ns_doc.get("items", []) if isinstance(ns_doc.get("items"), list) else []
        summary["namespaces_total"] = len(ns_items)
        namespace_rows: List[Dict[str, Any]] = []
        pod_namespace_counts: Dict[str, Dict[str, int]] = {}
        for pod_row in summary.get("pod_rows", []):
            if not isinstance(pod_row, dict):
                continue
            namespace = str(pod_row.get("namespace", ""))
            if not namespace:
                continue
            counts = pod_namespace_counts.setdefault(namespace, {"total": 0, "running": 0})
            counts["total"] += 1
            if str(pod_row.get("phase", "")) == "Running":
                counts["running"] += 1
        for ns in ns_items:
            if not isinstance(ns, dict):
                continue
            metadata = ns.get("metadata", {}) if isinstance(ns.get("metadata"), dict) else {}
            status = ns.get("status", {}) if isinstance(ns.get("status"), dict) else {}
            name = str(metadata.get("name", ""))
            counts = pod_namespace_counts.get(name, {"total": 0, "running": 0})
            namespace_rows.append(
                {
                    "name": name,
                    "phase": str(status.get("phase", "")),
                    "total_pods": counts.get("total", 0),
                    "running_pods": counts.get("running", 0),
                }
            )
        summary["namespace_rows"] = namespace_rows

    if isinstance(jobs_doc, dict):
        job_items = jobs_doc.get("items", []) if isinstance(jobs_doc.get("items"), list) else []
        summary["jobs_total"] = len(job_items)
        job_rows: List[Dict[str, Any]] = []
        jobs_running = 0
        for job in job_items:
            if not isinstance(job, dict):
                continue
            metadata = job.get("metadata", {}) if isinstance(job.get("metadata"), dict) else {}
            status = job.get("status", {}) if isinstance(job.get("status"), dict) else {}
            active = 0
            succeeded = 0
            failed = 0
            try:
                active = int(status.get("active", 0) or 0)
            except (TypeError, ValueError):
                active = 0
            try:
                succeeded = int(status.get("succeeded", 0) or 0)
            except (TypeError, ValueError):
                succeeded = 0
            try:
                failed = int(status.get("failed", 0) or 0)
            except (TypeError, ValueError):
                failed = 0
            if active > 0:
                jobs_running += 1
            conditions = status.get("conditions", []) if isinstance(status.get("conditions"), list) else []
            if conditions:
                last_condition = conditions[-1]
                if isinstance(last_condition, dict):
                    condition_status = str(last_condition.get("type", "")).strip()
                    if condition_status == "Complete":
                        status_text = "Succeeded"
                    elif condition_status == "Failed":
                        status_text = "Failed"
                    elif active > 0:
                        status_text = "Running"
                    else:
                        status_text = condition_status or "Unknown"
                else:
                    status_text = "Unknown"
            elif active > 0:
                status_text = "Running"
            elif succeeded > 0:
                status_text = "Succeeded"
            elif failed > 0:
                status_text = "Failed"
            else:
                status_text = "Pending"
            job_rows.append(
                {
                    "namespace": str(metadata.get("namespace", "default")),
                    "name": str(metadata.get("name", "")),
                    "status": status_text,
                    "active": active,
                    "succeeded": succeeded,
                    "failed": failed,
                }
            )
        summary["jobs_running"] = jobs_running
        summary["job_rows"] = job_rows[:50]

    if not summary["last_error"] and (pods_err or ns_err or jobs_err):
        summary["last_error"] = pods_err or ns_err or jobs_err

    return summary
