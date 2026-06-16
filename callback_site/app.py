from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from urllib import error as urlerror
from urllib import request as urlrequest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "callbacks.jsonl"
KEYS_DIR = DATA_DIR / "ssh_keys"
NODE_CONFIG_FILE = DATA_DIR / "node_monitor_config.json"
INVENTORY_FILE = BASE_DIR.parent / "inventory.yml"
SCRIPT_PATH = BASE_DIR.parent / "scripts" / "prepare_sd_cloud_init.sh"
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
TAILSCALE_POLICY_DEFAULTS: Dict[str, Any] = {
    "tailnet": "tail00c2ce.ts.net",
    "admin_group": "group:admins",
    "rpi5_tag": "tag:rpi5",
    "rpi_bplus_tag": "tag:rpi-bplus",
    "homelab_tag": "tag:homelab",
    "pi5_subnets": ["192.168.174.0/24", "192.168.175.0/24"],
}

app = Flask(__name__)


def _ensure_data_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.touch()


def _ensure_keys_dir() -> None:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(KEYS_DIR, 0o700)
    except PermissionError:
        # Best effort; keep running on filesystems that ignore chmod semantics.
        pass


def _append_event(event: Dict[str, Any]) -> None:
    _ensure_data_file()
    with DATA_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def _read_events(limit: int = 200) -> List[Dict[str, Any]]:
    _ensure_data_file()
    lines = DATA_FILE.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    events: List[Dict[str, Any]] = []
    for line in selected:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    events.reverse()
    return events


def _ensure_prereqs() -> List[str]:
    errors: List[str] = []
    if not SCRIPT_PATH.exists():
        errors.append(f"Missing script: {SCRIPT_PATH}")
    if not INVENTORY_FILE.exists():
        errors.append(f"Missing inventory file: {INVENTORY_FILE}")
    return errors


def _load_node_config() -> Dict[str, Any]:
    _ensure_data_file()
    default = {
        "username": "",
        "password": "",
        "webhook_url": "",
        "verify_tls": True,
    }
    if not NODE_CONFIG_FILE.exists():
        return default
    try:
        loaded = json.loads(NODE_CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default
    default.update({k: loaded.get(k, default[k]) for k in default})
    return default


def _save_node_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    config = _load_node_config()
    for key in ("username", "password", "webhook_url", "verify_tls"):
        if key in payload:
            config[key] = payload[key]
    _ensure_data_file()
    NODE_CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def _inventory_nodes() -> List[Dict[str, Any]]:
    try:
        result = subprocess.run(
            ["ansible-inventory", "-i", str(INVENTORY_FILE), "--list"],
            check=True,
            capture_output=True,
            text=True,
        )
        inv = json.loads(result.stdout)
    except Exception:
        return _inventory_nodes_from_yaml_fallback()

    hostvars = inv.get("_meta", {}).get("hostvars", {})
    groups = ["rpi5_active", "rpi_bplus_active"]
    nodes: List[Dict[str, Any]] = []
    for group in groups:
        for host in inv.get(group, {}).get("hosts", []):
            hv = hostvars.get(host, {})
            nodes.append(
                {
                    "name": host,
                    "group": group,
                    "address": hv.get("ansible_host", host),
                }
            )
    return nodes


def _inventory_nodes_from_yaml_fallback() -> List[Dict[str, Any]]:
    if not INVENTORY_FILE.exists():
        return []

    text = INVENTORY_FILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    target_groups = {"rpi5_active", "rpi_bplus_active"}

    nodes: List[Dict[str, Any]] = []
    current_group = ""
    group_indent = -1
    in_hosts = False
    hosts_indent = -1
    current_host: Dict[str, Any] | None = None
    host_indent = -1

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].rstrip("\n")
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        match = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()

        if key in target_groups and value == "":
            current_group = key
            group_indent = indent
            in_hosts = False
            current_host = None
            continue

        if current_group and indent <= group_indent:
            current_group = ""
            in_hosts = False
            current_host = None

        if not current_group:
            continue

        if key == "hosts" and value == "" and indent > group_indent:
            in_hosts = True
            hosts_indent = indent
            current_host = None
            continue

        if in_hosts and indent <= hosts_indent:
            in_hosts = False
            current_host = None

        if not in_hosts:
            continue

        if value == "" and indent > hosts_indent:
            current_host = {
                "name": key,
                "group": current_group,
                "address": key,
            }
            host_indent = indent
            nodes.append(current_host)
            continue

        if current_host and indent > host_indent and key == "ansible_host":
            current_host["address"] = value.strip('"\'')

    return nodes


def _ping_status(address: str) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "1", address],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return {"reachable": False, "latency_ms": None, "error": "ping command not found"}

    reachable = proc.returncode == 0
    latency = None
    if reachable:
        match = re.search(r"time=([0-9.]+)\s*ms", proc.stdout or "")
        if match:
            latency = float(match.group(1))
    return {"reachable": reachable, "latency_ms": latency, "error": "" if reachable else (proc.stderr or proc.stdout).strip()}


