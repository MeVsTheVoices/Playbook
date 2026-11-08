"""GitHub chart imports and durable, re-openable Helm configurations."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from services.storage import APPDATA_DIR, _atomic_write_text

logger = logging.getLogger(__name__)
CONFIG_FILE = APPDATA_DIR / "helm_configurations.json"
LOCK = threading.Lock()
EXAMPLES = [{"name": "Hello World (NGINX)", "repository": "https://github.com/helm/examples", "chart_path": "charts/hello-world"}]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> list[dict[str, Any]]:
    if not CONFIG_FILE.exists():
        return []
    try:
        records = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Helm configuration storage is corrupt; restore it from a backup.") from exc
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise RuntimeError("Helm configuration storage is invalid; restore it from a backup.")
    return records


def _write(records: list[dict[str, Any]]) -> None:
    _atomic_write_text(CONFIG_FILE, json.dumps(records, indent=2))


def _command(args: list[str], *, cwd: Path | None = None, timeout: int = 60) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    try:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{args[0]} could not complete: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {(result.stderr or result.stdout).strip()[:3000]}")
    return result.stdout


def _repository(raw: Any) -> str:
    url = urlparse(str(raw or "").strip())
    parts = url.path.rstrip("/").removesuffix(".git").split("/")
    if (
        url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment
        or len(parts) != 3 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in parts[1:])
    ):
        raise ValueError("Use a public GitHub repository URL: https://github.com/owner/repository.")
    return f"https://github.com/{parts[1]}/{parts[2]}"


def _checkout(repository: str, root: Path, commit: str = "") -> str:
    _command(["git", "init", "--quiet", str(root)])
    _command(["git", "remote", "add", "origin", repository], cwd=root)
    _command(["git", "fetch", "--quiet", "--depth=1", "origin", commit or "HEAD"], cwd=root)
    _command(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=root)
    return _command(["git", "rev-parse", "HEAD"], cwd=root).strip()


def _chart(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        raise ValueError("Chart path must be a relative directory inside the repository.")
    chart = root / path
    if not chart.is_dir() or not (chart / "Chart.yaml").is_file():
        raise ValueError(f"No Chart.yaml found at {relative}.")
    if any(part.is_symlink() for part in (chart, *chart.parents) if part != root.parent):
        raise ValueError("Chart directories cannot use symbolic links.")
    if any(item.is_symlink() for item in chart.rglob("*")):
        raise ValueError("Chart files cannot use symbolic links.")
    return chart


def _values(raw: Any) -> str:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 262144:
        raise ValueError("Values must be YAML text no larger than 256 KiB.")
    try:
        values = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid values YAML: {exc}") from exc
    if values is not None and not isinstance(values, dict):
        raise ValueError("Values YAML must be a mapping.")
    return raw


def _find(records: list[dict[str, Any]], record_id: str) -> dict[str, Any]:
    record = next((item for item in records if item.get("id") == record_id), None)
    if record is None:
        raise KeyError("Helm configuration not found.")
    return record


def configurations() -> list[dict[str, Any]]:
    with LOCK:
        return _load()


def import_chart(payload: dict[str, Any]) -> dict[str, Any]:
    repository = _repository(payload.get("repository"))
    with tempfile.TemporaryDirectory(prefix="helm-import-") as directory:
        root = Path(directory) / "repository"
        commit = _checkout(repository, root)
        paths = sorted(
            str(item.parent.relative_to(root)).replace("\\", "/")
            for item in root.rglob("Chart.yaml") if ".git" not in item.parts
        )
        relative = str(payload.get("chart_path") or "").strip()
        if not relative:
            if len(paths) != 1:
                raise ValueError(f"Select a chart path. Found: {', '.join(paths) or 'no Helm charts'}.")
            relative = paths[0]
        chart = _chart(root, relative)
        metadata = yaml.safe_load(_command(["helm", "show", "chart", str(chart)]))
        if not isinstance(metadata, dict) or metadata.get("type", "application") != "application":
            raise ValueError("Select an application chart, not a library chart.")
        defaults = _values(_command(["helm", "show", "values", str(chart)]))
    record_id = uuid.uuid4().hex[:12]
    release = re.sub(r"[^a-z0-9-]", "-", str(metadata.get("name") or "chart").lower()).strip("-")[:35] or "chart"
    record = {
        "id": record_id, "name": str(metadata.get("name") or "chart"),
        "repository": repository, "commit": commit, "chart_path": relative,
        "chart_name": metadata.get("name"), "chart_version": metadata.get("version"),
        "release": f"{release}-{record_id[:6]}", "namespace": "playbook-helm",
        "defaults": defaults, "values": defaults, "revision": 1,
        "saved_at": _now(), "last_submission": None,
    }
    with LOCK:
        records = _load()
        records.append(record)
        _write(records)
    return record


def save_configuration(record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    release = str(payload.get("release") or "").strip()
    namespace = str(payload.get("namespace") or "").strip()
    if not name or len(name) > 60:
        raise ValueError("Configuration name is required (maximum 60 characters).")
    for label, value, maximum in (("Release", release, 53), ("Namespace", namespace, 63)):
        if len(value) > maximum or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value):
            raise ValueError(f"{label} must be a lowercase DNS label, at most {maximum} characters.")
    values = _values(payload.get("values"))
    with LOCK:
        records = _load()
        record = _find(records, record_id)
        if payload.get("revision") != record["revision"]:
            raise ValueError("This configuration changed. Reopen it before saving.")
        if any(item["id"] != record_id and item["release"] == release and item["namespace"] == namespace for item in records):
            raise ValueError("Another configuration already uses that release and namespace.")
        record.update(name=name, release=release, namespace=namespace, values=values, revision=record["revision"] + 1, saved_at=_now())
        _write(records)
        return record


def _helm_connection() -> list[str]:
    if os.getenv("KUBECONFIG") or (Path.home() / ".kube" / "config").is_file():
        return []
    path = Path("/etc/rancher/k3s/k3s.yaml")
    if path.is_file():
        return ["--kubeconfig", str(path)]
    raise RuntimeError("No Kubernetes credentials configured. Connect a cluster before deploying.")


def run_configuration(record_id: str, revision: Any, *, deploy: bool = False) -> dict[str, Any]:
    with LOCK:
        records = _load()
        record = _find(records, record_id)
        if revision != record["revision"]:
            raise ValueError("This configuration changed. Reopen it before continuing.")
        with tempfile.TemporaryDirectory(prefix="helm-run-") as directory:
            root = Path(directory) / "repository"
            _checkout(record["repository"], root, record["commit"])
            chart = _chart(root, record["chart_path"])
            values_file = Path(directory) / "values.yaml"
            values_file.write_text(record["values"], encoding="utf-8")
            args = [record["release"], str(chart), "--namespace", record["namespace"], "--values", str(values_file)]
            manifest = _command(["helm", "template", *args])
            if not deploy:
                return {"manifest": manifest}
            connection = _helm_connection()
            # Capture the cluster and submitted inputs separately from later edits.
            kubectl = ["kubectl"] if shutil.which("kubectl") else ["k3s", "kubectl"]
            target = json.loads(_command([*kubectl, *connection, "config", "view", "--minify", "-o", "json"]))
            context = str(target.get("current-context") or "")
            clusters = target.get("clusters") or []
            if not context or not clusters:
                raise RuntimeError("The Kubernetes context is not configured.")
            submission = {
                "submitted_at": _now(), "revision": record["revision"],
                "context": context, "server": clusters[0]["cluster"]["server"],
                "release": record["release"], "namespace": record["namespace"],
                "repository": record["repository"], "commit": record["commit"],
                "chart_path": record["chart_path"], "values": record["values"], "status": "pending",
            }
            record["last_submission"] = submission
            _write(records)
            try:
                output = _command(
                    ["helm", "upgrade", "--install", *args, *connection,
                     "--create-namespace", "--wait", "--timeout", "120s", "--output", "json"],
                    timeout=150,
                )
                result = json.loads(output)
                submission.update(status="deployed", helm_revision=result.get("version"))
            except (RuntimeError, json.JSONDecodeError) as exc:
                submission.update(status="failed", error=str(exc))
                _write(records)
                raise RuntimeError(f"Helm submission failed; the attempt was saved: {exc}") from exc
            _write(records)
            return {"configuration": record}
