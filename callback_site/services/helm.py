"""GitHub chart imports and durable, re-openable Helm configurations."""

from __future__ import annotations

import base64
import hashlib
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
BUNDLED_CHARTS = {
    name: Path(__file__).resolve().parents[2] / "charts" / name
    for name in ("dockerbot", "headlamp")
}
BUNDLED = [
    {"id": "dockerbot", "name": "Dockerbot (Red Discord Bot)"},
    {"id": "headlamp", "name": "Headlamp (Kubernetes web UI)"},
]
LOCK = threading.Lock()
EXAMPLES = [
    {"name": "Hello World (NGINX)", "repository": "https://github.com/helm/examples", "chart_path": "charts/hello-world"},
    {"name": "Grafana Alloy (collector)", "repository": "https://github.com/grafana/alloy", "chart_path": "operations/helm/charts/alloy"},
    {"name": "n8n (community chart)", "repository": "https://github.com/8gears/n8n-helm-chart", "chart_path": "charts/n8n"},
]


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


def _guide(chart: Path) -> str:
    for name in ("PLAYBOOK_GUIDE.md", "README.md"):
        path = chart / name
        if path.is_file():
            if path.stat().st_size > 65536:
                raise ValueError(f"{name} exceeds the 64 KiB guide limit.")
            return path.read_text(encoding="utf-8")
    return ""


def _chart_digest(chart: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(chart.rglob("*")):
        if item.is_symlink():
            raise ValueError("Chart files cannot use symbolic links.")
        if item.is_file():
            name = item.relative_to(chart).as_posix().encode()
            content = item.read_bytes()
            digest.update(len(name).to_bytes(8, "big") + name)
            digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def _snapshot_path(record_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", record_id):
        raise ValueError("Invalid bundled chart identifier.")
    return CONFIG_FILE.parent / "helm-chart-snapshots" / record_id


def _tailscale(raw: Any) -> dict[str, str]:
    if raw is None:
        return {"mode": "off"}
    if not isinstance(raw, dict):
        raise ValueError("Tailscale configuration must be an object.")
    mode = raw.get("mode", "off")
    if mode == "off":
        return {"mode": "off"}
    if not isinstance(mode, str) or mode not in {"https", "service"}:
        raise ValueError("Choose HTTPS ingress, service exposure, or off.")
    result = {"mode": mode}
    for key in ("service", "hostname", "proxy_class", "proxy_group"):
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"Tailscale {key} must be text.")
        value = value.strip()
        if value and (len(value) > 63 or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value)):
            raise ValueError(f"Tailscale {key} must be a lowercase DNS label (maximum 63 characters).")
        result[key] = value
    if not result["service"] or not result["hostname"]:
        raise ValueError("Tailscale exposure requires a Service name and hostname.")
    port = str(raw.get("port", "")).strip()
    if mode == "https" and (not port.isdigit() or not 1 <= int(port) <= 65535):
        raise ValueError("HTTPS exposure requires a Service port from 1 to 65535.")
    result["port"] = port if mode == "https" else ""
    tags = raw.get("tags", "")
    if not isinstance(tags, str):
        raise ValueError("Tailscale tags must be text.")
    tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
    if any(not re.fullmatch(r"tag:[a-zA-Z][a-zA-Z0-9-]*", tag) for tag in tag_list):
        raise ValueError("Tailscale tags must be comma-separated tag:name values.")
    if result["proxy_group"] and (tag_list or result["proxy_class"]):
        raise ValueError("Configure tags and proxy class on the ProxyGroup, not the exposed resource.")
    result["tags"] = ",".join(tag_list)
    return result


