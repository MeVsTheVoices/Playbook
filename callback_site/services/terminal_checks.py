"""Named verifier checks and the test-terminal job runner."""

from __future__ import annotations

import subprocess
import logging
import codecs
import re
import shlex
import threading
from datetime import datetime, timezone
from typing import Any, Dict

from services.events import _read_events
from services.network import _is_usable_callback_ip, _normalize_ip
from services.connection_profiles import gather_profiles
from services.node_inventory import _node_ops_inventory_view
from services.node_state import _node_aliases
from services.node_archive import active_nodes


TEST_TERMINAL_JOBS: Dict[str, Dict[str, Any]] = {}


TEST_TERMINAL_LOCK = threading.Lock()
TERMINAL_OUTPUT_LIMIT = 20_000
TERMINAL_TIMEOUT_SECONDS = 120
logger = logging.getLogger(__name__)


TERMINAL_CHECKS: Dict[str, Dict[str, Any]] = {
    "identity": {
        "label": "Identity + Kernel",
        "category": "System",
        "description": "Validate hostname/user/kernel baseline",
        "command": "hostname && whoami && uname -a",
    },
    "cloud_init": {
        "label": "Cloud-Init State",
        "category": "Boot & logs",
        "description": "Check cloud-init completion and status",
        "command": "cloud-init status --long || cloud-init status",
    },
    "service_baseline": {
        "label": "Service Baseline",
        "category": "System",
        "description": "Quick system health and networking status",
        "command": "systemctl is-system-running || true; systemctl --failed --no-legend || true; ip -brief address",
    },
    "boot_log": {
        "label": "Boot Log",
        "category": "Boot & logs",
        "description": "Failed units, this boot's journal and the cloud-init output log (last lines)",
        "command": (
            'printf "=== failed units ===\\n"; systemctl --failed --no-pager || true; '
            'printf "\\n=== journal, this boot (last 300) ===\\n"; sudo -n journalctl -b --no-pager -o short-iso -n 300 || journalctl -b --no-pager -n 300 || true; '
            'printf "\\n=== cloud-init-output.log (last 200) ===\\n"; sudo -n tail -n 200 /var/log/cloud-init-output.log || true'
        ),
    },
    "block_devices": {
        "label": "Block Devices",
        "category": "Inventory",
        "description": "Detail all block devices, partitions, filesystems, and mounts",
        "command": (
            'printf "=== lsblk ===\\n"; lsblk -o NAME,KNAME,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,ROTA,RM || lsblk || true; '
            'printf "\\n=== blkid ===\\n"; sudo -n blkid || blkid || true; '
            'printf "\\n=== partitions ===\\n"; cat /proc/partitions || true; '
            'printf "\\n=== disk usage ===\\n"; df -h -x tmpfs -x devtmpfs || df -h || true'
        ),
    },
    "remote_inventory": {
        "label": "Remote Inventory",
        "category": "Inventory",
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

KUBERNETES_PARAMETERS = {
    "runner": {"label": "Kubectl client on the SSH host", "choices": ["kubectl", "sudo -n kubectl", "sudo -n k3s kubectl"], "default": "kubectl"},
    "context": {"label": "Context (blank: client's current context)", "flag": "--context"},
    "namespace": {"label": "Namespace (blank: context default)", "flag": "--namespace"},
    "all_namespaces": {"label": "All namespaces (overrides Namespace)", "type": "checkbox", "flag": "--all-namespaces"},
    "selector": {"label": "Label selector", "placeholder": "app=nginx,tier!=cache", "flag": "--selector"},
    "output": {"label": "Output", "choices": ["", "wide", "yaml", "json", "name"], "flag": "--output"},
    "resource": {"label": "Resource (kind/name)", "placeholder": "deployment/my-app", "required": True},
    "pod": {"label": "Pod name", "placeholder": "my-app-abc123", "required": True},
    "container": {"label": "Container (blank: pod default)", "flag": "--container"},
    "tail": {"label": "Log lines (1–2000)", "type": "number", "min": 1, "max": 2000, "default": "200", "flag": "--tail"},
    "previous": {"label": "Previous container instance", "type": "checkbox", "flag": "--previous"},
}

_KUBE_SCOPE = ("namespace", "all_namespaces", "selector", "output")
_KUBE_TASKS = [
    ("nodes", "🖥️ Nodes & roles", "Kubernetes · Cluster", "Node readiness, roles and addresses.", ("get", "nodes"), ("selector", "output")),
    ("namespaces", "🗂️ Namespaces", "Kubernetes · Cluster", "Available namespaces and their status.", ("get", "namespaces"), ("selector", "output")),
    ("version", "🏷️ Client & server versions", "Kubernetes · Cluster", "Compare kubectl and API server versions.", ("version",), ()),
    ("health", "🩺 API readiness", "Kubernetes · Cluster", "Detailed API readiness, including etcd checks.", ("get", "--raw=/readyz?verbose"), ()),
    ("api_resources", "🧭 API resources", "Kubernetes · Cluster", "Discover resource kinds, scope and supported verbs.", ("api-resources",), ()),
    ("contexts", "🔀 Kubeconfig contexts", "Kubernetes · Cluster", "List configured contexts without exposing kubeconfig credentials.", ("config", "get-contexts"), ()),
    ("permissions", "🔐 Allowed actions", "Kubernetes · Cluster", "List this client's permissions in the selected namespace.", ("auth", "can-i", "--list"), ("namespace",)),
    ("pods", "📦 Pods", "Kubernetes · Workloads", "Pod status, placement and restarts.", ("get", "pods"), _KUBE_SCOPE),
    ("deployments", "🚀 Deployments", "Kubernetes · Workloads", "Desired and available replicas.", ("get", "deployments"), _KUBE_SCOPE),
    ("statefulsets", "💾 StatefulSets", "Kubernetes · Workloads", "Stateful workload replicas.", ("get", "statefulsets"), _KUBE_SCOPE),
    ("daemonsets", "🌐 DaemonSets", "Kubernetes · Workloads", "Per-node workload coverage.", ("get", "daemonsets"), _KUBE_SCOPE),
    ("jobs", "🏁 Jobs & CronJobs", "Kubernetes · Workloads", "Batch completion and scheduled workloads.", ("get", "jobs,cronjobs"), _KUBE_SCOPE),
    ("replicasets", "🧬 ReplicaSets", "Kubernetes · Workloads", "Replica counts and deployment history.", ("get", "replicasets"), _KUBE_SCOPE),
    ("events", "📣 Events", "Kubernetes · Workloads", "Events ordered by creation time.", ("get", "events", "--sort-by=.metadata.creationTimestamp"), _KUBE_SCOPE),
    ("describe", "🔎 Describe resource", "Kubernetes · Workloads", "Resource details and related events; enter kind/name.", ("describe",), ("resource", "namespace")),
    ("logs", "📜 Pod logs", "Kubernetes · Workloads", "Bounded logs from a pod or its previous container.", ("logs", "--timestamps=true"), ("pod", "namespace", "container", "tail", "previous")),
    ("rollout_status", "🚦 Rollout status", "Kubernetes · Workloads", "Check a Deployment, StatefulSet or DaemonSet rollout (60s maximum).", ("rollout", "status", "--timeout=60s"), ("resource", "namespace")),
    ("rollout_history", "🕰️ Rollout history", "Kubernetes · Workloads", "Inspect rollout revisions without changing them.", ("rollout", "history"), ("resource", "namespace")),
    ("services", "🔌 Services", "Kubernetes · Network", "Service types, ports and addresses.", ("get", "services"), _KUBE_SCOPE),
    ("endpoints", "🎯 EndpointSlices", "Kubernetes · Network", "Service backends and endpoint readiness.", ("get", "endpointslices"), _KUBE_SCOPE),
    ("ingresses", "🌍 Ingresses", "Kubernetes · Network", "Ingress hosts, classes and addresses.", ("get", "ingresses"), _KUBE_SCOPE),
    ("networkpolicies", "🛡️ Network policies", "Kubernetes · Network", "Namespaced network policy inventory.", ("get", "networkpolicies"), _KUBE_SCOPE),
    ("pvc", "🗄️ Volume claims", "Kubernetes · Storage", "PVC binding, requested capacity and storage classes.", ("get", "persistentvolumeclaims"), _KUBE_SCOPE),
    ("pv", "💽 Persistent volumes", "Kubernetes · Storage", "Cluster volume capacity, claims and reclaim policy.", ("get", "persistentvolumes"), ("selector", "output")),
    ("storageclasses", "🏷️ Storage classes", "Kubernetes · Storage", "Provisioners, reclaim policy and expansion support.", ("get", "storageclasses"), ("output",)),
    ("configmaps", "⚙️ ConfigMaps", "Kubernetes · Configuration", "ConfigMap inventory; YAML/JSON output includes their contents.", ("get", "configmaps"), _KUBE_SCOPE),
    ("quotas", "📏 Quotas & limits", "Kubernetes · Configuration", "Resource quotas and default resource limits.", ("get", "resourcequotas,limitranges"), _KUBE_SCOPE),
    ("top_nodes", "📊 Node usage", "Kubernetes · Metrics", "Node CPU and memory; requires metrics-server.", ("top", "nodes"), ("selector",)),
    ("top_pods", "📈 Pod usage", "Kubernetes · Metrics", "Pod CPU and memory; requires metrics-server.", ("top", "pods"), ("namespace", "all_namespaces", "selector")),
]

for _id, _label, _category, _description, _arguments, _options in _KUBE_TASKS:
    TERMINAL_CHECKS["kube_" + _id] = {
        "label": _label, "category": _category, "description": _description,
        "command": shlex.join(["kubectl", "--request-timeout=20s", *_arguments]),
        "arguments": _arguments,
        "parameters": [{"id": key, **KUBERNETES_PARAMETERS[key]} for key in ("runner", "context", *_options)],
    }


def terminal_command(check_id: str, parameters: Any = None) -> str:
    """Build a named command; parameter values are shell-quoted, never shell fragments."""
    check = TERMINAL_CHECKS[check_id]
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")
    specs = {spec["id"]: spec for spec in check.get("parameters", [])}
    if parameters.keys() - specs.keys():
        raise ValueError("Unsupported task parameters: " + ", ".join(sorted(parameters.keys() - specs.keys())))
    if not specs:
        return check["command"]

    values = {}
    for key, spec in specs.items():
        value = parameters.get(key, spec.get("default", False if spec.get("type") == "checkbox" else ""))
        if spec.get("type") == "checkbox":
            if not isinstance(value, bool):
                raise ValueError(spec["label"] + " must be true or false")
        else:
            if not isinstance(value, str):
                raise ValueError(spec["label"] + " must be text")
            value = value.strip()
            if len(value) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("Invalid " + spec["label"])
            if spec.get("required") and not value:
                raise ValueError(spec["label"] + " is required")
            if "choices" in spec and value not in spec["choices"]:
                raise ValueError("Invalid " + spec["label"])
            if key in ("resource", "pod") and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*(/[a-zA-Z0-9][a-zA-Z0-9._-]*)?", value):
                raise ValueError("Use a resource name, not flags or a command")
            if key == "pod" and "/" in value:
                raise ValueError("Pod name must not include a resource kind")
            if spec.get("type") == "number" and (not value.isascii() or not value.isdigit() or not spec["min"] <= int(value) <= spec["max"]):
                raise ValueError(f'{spec["label"]} must be between {spec["min"]} and {spec["max"]}')
        values[key] = value

    args = shlex.split(values["runner"]) + ["--request-timeout=20s", *check["arguments"]]
    for key, spec in specs.items():
        value = values[key]
        if key == "runner" or not value or (key == "namespace" and values.get("all_namespaces")):
            continue
        if spec.get("flag"):
            args.append(spec["flag"] if spec.get("type") == "checkbox" else spec["flag"] + "=" + value)
        else:
            args.append(value)
    return shlex.join(args)


def _run_test_terminal_job(job_id: str, cmd: list[str]) -> None:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        logger.error("Could not start terminal job %s: %s", job_id, exc)
        with TEST_TERMINAL_LOCK:
            TEST_TERMINAL_JOBS[job_id]["status"] = "failed"
            TEST_TERMINAL_JOBS[job_id]["stderr"] = str(exc)
            TEST_TERMINAL_JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        return

    timed_out = threading.Event()

    def expire() -> None:
        if proc.poll() is None:
            timed_out.set()
            proc.kill()

    timer = threading.Timer(TERMINAL_TIMEOUT_SECONDS, expire)
    timer.daemon = True
    timer.start()
    output = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    stream_error = ""
    try:
        if proc.stdout is not None:
            while chunk := proc.stdout.read1(4096):
                output = (output + decoder.decode(chunk))[-TERMINAL_OUTPUT_LIMIT:]
                with TEST_TERMINAL_LOCK:
                    TEST_TERMINAL_JOBS[job_id]["stdout"] = output
            output = (output + decoder.decode(b"", final=True))[-TERMINAL_OUTPUT_LIMIT:]
            with TEST_TERMINAL_LOCK:
                TEST_TERMINAL_JOBS[job_id]["stdout"] = output
        proc.wait()
    except OSError as exc:
        stream_error = f"Could not read terminal output: {exc}"
        logger.error("Terminal job %s failed: %s", job_id, exc)
        proc.kill()
        proc.wait()
    finally:
        timer.cancel()
        timer.join()
        if proc.stdout is not None:
            proc.stdout.close()
    with TEST_TERMINAL_LOCK:
        TEST_TERMINAL_JOBS[job_id]["exit_code"] = proc.returncode
        TEST_TERMINAL_JOBS[job_id]["status"] = "completed" if proc.returncode == 0 and not timed_out.is_set() and not stream_error else "failed"
        if stream_error:
            TEST_TERMINAL_JOBS[job_id]["stderr"] = stream_error
        if timed_out.is_set():
            TEST_TERMINAL_JOBS[job_id]["stderr"] = f"Terminal task exceeded {TERMINAL_TIMEOUT_SECONDS} seconds; the local SSH process was stopped."
        TEST_TERMINAL_JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    if timed_out.is_set():
        logger.warning("Terminal job %s exceeded its timeout", job_id)


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
            "node_username": str(event.get("node_username", "") or "").strip(),
            "certificate": str(event.get("certificate", "") or "").strip(),
            "port": str(event.get("port", "") or "").strip(),
        }
    )


