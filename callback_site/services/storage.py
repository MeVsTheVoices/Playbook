"""Data paths and low-level file helpers shared by every service."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


APPDATA_DIR = Path(os.getenv("CALLBACK_APPDATA_DIR", str(BASE_DIR / "data"))).expanduser()


DATA_DIR = APPDATA_DIR


DATA_FILE = DATA_DIR / "callbacks.jsonl"


IMAGE_HISTORY_FILE = DATA_DIR / "image_download_catalog.jsonl"


IMAGE_CACHE_DIR = DATA_DIR / "image_cache"


KEYS_DIR = DATA_DIR / "ssh_keys"


KNOWN_HOSTS_FILE = DATA_DIR / "known_hosts"


KEY_ARCHIVE_DIR = DATA_DIR / "ssh_key_archive"


KEY_ARCHIVE_INDEX_FILE = DATA_DIR / "ssh_key_archive.jsonl"


NODE_CONFIG_FILE = DATA_DIR / "node_monitor_config.json"


NODE_PROVISION_FILE = DATA_DIR / "node_provisioning.json"


MONITOR_STATE_FILE = DATA_DIR / "node_monitor_state.json"


APP_SETTINGS_FILE = DATA_DIR / "app_settings.json"


SD_OPTION_PROFILES_FILE = DATA_DIR / "sd_option_profiles.json"


SD_OPTION_PROFILE_STATE_FILE = DATA_DIR / "sd_option_profile_state.json"


PROFILE_SEGMENTS_FILE = DATA_DIR / "profile_segments.json"


MODULE_PRESETS_FILE = DATA_DIR / "module_presets.json"


BOOT_LOGS_DIR = DATA_DIR / "boot_logs"


CONNECTION_PROFILES_FILE = DATA_DIR / "connection_profiles.json"


INVENTORY_FILE = BASE_DIR.parent / "inventory.yml"


SCRIPT_PATH = BASE_DIR.parent / "scripts" / "prepare_sd_cloud_init.sh"


def _ensure_data_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.touch()


def _ensure_image_history_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not IMAGE_HISTORY_FILE.exists():
        IMAGE_HISTORY_FILE.touch()


def _ensure_image_cache_dir() -> None:
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o666) -> None:
    # Temp-file then replace: keeps JSON files from becoming cursed half-writes.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, mode)
        tmp_path.replace(path)
        try:
            os.chmod(path, mode)
        except OSError:
            if mode != 0o666:
                raise
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