def _prepare_dependencies(chart: Path) -> None:
    metadata = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
    if isinstance(metadata, dict) and metadata.get("dependencies"):
        dependencies = metadata["dependencies"]
        if all(
            isinstance(dep, dict) and isinstance(dep.get("name"), str)
            and ((chart / "charts" / dep["name"] / "Chart.yaml").is_file()
                 or (chart / "charts" / f"{dep['name']}-{dep.get('version')}.tgz").is_file())
            for dep in dependencies
        ):
            return
        if not (chart / "Chart.lock").is_file():
            raise ValueError("Chart dependencies require a committed Chart.lock to pin dependency versions.")
        lock = yaml.safe_load((chart / "Chart.lock").read_text(encoding="utf-8"))
        if not isinstance(lock, dict) or not isinstance(lock.get("dependencies"), list):
            raise ValueError("Chart.lock has an invalid dependency list.")
        with tempfile.TemporaryDirectory(prefix="helm-repositories-") as directory:
            flags = ["--repository-config", str(Path(directory) / "repositories.yaml"),
                     "--repository-cache", str(Path(directory) / "cache")]
            repositories = set()
            for dependency in lock["dependencies"]:
                if not isinstance(dependency, dict) or not isinstance(dependency.get("repository"), str):
                    raise ValueError("Chart.lock has an invalid dependency repository.")
                repository = dependency["repository"]
                if repository.startswith("oci://") or not repository:
                    continue
                url = urlparse(repository)
                if url.scheme != "https" or not url.netloc or url.username or url.password:
                    raise ValueError("Chart dependency repositories must use HTTPS or OCI.")
                if repository not in repositories:
                    _command(["helm", "repo", "add", f"dependency-{len(repositories)}", repository, *flags], timeout=120)
                    repositories.add(repository)
            _command(["helm", "dependency", "build", str(chart), *flags], timeout=120)


def _find(records: list[dict[str, Any]], record_id: str) -> dict[str, Any]:
    record = next((item for item in records if item.get("id") == record_id), None)
    if record is None:
        raise KeyError("Helm configuration not found.")
    return record


def configurations() -> list[dict[str, Any]]:
    with LOCK:
        return _load()


def import_chart(payload: dict[str, Any]) -> dict[str, Any]:
    bundled = payload.get("bundled")
    if bundled is not None and not isinstance(bundled, str):
        raise ValueError("Bundled chart must be a name.")
    if bundled and bundled not in BUNDLED_CHARTS:
        raise ValueError("Unknown bundled chart.")
    repository = "" if bundled else _repository(payload.get("repository"))
    record_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="helm-import-") as directory:
        root = Path(directory) / "repository"
        if bundled:
            chart = _chart(BUNDLED_CHARTS[bundled].parent, bundled)
            relative = f"charts/{bundled}"
            commit = _chart_digest(chart)
        else:
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
        guide = _guide(chart)
        if bundled:
            snapshot = _snapshot_path(record_id)
            snapshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                shutil.copytree(chart, snapshot)
            except OSError:
                if snapshot.exists():
                    shutil.rmtree(snapshot)
                raise
            if _chart_digest(snapshot) != commit:
                shutil.rmtree(snapshot)
                raise RuntimeError("Bundled chart changed during import; try again.")
    release = re.sub(r"[^a-z0-9-]", "-", str(metadata.get("name") or "chart").lower()).strip("-")[:35] or "chart"
    record = {
        "id": record_id, "name": str(metadata.get("name") or "chart"),
        "repository": repository, "commit": commit, "chart_path": relative,
        "chart_name": metadata.get("name"), "chart_version": metadata.get("version"),
        "release": f"{release}-{record_id[:6]}", "namespace": "playbook-helm",
        "defaults": defaults, "values": defaults, "revision": 1,
        "tailscale": {"mode": "off"},
        "guide": guide, "bundled": bundled or "",
        "saved_at": _now(), "last_submission": None,
    }
    try:
        with LOCK:
            records = _load()
            records.append(record)
            _write(records)
    except (OSError, RuntimeError):
        if bundled:
            shutil.rmtree(snapshot)
        raise
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
        tailscale = _tailscale(payload.get("tailscale", record.get("tailscale")))
        if payload.get("revision") != record["revision"]:
            raise ValueError("This configuration changed. Reopen it before saving.")
        if any(item["id"] != record_id and item["release"] == release and item["namespace"] == namespace for item in records):
            raise ValueError("Another configuration already uses that release and namespace.")
        record.update(name=name, release=release, namespace=namespace, values=values, tailscale=tailscale, revision=record["revision"] + 1, saved_at=_now())
        _write(records)
        return record


def _helm_connection() -> list[str]:
    if os.getenv("KUBECONFIG") or (Path.home() / ".kube" / "config").is_file():
        return []
    path = Path("/etc/rancher/k3s/k3s.yaml")
    if path.is_file():
        return ["--kubeconfig", str(path)]
    raise RuntimeError("No Kubernetes credentials configured. Connect a cluster before deploying.")


