"""Dashboard account, browser sessions, and the gate that keeps node endpoints open."""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from flask import Response, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from services.storage import APPDATA_DIR

AUTH_FILE = APPDATA_DIR / "admin_auth.json"
SECRET_KEY_FILE = APPDATA_DIR / "admin_secret_key"
SESSION_LIFETIME = timedelta(days=7)
SESSION_KEY = "admin"
CSRF_KEY = "csrf"
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
FREE_LOGIN_ATTEMPTS = 5
MAX_LOGIN_DELAY_SECONDS = 300

# Cloud-init, installer scripts, and the container healthcheck call these without a browser session.
NODE_PATHS = frozenset({"/health", "/api/cloud-init/callback", "/api/cloud-init/boot-log"})
NODE_PATH_PREFIXES = ("/api/ssh/public/",)

_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILURES = {"count": 0, "blocked_until": 0.0}
_DUMMY_HASH: list[str] = []


class AuthStateError(RuntimeError):
    """The account file exists but cannot be trusted."""


def _create_private_file(path: Path, text: str) -> bool:
    """Write a 0600 file only if none exists; never exposes a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp_name)


def load_secret_key() -> bytes:
    for _ in range(2):
        try:
            text = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            _create_private_file(SECRET_KEY_FILE, secrets.token_hex(32))
            continue
        if re.fullmatch(r"[0-9a-f]{64}", text):
            return bytes.fromhex(text)
        raise AuthStateError(f"{SECRET_KEY_FILE} is corrupt; delete it and restart to sign everyone out.")
    raise AuthStateError(f"Could not create {SECRET_KEY_FILE}.")


def load_account() -> Optional[Dict[str, str]]:
    try:
        data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AuthStateError(f"{AUTH_FILE} is unreadable; delete it to run first-time setup again.") from exc
    if not isinstance(data, dict) or not all(
        isinstance(data.get(key), str) and data.get(key) for key in ("username", "password_hash", "epoch")
    ):
        raise AuthStateError(f"{AUTH_FILE} has an invalid format; delete it to run first-time setup again.")
    return {key: data[key] for key in ("username", "password_hash", "epoch")}


def validate_credentials(username: str, password: str, confirm: str) -> Optional[str]:
    if not USERNAME_PATTERN.fullmatch(username):
        return "Username: 1-64 letters, digits, or . _ @ -, starting with a letter or digit."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    if not hmac.compare_digest(password, confirm):
        return "Passwords do not match."
    return None


def create_account(username: str, password: str) -> Optional[Dict[str, str]]:
    """Create the only dashboard account; None means one already exists."""
    account = {
        "username": username,
        "password_hash": generate_password_hash(password),
        "epoch": secrets.token_hex(16),
    }
    if not _create_private_file(AUTH_FILE, json.dumps(account, indent=2) + "\n"):
        return None
    return account


def login_retry_after() -> int:
    with _LOGIN_LOCK:
        return max(0, int(_LOGIN_FAILURES["blocked_until"] - time.monotonic() + 0.999))


def verify_login(username: str, password: str) -> Optional[Dict[str, str]]:
    account = load_account()
    if account is None or len(password) > MAX_PASSWORD_LENGTH:
        # Hashing anyway keeps a failed lookup as slow as a wrong password.
        if not _DUMMY_HASH:
            _DUMMY_HASH.append(generate_password_hash(secrets.token_urlsafe(16)))
        check_password_hash(_DUMMY_HASH[0], password[:MAX_PASSWORD_LENGTH])
        valid = False
    else:
        password_ok = check_password_hash(account["password_hash"], password)
        valid = hmac.compare_digest(username, account["username"]) and password_ok
    with _LOGIN_LOCK:
        if valid:
            _LOGIN_FAILURES.update(count=0, blocked_until=0.0)
            return account
        # The source IP is unreliable behind Docker Desktop, so the backoff is global.
        _LOGIN_FAILURES["count"] += 1
        excess = _LOGIN_FAILURES["count"] - FREE_LOGIN_ATTEMPTS
        if excess > 0:
            delay = min(MAX_LOGIN_DELAY_SECONDS, 2 ** min(excess, 16))
            _LOGIN_FAILURES["blocked_until"] = time.monotonic() + delay
    return None


def reset_login_backoff() -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.update(count=0, blocked_until=0.0)


def session_claims(account: Dict[str, str]) -> Dict[str, Any]:
    return {SESSION_KEY: {"user": account["username"], "epoch": account["epoch"]}}


def start_session(account: Dict[str, str]) -> None:
    session.clear()
    session.update(session_claims(account))
    session.permanent = True


def current_user(account: Optional[Dict[str, str]]) -> str:
    claims = session.get(SESSION_KEY)
    if not account or not isinstance(claims, dict):
        return ""
    # Recreating the account rotates the epoch, which signs out every earlier session.
    if not hmac.compare_digest(str(claims.get("epoch", "")), account["epoch"]):
        return ""
    if claims.get("user") != account["username"]:
        return ""
    return account["username"]


def csrf_token() -> str:
    token = session.get(CSRF_KEY)
    if not isinstance(token, str) or len(token) != 64:
        token = secrets.token_hex(32)
        session[CSRF_KEY] = token
    return token


def csrf_valid(submitted: Any) -> bool:
    token = session.get(CSRF_KEY)
    return isinstance(token, str) and isinstance(submitted, str) and hmac.compare_digest(token, submitted)


def safe_next_path(value: Any) -> str:
    text = str(value or "")
    if not text.startswith("/") or text.startswith(("//", "/\\")) or urlsplit(text).netloc:
        return "/"
    return text


def setup_host_allowed() -> bool:
    """Block DNS-rebinding pages from claiming a fresh install through a hostile hostname."""
    hostname = (urlsplit(f"//{request.host}").hostname or "").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    allowed = {name.strip().lower() for name in os.getenv("CALLBACK_ADMIN_HOSTS", "").split(",") if name.strip()}
    return hostname in allowed


def is_node_path(path: str) -> bool:
    return path in NODE_PATHS or path.startswith(NODE_PATH_PREFIXES)


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.method not in ("GET", "HEAD")


def _refuse(status: int, message: str, **extra: Any) -> Response:
    if _wants_json():
        response = jsonify({"ok": False, "error": message, **extra})
    else:
        response = Response(message + "\n", mimetype="text/plain")
    response.status_code = status
    return response


def _on_node_only_port() -> bool:
    node_port = os.getenv("CALLBACK_NODE_ONLY_PORT", "").strip()
    return bool(node_port) and str(request.environ.get("SERVER_PORT", "")) == node_port


def gate_request() -> Optional[Response]:
    path = request.path
    if _on_node_only_port():
        if is_node_path(path):
            return None
        admin_port = os.getenv("CALLBACK_ADMIN_PORT", "8091")
        return _refuse(
            404,
            f"This port only serves node callbacks. Open the dashboard on port {admin_port} "
            f"(http://127.0.0.1:{admin_port} on the Docker host by default).",
        )
    if is_node_path(path) or path.startswith("/static/"):
        return None
    try:
        account = load_account()
    except AuthStateError as exc:
        return _refuse(503, str(exc))
    if path in ("/setup", "/login", "/logout"):
        return None
    if account is None:
        if _wants_json():
            return _refuse(401, "Create the dashboard account first.", setup_url=url_for("auth_setup"))
        return redirect(url_for("auth_setup"))
    user = current_user(account)
    if user:
        g.admin_user = user
        return None
    if _wants_json():
        return _refuse(401, "Sign in to use the dashboard.", login_url=url_for("auth_login"))
    return redirect(url_for("auth_login", next=safe_next_path(request.full_path.rstrip("?"))))
