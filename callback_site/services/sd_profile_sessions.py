"""Browser-session cloud-init selection and private credential storage."""

import json
import re
import secrets
import threading

from flask import g, request
from services.sd_prepare import SD_SECRET_OPTIONS
from services.storage import APPDATA_DIR, _atomic_write_text

CONFIGURED_SECRET = "__CONFIGURED__"
SESSION_COOKIE = "playbook_sd_session"
SESSION_DIR = APPDATA_DIR / "sd_profile_sessions"
SESSION_LOCK = threading.RLock()


def _sd_session_id() -> str:
    if not hasattr(g, "sd_profile_session_id"):
        ident = request.cookies.get(SESSION_COOKIE, "")
        if not re.fullmatch(r"[a-f0-9]{64}", ident):
            ident = secrets.token_hex(32)
            g.sd_profile_new_session = True
        g.sd_profile_session_id = ident
    return g.sd_profile_session_id


def _load_sd_session() -> dict:
    path = SESSION_DIR / f"{_sd_session_id()}.json"
    if not path.exists():
        return {"selected_profile": "", "selected_ssh_key": "", "credentials": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError("Saved cloud-init session is corrupt; restore it from backup.") from exc
    if not isinstance(state, dict) or not isinstance(state.get("credentials"), dict) or not isinstance(state.get("selected_profile"), str):
        raise RuntimeError("Saved cloud-init session has an invalid format.")
    if any(not isinstance(value, dict) or any(key not in SD_SECRET_OPTIONS or not isinstance(secret, str) for key, secret in value.items()) for value in state["credentials"].values()):
        raise RuntimeError("Saved cloud-init credentials have an invalid format.")
    state.setdefault("selected_ssh_key", "")
    if not isinstance(state["selected_ssh_key"], str):
        raise RuntimeError("Saved SSH-key selection has an invalid format.")
    return state


def _save_sd_session(state: dict) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SESSION_DIR.chmod(0o700)
    _atomic_write_text(SESSION_DIR / f"{_sd_session_id()}.json", json.dumps(state, indent=2), mode=0o600)


def _selected_sd_session_profile() -> str:
    return _load_sd_session()["selected_profile"]


def _select_sd_session_profile(name: str | None = None, ssh_key: str | None = None) -> None:
    with SESSION_LOCK:
        state = _load_sd_session()
        if name is not None:
            state["selected_profile"] = name
        if ssh_key is not None:
            state["selected_ssh_key"] = ssh_key
        _save_sd_session(state)


def _session_sd_profile(profile: dict) -> dict:
    result = {**profile, "options": {key: value for key, value in profile.get("options", {}).items() if key not in SD_SECRET_OPTIONS}}
    for key, value in _load_sd_session()["credentials"].get(profile["name"], {}).items():
        if value:
            result["options"][key] = CONFIGURED_SECRET
    return result


def _resolve_sd_session_secrets(name: str, options: dict) -> dict:
    result = dict(options)
    saved = _load_sd_session()["credentials"].get(name, {})
    for key in SD_SECRET_OPTIONS:
        if result.get(key) == CONFIGURED_SECRET:
            if not saved.get(key):
                raise ValueError(f"No saved {key} for this browser session. Enter it again.")
            result[key] = saved[key]
    return result


def _retain_sd_session_secrets(name: str, options: dict, clear: list | None = None) -> None:
    with SESSION_LOCK:
        state = _load_sd_session()
        saved = state["credentials"].setdefault(name, {})
        for key in clear or []:
            saved.pop(key, None)
        for key in SD_SECRET_OPTIONS:
            value = options.get(key)
            if value == CONFIGURED_SECRET:
                if not saved.get(key):
                    raise ValueError(f"No saved {key} for this browser session. Enter it again.")
            elif isinstance(value, str) and value:
                saved[key] = value
        state["selected_profile"] = name
        _save_sd_session(state)


def _forget_sd_session_profile(name: str) -> None:
    with SESSION_LOCK:
        state = _load_sd_session()
        state["credentials"].pop(name, None)
        if state["selected_profile"] == name:
            state["selected_profile"] = ""
        _save_sd_session(state)