def _test_terminal_targets(limit: int = 2000) -> list[Dict[str, Any]]:
    events = active_nodes(_read_events(limit=limit))
    nodes = _node_ops_inventory_view()
    targets: list[Dict[str, Any]] = []
    seen: set[str] = set()
    alias_map = _terminal_alias_connect_host_map(nodes)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_name = str(node.get("name", "") or "").strip()
        profiles = gather_profiles(
            node_name,
            str(node.get("address", "") or "").strip(),
            str(node.get("callback_connect_ip", "") or node.get("callback_source_ip", "") or "").strip(),
            user=str(node.get("node_username", "") or "").strip(),
            key=str(node.get("certificate", "") or "").strip(),
        ) if node_name else []
        node_address = _terminal_preferred_host_for_node(node)
        fallback_profile = next((profile for profile in profiles if profile.tailnet and profile.usable), None)
        if fallback_profile is None:
            fallback_profile = next((profile for profile in profiles if profile.tailnet), None)
        if not node_address and fallback_profile:
            node_address = fallback_profile.address
        callback_ip = str(node.get("callback_source_ip", "") or "").strip()
        if not node_name or not node_address:
            continue
        normalized_source = str(node.get("address_source", "") or "inventory").strip() or "inventory"
        if callback_ip and node_address == callback_ip and _is_usable_callback_ip(callback_ip):
            source = "source_ip"
        elif normalized_source == "callback":
            source = "source_ip"
        elif fallback_profile and node_address == fallback_profile.address:
            source = fallback_profile.source
        else:
            source = normalized_source
        event_like = {
            "hostname": node_name,
            "fqdn": "",
            "profile": str(node.get("callback_profile", "") or "").strip(),
            "instance_id": str(node.get("callback_instance_id", "") or "").strip(),
            "received_at": str(node.get("last_callback", "") or "").strip(),
            "node_username": str(node.get("node_username", "") or "").strip(),
            "certificate": str(node.get("certificate", "") or "").strip(),
            "port": str(node.get("ssh_port", "") or "").strip(),
        }
        _append_terminal_target(targets, seen, host=node_address, source=source, event=event_like, connect_host=node_address)

        for profile in profiles:
            if not profile.tailnet:
                continue
            _append_terminal_target(
                targets,
                seen,
                host=profile.address,
                source=profile.source,
                event=event_like,
                connect_host=profile.address,
            )

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