def _last_callback_by_host() -> Dict[str, str]:
    seen: Dict[str, str] = {}
    for event in _read_events(limit=1000):
        host = event.get("hostname")
        ts = event.get("received_at")
        if host and ts and host not in seen:
            seen[host] = ts
    return seen


def _send_webhook(url: str, payload: Dict[str, Any], verify_tls: bool = True) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    context = None
    if not verify_tls and url.startswith("https://"):
        import ssl

        context = ssl._create_unverified_context()
    try:
        with urlrequest.urlopen(req, timeout=10, context=context) as resp:  # nosec B310
            return {"ok": True, "status": resp.status}
    except (urlerror.URLError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc)}


def _is_safe_key_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name))


def _generate_or_load_ssh_key(key_name: str, comment: str = "rack-bootstrap") -> Dict[str, Any]:
    _ensure_keys_dir()
    private_key = KEYS_DIR / key_name
    public_key = KEYS_DIR / f"{key_name}.pub"

    created = False
    if not private_key.exists() or not public_key.exists():
        subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-a",
                "64",
                "-N",
                "",
                "-C",
                comment,
                "-f",
                str(private_key),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        created = True

    pub = public_key.read_text(encoding="utf-8").strip()
    return {
        "created": created,
        "key_name": key_name,
        "public_key": pub,
        "private_key_path": str(private_key),
        "public_key_path": str(public_key),
    }


