#!/usr/bin/env python3
"""Host-side SD/USB flashing agent for a containerised callback dashboard.

Runs as root where the removable devices are, and exposes a small
token-authenticated JSON API that the dashboard uses through
``services/sd_flasher_client.py``. It only lists removable disks and only runs
``scripts/prepare_sd_cloud_init.sh`` against a device it currently lists, so a
caller cannot run arbitrary commands or name a system disk.

Standard library only, so ``sudo python3 callback_site/sd_flasher.py`` works
without the dashboard's virtualenv. Start it with ``scripts/run_sd_flasher.sh``.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from services.sd_devices import _local_sd_devices  # noqa: E402

REPO_DIR = BASE_DIR.parent
PROFILES = {"pi5", "pi4", "pi3bplus", "network-toolkit", "playbook-server"}
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ENV_KEY_RE = re.compile(r"^CLOUD_INIT_[A-Z0-9_]{1,64}$")
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}$")
SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REQUIRED_COMMANDS = (
    "curl", "lsblk", "dd", "mount", "umount", "mountpoint", "udevadm", "parted", "partprobe",
    "mktemp", "sha256sum", "python3", "openssl", "blockdev", "cmp", "flock", "xz",
)
MAX_BODY_BYTES = 1024 * 1024
OUTPUT_LIMIT = 50000
MAX_JOBS = 20
BASE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")


class Config:
    def __init__(self, token: str, script: Path, image_cache_dir: Path, work_dir: Path) -> None:
        self.token = token
        self.script = script
        self.image_cache_dir = image_cache_dir
        self.work_dir = work_dir


JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def health_errors(config: Config) -> List[str]:
    errors: List[str] = []
    if os.geteuid() != 0:
        errors.append("SD flasher is not running as root; it cannot write SD/USB devices.")
    if not config.script.is_file():
        errors.append(f"SD flasher is missing its prepare script: {config.script}")
    missing = [cmd for cmd in REQUIRED_COMMANDS if shutil.which(cmd) is None]
    if missing:
        errors.append(f"SD flasher host is missing commands: {', '.join(missing)}")
    return errors


def _http_url(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 2048 and bool(re.match(r"^https?://[^\s/@]+", value))


def validate_job(spec: Any, devices: List[Dict[str, Any]], config: Config) -> Tuple[Dict[str, Any], List[str]]:
    if not isinstance(spec, dict):
        return {}, ["Job must be a JSON object."]
    errors: List[str] = []
    profile = spec.get("profile")
    if profile not in PROFILES:
        errors.append(f"profile must be one of: {', '.join(sorted(PROFILES))}")
    hostname = spec.get("hostname")
    if not isinstance(hostname, str) or not HOSTNAME_RE.match(hostname):
        errors.append("Invalid hostname.")
    device = spec.get("device")
    if device not in {d.get("path") for d in devices}:
        errors.append(f"Device {device!r} is not a removable SD/USB disk currently attached to this host.")
    if not _http_url(spec.get("callback_url")):
        errors.append("callback_url must be an http(s) URL.")
    key = spec.get("ssh_public_key")
    if not isinstance(key, str) or not key.strip() or len(key) > 16384 or "\0" in key:
        errors.append("ssh_public_key is required.")
    image_url = spec.get("image_url") or ""
    if image_url and not _http_url(image_url):
        errors.append("image_url must be an http(s) URL.")
    image_sha256 = spec.get("image_sha256") or ""
    if image_sha256 and not (isinstance(image_sha256, str) and SHA256_RE.match(image_sha256)):
        errors.append("image_sha256 must be 64 hex characters.")
    image_name = spec.get("image_name") or ""
    image_path: Optional[Path] = None
    if image_name:
        if not isinstance(image_name, str) or not IMAGE_NAME_RE.match(image_name) or ".." in image_name:
            errors.append("Invalid image_name.")
        else:
            image_path = config.image_cache_dir / image_name
            if not image_url and not image_path.is_file():
                errors.append(f"Image {image_name} is not in the flasher image cache ({config.image_cache_dir}).")
    env = spec.get("env") or {}
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not ENV_KEY_RE.match(k) or not isinstance(v, str) or "\0" in v
        for k, v in env.items()
    ):
        errors.append("env may only contain CLOUD_INIT_* string values.")
    if errors:
        return {}, errors
    return {
        "profile": profile, "hostname": hostname, "device": device,
        "callback_url": spec["callback_url"], "ssh_public_key": key.strip(),
        "image_url": image_url, "image_sha256": image_sha256, "image_path": image_path, "env": env,
    }, []


def build_command(job: Dict[str, Any], key_file: Path, config: Config) -> Tuple[List[str], Dict[str, str]]:
    cmd = [
        str(config.script),
        "--profile", job["profile"],
        "--hostname", job["hostname"],
        "--device", job["device"],
        "--ssh-key-file", str(key_file),
        "--callback-url", job["callback_url"],
        "--workspace", str(config.work_dir),
        "--yes",
    ]
    env = {k: os.environ[k] for k in BASE_ENV_KEYS if k in os.environ}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env["HOME"] = "/root"
    env["IMAGE_CACHE_DIR"] = str(config.image_cache_dir)
    env.update(job["env"])
    image_path: Optional[Path] = job["image_path"]
    if job["image_url"]:
        cmd.extend(["--image-url", job["image_url"]])
        if image_path is not None:
            env["IMAGE_CACHE_PATH"] = str(image_path)
    elif image_path is not None:
        cmd.extend(["--image-file", str(image_path)])
    if job["image_sha256"]:
        cmd.extend(["--image-sha256", job["image_sha256"]])
    return cmd, env


def _run_job(job_id: str, job: Dict[str, Any], config: Config) -> None:
    chunks: List[str] = []
    exit_code = 1
    try:
        config.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sdflash-key-") as tdir:
            key_file = Path(tdir) / "id.pub"
            key_file.write_text(job["ssh_public_key"] + "\n", encoding="utf-8")
            cmd, env = build_command(job, key_file, config)
            with subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                env=env, cwd=str(config.work_dir),
            ) as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    chunks.append(line)
                    with JOBS_LOCK:
                        JOBS[job_id]["output"] = "".join(chunks)[-OUTPUT_LIMIT:]
                exit_code = proc.wait()
    except Exception as exc:  # report every failure to the dashboard
        chunks.append(f"SD flasher error: {exc}\n")
    with JOBS_LOCK:
        JOBS[job_id].update(
            output="".join(chunks)[-OUTPUT_LIMIT:],
            exit_code=exit_code,
            status="completed" if exit_code == 0 else "failed",
            finished_at=_now(),
        )


def start_job(job: Dict[str, Any], config: Config) -> Tuple[Optional[str], List[str]]:
    with JOBS_LOCK:
        if any(j["status"] == "running" and j["device"] == job["device"] for j in JOBS.values()):
            return None, [f"{job['device']} is already being prepared."]
        finished = [k for k, j in JOBS.items() if j["status"] != "running"]
        for old in finished[: max(0, len(JOBS) - MAX_JOBS + 1)]:
            JOBS.pop(old, None)
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {
            "id": job_id, "status": "running", "device": job["device"], "hostname": job["hostname"],
            "output": "", "exit_code": None, "started_at": _now(),
        }
    threading.Thread(target=_run_job, args=(job_id, job, config), daemon=True).start()
    return job_id, []


def make_handler(config: Config) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlaybookSDFlasher/1"

        def log_request(self, code: Any = "-", size: Any = "-") -> None:
            # The dashboard polls every few seconds; only log changes and failures.
            if self.command == "GET" and str(code) == "200":
                return
            super().log_request(code, size)

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

        def _send(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {config.token}"
            if hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                return True
            self._send(401, {"ok": False, "errors": ["Unauthorized."]})
            return False

        def do_GET(self) -> None:
            if not self._authorized():
                return
            if self.path == "/health":
                errors = health_errors(config)
                self._send(200, {"ok": not errors, "root": os.geteuid() == 0, "errors": errors})
            elif self.path == "/devices":
                self._send(200, {"ok": True, "devices": _local_sd_devices()})
            elif self.path.startswith("/jobs/") and JOB_ID_RE.match(self.path[6:]):
                with JOBS_LOCK:
                    job = dict(JOBS.get(self.path[6:]) or {})
                if job:
                    self._send(200, {"ok": True, "job": job})
                else:
                    self._send(404, {"ok": False, "errors": ["Job not found."]})
            else:
                self._send(404, {"ok": False, "errors": ["Not found."]})

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self.path != "/jobs":
                self._send(404, {"ok": False, "errors": ["Not found."]})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send(400, {"ok": False, "errors": ["Invalid request size."]})
                return
            try:
                spec = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                self._send(400, {"ok": False, "errors": ["Invalid JSON."]})
                return
            errors = health_errors(config)
            if errors:
                self._send(503, {"ok": False, "errors": errors})
                return
            job, errors = validate_job(spec, _local_sd_devices(), config)
            if errors:
                self._send(400, {"ok": False, "errors": errors})
                return
            job_id, errors = start_job(job, config)
            if errors:
                self._send(409, {"ok": False, "errors": errors})
                return
            sys.stderr.write(f"SD job {job_id}: {job['hostname']} -> {job['device']}\n")
            self._send(202, {"ok": True, "job_id": job_id})

    return Handler


def read_token(args: argparse.Namespace) -> str:
    token = os.getenv("SD_FLASHER_TOKEN", "").strip()
    if not token and args.token_file:
        text = Path(args.token_file).read_text(encoding="utf-8")
        # Accept a bare token or the KEY=value env file shared with Docker Compose.
        match = re.search(r"^\s*SD_FLASHER_TOKEN\s*=\s*(\S+)\s*$", text, re.MULTILINE)
        token = match.group(1) if match else text.strip()
    return token


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default=os.getenv("SD_FLASHER_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SD_FLASHER_PORT", "8089")))
    parser.add_argument("--token-file", default=os.getenv("SD_FLASHER_TOKEN_FILE", ""))
    parser.add_argument("--script", default=os.getenv("SD_FLASHER_SCRIPT", str(REPO_DIR / "scripts" / "prepare_sd_cloud_init.sh")))
    parser.add_argument("--image-cache-dir", default=os.getenv("SD_FLASHER_IMAGE_CACHE_DIR", str(BASE_DIR / "data" / "image_cache")))
    parser.add_argument("--work-dir", default=os.getenv("SD_FLASHER_WORK_DIR", "/var/tmp/playbook-sd-flasher"))
    args = parser.parse_args(argv)

    token = read_token(args)
    if len(token) < 32:
        parser.error("set SD_FLASHER_TOKEN or --token-file to a token of at least 32 characters")
    config = Config(token, Path(args.script), Path(args.image_cache_dir), Path(args.work_dir))
    for error in health_errors(config):
        sys.stderr.write(f"WARNING: {error}\n")
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(config))
    server.daemon_threads = True
    sys.stderr.write(f"SD flasher listening on http://{args.bind}:{args.port} (image cache {config.image_cache_dir})\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
