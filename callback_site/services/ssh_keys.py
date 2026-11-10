"""SSH key generation, archive, known_hosts and key-permission helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from services.storage import (
    BASE_DIR,
    DATA_DIR,
    KEYS_DIR,
    KEY_ARCHIVE_DIR,
    KEY_ARCHIVE_INDEX_FILE,
    KNOWN_HOSTS_FILE,
)


def _resolve_download_path(download_path: str) -> Path | None:
    path_raw = str(download_path or "").strip()
    if not path_raw:
        return None

    candidate = Path(path_raw)
    candidates: List[Path] = []
    if candidate.is_absolute():
        candidates.append(candidate)
    else:
        candidates.extend(
            [
                (BASE_DIR.parent / candidate),
                (DATA_DIR / candidate),
                (BASE_DIR / candidate),
                candidate,
            ]
        )

    for resolved in candidates:
        if resolved.exists():
            return resolved
    return None


def _ensure_keys_dir() -> None:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(KEYS_DIR, 0o700)
    except PermissionError:
        # Best effort; keep running on filesystems that ignore chmod semantics.
        pass
    _normalize_ssh_key_artifacts()


def _data_dir_owner() -> tuple[int, int] | None:
    try:
        stat_result = DATA_DIR.stat()
        return stat_result.st_uid, stat_result.st_gid
    except OSError:
        return None


def _chown_to_data_owner(path: Path) -> None:
    if os.geteuid() != 0:
        return
    owner = _data_dir_owner()
    if owner is None:
        return
    uid, gid = owner
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def _normalize_ssh_key_artifacts() -> None:
    try:
        key_files = list(KEYS_DIR.iterdir())
    except OSError:
        return

    for key_file in key_files:
        if not key_file.is_file():
            continue
        _chown_to_data_owner(key_file)
        target_mode = 0o644 if key_file.name.endswith(".pub") else 0o600
        try:
            os.chmod(key_file, target_mode)
        except PermissionError:
            pass


def _ensure_known_hosts_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not KNOWN_HOSTS_FILE.exists():
        KNOWN_HOSTS_FILE.touch()
    _chown_to_data_owner(KNOWN_HOSTS_FILE)
    try:
        os.chmod(KNOWN_HOSTS_FILE, 0o600)
    except PermissionError:
        pass


def _seed_known_host(host: str, port: int = 22) -> None:
    host_text = str(host or "").strip()
    if not host_text or not _is_safe_ssh_host(host_text):
        return

    _ensure_known_hosts_file()
    known_hosts_path = str(KNOWN_HOSTS_FILE)
    match_tokens = [host_text]
    if int(port or 22) != 22:
        match_tokens.insert(0, f"[{host_text}]:{int(port)}")

    try:
        for token in match_tokens:
            match_proc = subprocess.run(
                ["ssh-keygen", "-F", token, "-f", known_hosts_path],
                check=False,
                capture_output=True,
                text=True,
            )
            if match_proc.returncode == 0:
                return

        scan_cmd = ["ssh-keyscan", "-T", "5"]
        if int(port or 22) != 22:
            scan_cmd.extend(["-p", str(int(port))])
        scan_cmd.extend(["--", host_text])
        scan_proc = subprocess.run(scan_cmd, check=False, capture_output=True, text=True)
        scanned = str(scan_proc.stdout or "").strip()
        if scan_proc.returncode != 0 or not scanned:
            return

        with KNOWN_HOSTS_FILE.open("a", encoding="utf-8") as kh:
            kh.write(scanned + "\n")
        _ensure_known_hosts_file()
    except (FileNotFoundError, OSError, ValueError):
        return


def _ansible_known_hosts_args() -> str:
    _ensure_known_hosts_file()
    return f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={KNOWN_HOSTS_FILE} -o PreferredAuthentications=publickey"


def _ssh_private_key_preflight(private_key_path: str) -> str:
    path = Path(str(private_key_path or "").strip())
    if not path.exists() or not path.is_file():
        return f"SSH private key not found: {path}"
    if not os.access(path, os.R_OK):
        return (
            f"SSH private key is not readable by user '{os.getenv('USER', 'current user')}': {path}. "
            f"Run: sudo chown $USER:$USER {path} {path}.pub && sudo chmod 600 {path} && sudo chmod 644 {path}.pub"
        )
    return ""


def _ensure_key_archive_dir() -> None:
    KEY_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(KEY_ARCHIVE_DIR, 0o700)
    except PermissionError:
        pass


def _archive_ssh_public_key(job_id: str, form: Dict[str, Any], public_key: str) -> Dict[str, str]:
    _ensure_key_archive_dir()
    if not KEY_ARCHIVE_INDEX_FILE.exists():
        KEY_ARCHIVE_INDEX_FILE.touch()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    profile = str(form.get("profile", "unknown")).strip() or "unknown"
    hostname = str(form.get("hostname", "host")).strip() or "host"
    safe_host = re.sub(r"[^A-Za-z0-9._-]+", "-", hostname)
    archive_name = f"{ts}-{safe_host}-{profile}-{job_id[:8]}.pub"
    archive_path = KEY_ARCHIVE_DIR / archive_name
    archive_path.write_text(public_key.strip() + "\n", encoding="utf-8")

    fingerprint = ""
    try:
        fp_proc = subprocess.run(
            ["ssh-keygen", "-lf", str(archive_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if fp_proc.returncode == 0 and fp_proc.stdout:
            # Output format example: "256 SHA256:abcd... comment (ED25519)"
            parts = fp_proc.stdout.strip().split()
            if len(parts) >= 2:
                fingerprint = parts[1]
    except FileNotFoundError:
        fingerprint = ""

    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "hostname": hostname,
        "profile": profile,
        "device": str(form.get("device", "")).strip(),
        "archive_path": str(archive_path),
        "fingerprint": fingerprint,
    }
    with KEY_ARCHIVE_INDEX_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")

    return {"archive_path": str(archive_path), "fingerprint": fingerprint}


def _is_safe_key_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", name))


# The first character can never be "-", so ssh and ssh-keyscan cannot read the value as an option.
_SSH_USER_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,31}")
_SSH_HOST_PATTERN = re.compile(r"[A-Za-z0-9_:\[][A-Za-z0-9._:%\[\]-]{0,253}")


def _is_safe_ssh_user(name: str) -> bool:
    return bool(_SSH_USER_PATTERN.fullmatch(str(name or "")))


def _is_safe_ssh_host(host: str) -> bool:
    return bool(_SSH_HOST_PATTERN.fullmatch(str(host or "")))


def _ssh_destination(user: str, host: str) -> str:
    if not _is_safe_ssh_user(user):
        raise ValueError("invalid SSH username")
    if not _is_safe_ssh_host(host):
        raise ValueError("invalid SSH host")
    return f"{user}@{host}"


def _ensure_ssh_key_permissions(private_key: Path, public_key: Path) -> None:
    try:
        os.chmod(private_key, 0o600)
    except (FileNotFoundError, PermissionError):
        pass
    try:
        os.chmod(public_key, 0o644)
    except (FileNotFoundError, PermissionError):
        pass


def _generate_or_load_ssh_key(key_name: str, comment: str = "rack-bootstrap", mode: str = "kload") -> Dict[str, Any]:
    _ensure_keys_dir()
    private_key = KEYS_DIR / key_name
    public_key = KEYS_DIR / f"{key_name}.pub"
    mode = str(mode or "kload").strip().lower()
    if mode not in {"generate", "kload"}:
        raise ValueError("mode must be generate or kload")

    created = False
    private_exists = private_key.exists()
    public_exists = public_key.exists()

    if mode == "generate" and (private_exists or public_exists):
        raise FileExistsError("key already exists; use mode 'kload' to load existing key")

    if mode == "generate" or not private_exists or not public_exists:
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

    _ensure_ssh_key_permissions(private_key, public_key)

    pub = public_key.read_text(encoding="utf-8").strip()
    return {
        "created": created,
        "mode": mode,
        "key_name": key_name,
        "public_key": pub,
        "private_key_path": str(private_key),
        "public_key_path": str(public_key),
    }


def _list_ssh_keys() -> List[Dict[str, Any]]:
    _ensure_keys_dir()
    records: List[Dict[str, Any]] = []
    for private_key in sorted(KEYS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not private_key.is_file() or private_key.name.endswith(".pub"):
            continue
        public_key = KEYS_DIR / f"{private_key.name}.pub"
        records.append(
            {
                "key_name": private_key.name,
                "private_key_exists": True,
                "public_key_exists": public_key.exists(),
            }
        )
    return records