def _list_sd_devices() -> List[Dict[str, Any]]:
    try:
        result = subprocess.run(
            [
                "lsblk",
                "-J",
                "-o",
                "NAME,PATH,SIZE,MODEL,RM,TYPE,TRAN,RO,HOTPLUG,MOUNTPOINTS",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
    except Exception:
        return []

    devices: List[Dict[str, Any]] = []
    for d in payload.get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        if int(d.get("ro") or 0) != 0:
            continue
        rm = int(d.get("rm") or 0)
        hotplug = int(d.get("hotplug") or 0)
        tran = (d.get("tran") or "").lower()
        if rm != 1 and hotplug != 1 and tran not in {"usb", "mmc"}:
            continue
        devices.append(
            {
                "name": d.get("name"),
                "path": d.get("path"),
                "size": d.get("size"),
                "model": d.get("model") or "",
                "transport": d.get("tran") or "",
                "mountpoints": d.get("mountpoints") or [],
            }
        )
    return devices


def _run_prepare_job(job_id: str, form: Dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="sdprep-key-") as tdir:
        pubkey_file = Path(tdir) / "id.pub"
        pubkey_file.write_text(form["ssh_public_key"].strip() + "\n", encoding="utf-8")

        cmd = [
            str(SCRIPT_PATH),
            "--profile",
            form["profile"],
            "--hostname",
            form["hostname"],
            "--device",
            form["device"],
            "--ssh-key-file",
            str(pubkey_file),
            "--callback-url",
            form["callback_url"],
            "--yes",
        ]

        image_url = form.get("image_url", "").strip()
        image_file = form.get("image_file", "").strip()
        if image_url:
            cmd.extend(["--image-url", image_url])
        if image_file:
            cmd.extend(["--image-file", image_file])

        run_cmd: List[str]
        if os.geteuid() == 0:
            run_cmd = cmd
        else:
            run_cmd = ["sudo", "-n"] + cmd

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["command"] = run_cmd

        proc = subprocess.run(run_cmd, capture_output=True, text=True)

        with JOBS_LOCK:
            JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["stdout"] = proc.stdout[-50000:]
            JOBS[job_id]["stderr"] = proc.stderr[-50000:]
            JOBS[job_id]["exit_code"] = proc.returncode
            JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"


@app.get("/")
def index() -> str:
    events = _read_events(limit=200)
    return render_template("index.html", events=events, devices=_list_sd_devices())


@app.get("/tailscale-acl")
def tailscale_acl() -> str:
    return render_template("tailscale_acl.html", defaults=TAILSCALE_POLICY_DEFAULTS)


@app.get("/nodes")
def nodes_page() -> str:
    return render_template("nodes.html")


@app.get("/health")
def health() -> Any:
    return jsonify({"ok": True})


@app.get("/api/cloud-init/events")
def events() -> Any:
    return jsonify({"events": _read_events(limit=500)})


@app.post("/api/cloud-init/callback")
def callback() -> Any:
    payload = request.get_json(silent=True) or {}
    event = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
        "user_agent": request.headers.get("User-Agent", ""),
        "instance_id": payload.get("instance_id") or request.form.get("instance_id"),
        "hostname": payload.get("hostname") or request.form.get("hostname"),
        "fqdn": payload.get("fqdn") or request.form.get("fqdn"),
        "profile": payload.get("profile"),
        "stage": payload.get("stage"),
        "raw": payload,
    }
    _append_event(event)
    return jsonify({"ok": True})


@app.get("/api/sd/devices")
def sd_devices() -> Any:
    prereq_errors = _ensure_prereqs()
    return jsonify({"devices": _list_sd_devices(), "errors": prereq_errors})


@app.post("/api/sd/prepare")
def sd_prepare() -> Any:
    prereq_errors = _ensure_prereqs()
    if prereq_errors:
        return jsonify({"ok": False, "errors": prereq_errors}), 400

    payload = request.get_json(silent=True) or {}
    required = ["profile", "hostname", "device", "ssh_public_key", "callback_url"]
    missing = [k for k in required if not str(payload.get(k, "")).strip()]
    if missing:
        return jsonify({"ok": False, "errors": [f"Missing fields: {', '.join(missing)}"]}), 400

    if payload["profile"] not in {"pi5", "pibplus"}:
        return jsonify({"ok": False, "errors": ["profile must be pi5 or pibplus"]}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "request": {
                "profile": payload["profile"],
                "hostname": payload["hostname"],
                "device": payload["device"],
                "callback_url": payload["callback_url"],
            },
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }

    thread = threading.Thread(target=_run_prepare_job, args=(job_id, payload), daemon=True)
    thread.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.post("/api/ssh/generate")
def ssh_generate() -> Any:
    payload = request.get_json(silent=True) or {}
    key_name = str(payload.get("key_name", "rack-bootstrap-ed25519")).strip()
    comment = str(payload.get("comment", "rack-bootstrap")).strip()

    if not key_name:
        return jsonify({"ok": False, "errors": ["key_name is required"]}), 400
    if not _is_safe_key_name(key_name):
        return jsonify({"ok": False, "errors": ["key_name may contain only letters, numbers, dot, dash, and underscore"]}), 400

    try:
        result = _generate_or_load_ssh_key(key_name, comment=comment)
    except FileNotFoundError:
        return jsonify({"ok": False, "errors": ["ssh-keygen command not found on host"]}), 500
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return jsonify({"ok": False, "errors": [f"ssh-keygen failed: {stderr}"]}), 500

    return jsonify({"ok": True, **result})


@app.get("/api/nodes/summary")
def nodes_summary() -> Any:
    config = _load_node_config()
    nodes = _inventory_nodes()
    masked = {
        "username": config.get("username", ""),
        "password_set": bool(config.get("password")),
        "webhook_url": config.get("webhook_url", ""),
        "verify_tls": bool(config.get("verify_tls", True)),
    }
    return jsonify({"ok": True, "nodes": nodes, "config": masked})


@app.post("/api/nodes/config")
def nodes_config() -> Any:
    payload = request.get_json(silent=True) or {}
    config = _save_node_config(
        {
            "username": str(payload.get("username", "")).strip(),
            "password": str(payload.get("password", "")),
            "webhook_url": str(payload.get("webhook_url", "")).strip(),
            "verify_tls": bool(payload.get("verify_tls", True)),
        }
    )
    return jsonify(
        {
            "ok": True,
            "config": {
                "username": config.get("username", ""),
                "password_set": bool(config.get("password")),
                "webhook_url": config.get("webhook_url", ""),
                "verify_tls": bool(config.get("verify_tls", True)),
            },
        }
    )


@app.post("/api/nodes/monitor")
def nodes_monitor() -> Any:
    payload = request.get_json(silent=True) or {}
    emit_webhook = bool(payload.get("send_webhook", False))

    nodes = _inventory_nodes()
    by_host = _last_callback_by_host()
    statuses: List[Dict[str, Any]] = []
    for node in nodes:
        status = _ping_status(node["address"])
        statuses.append(
            {
                "name": node["name"],
                "group": node["group"],
                "address": node["address"],
                "reachable": status["reachable"],
                "latency_ms": status["latency_ms"],
                "last_callback": by_host.get(node["name"], ""),
                "error": status["error"],
            }
        )

    webhook_result: Dict[str, Any] = {"sent": False}
    config = _load_node_config()
    webhook_url = config.get("webhook_url", "")
    if emit_webhook and webhook_url:
        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "reachable": sum(1 for s in statuses if s["reachable"]),
            "unreachable": sum(1 for s in statuses if not s["reachable"]),
            "nodes": statuses,
        }
        result = _send_webhook(webhook_url, summary, verify_tls=bool(config.get("verify_tls", True)))
        webhook_result = {"sent": True, **result}

    return jsonify({"ok": True, "statuses": statuses, "webhook": webhook_result})


@app.get("/api/sd/jobs/<job_id>")
def sd_job(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    return jsonify({"ok": True, "job": job})


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8088"))
    app.run(host=host, port=port, debug=False)