def set_dockerbot_token(record_id: str, payload: dict[str, Any]) -> None:
    token = payload.get("token")
    if not isinstance(token, str) or not 1 <= len(token) <= 512 or not re.fullmatch(r"[!-~]+", token):
        raise ValueError("Enter a non-empty bot token without whitespace.")
    if payload.get("confirm") is not True:
        raise ValueError("Writing a Kubernetes Secret requires explicit confirmation.")
    with LOCK:
        record = _find(_load(), record_id)
        if payload.get("revision") != record["revision"]:
            raise ValueError("This configuration changed. Reopen it before continuing.")
        is_dockerbot = record.get("bundled") == "dockerbot" or (
            str(record.get("repository") or "").lower() == "https://github.com/mevsthevoices/playbook"
            and record.get("chart_path") == "charts/dockerbot"
        )
        if not is_dockerbot:
            raise ValueError("Token setup is only available for the Playbook Dockerbot chart.")
        values = yaml.safe_load(_values(record["values"])) or {}
        bot = values.get("bot")
        if not isinstance(bot, dict):
            raise ValueError("Set bot.existingSecret and bot.tokenKey in the saved values.")
        name, key = bot.get("existingSecret"), bot.get("tokenKey")
        if not isinstance(name, str) or len(name) > 253 or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name) or ".." in name:
            raise ValueError("bot.existingSecret must be a valid Kubernetes Secret name.")
        if not isinstance(key, str) or len(key) > 253 or not re.fullmatch(r"[-._a-zA-Z0-9]+", key):
            raise ValueError("bot.tokenKey must be a valid Kubernetes Secret key.")
        manifest = {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": name, "namespace": record["namespace"]},
            "type": "Opaque", "data": {key: base64.b64encode(token.encode()).decode()},
        }
        manager = "playbook-dockerbot-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        kubectl = ["kubectl"] if shutil.which("kubectl") else ["k3s", "kubectl"]
        args = [*kubectl, *_helm_connection(), "apply", "--server-side", f"--field-manager={manager}", "-f", "-", "-o", "name"]
        try:
            result = subprocess.run(args, input=json.dumps(manifest), capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Could not submit the Secret to Kubernetes. Check the cluster connection.") from exc
        if result.returncode:
            raise RuntimeError("Kubernetes rejected the Secret. Check namespace, permissions, and field ownership; no token was saved by Playbook.")


def run_configuration(record_id: str, revision: Any, *, deploy: bool = False) -> dict[str, Any]:
    with LOCK:
        records = _load()
        record = _find(records, record_id)
        if revision != record["revision"]:
            raise ValueError("This configuration changed. Reopen it before continuing.")
        with tempfile.TemporaryDirectory(prefix="helm-run-") as directory:
            root = Path(directory) / "repository"
            if record.get("bundled"):
                snapshot = _snapshot_path(record_id)
                chart = _chart(snapshot.parent, record_id)
                if _chart_digest(chart) != record["commit"]:
                    raise RuntimeError("Bundled chart snapshot changed; import a new configuration.")
                shutil.copytree(chart, root)
                chart = root
            else:
                _checkout(record["repository"], root, record["commit"])
                chart = _chart(root, record["chart_path"])
            _prepare_dependencies(chart)
            values_file = Path(directory) / "values.yaml"
            values_file.write_text(record["values"], encoding="utf-8")
            args = [record["release"], str(chart), "--namespace", record["namespace"], "--values", str(values_file)]
            tailscale = _tailscale(record.get("tailscale"))
            if tailscale["mode"] != "off":
                renderer_config = Path(directory) / "tailscale.json"
                renderer_config.write_text(json.dumps({**tailscale, "namespace": record["namespace"], "release": record["release"]}), encoding="utf-8")
                args += ["--post-renderer", str(Path(__file__).with_name("helm_postrender.py")), "--post-renderer-args", str(renderer_config)]
            manifest = _command(["helm", "template", *args])
            if not deploy:
                return {"manifest": manifest}
            if tailscale["mode"] != "off":
                from services import tailscale_operator
                status = tailscale_operator.operator_status()
                if status.get("error"):
                    raise RuntimeError(f"Cannot check Tailscale operator: {status['error']}")
                if not status.get("installed") or not status.get("ready_replicas"):
                    raise RuntimeError("The Tailscale operator must be installed and ready before deploying exposure.")
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
                "chart_path": record["chart_path"], "values": record["values"], "tailscale": tailscale, "status": "pending",
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
