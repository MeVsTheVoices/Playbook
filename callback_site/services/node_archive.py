"""Non-destructive hiding of retired nodes from active operator views."""

import json
import re
import threading
from datetime import datetime, timezone

from services.storage import APPDATA_DIR, _atomic_write_text
from services.node_state import _node_aliases

ARCHIVE_FILE = APPDATA_DIR / "archived_nodes.json"
ARCHIVE_LOCK = threading.Lock()


def node_key(name: str) -> str:
    return min(_node_aliases(name), key=len, default="")


def validate_node_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", name):
        raise ValueError("Invalid node name.")


def archived_nodes() -> list[dict]:
    if not ARCHIVE_FILE.exists():
        return []
    try:
        records = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Node archive is corrupt; restore it from a backup.") from exc
    if not isinstance(records, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str)
        or not isinstance(item.get("archived_at"), str) for item in records
    ):
        raise RuntimeError("Node archive is invalid; restore it from a backup.")
    for item in records:
        try:
            validate_node_name(item["name"])
            datetime.fromisoformat(item["archived_at"])
        except ValueError as exc:
            raise RuntimeError("Node archive is invalid; restore it from a backup.") from exc
    return records


def active_nodes(records: list[dict]) -> list[dict]:
    hidden = {node_key(item["name"]) for item in archived_nodes()}
    return [
        item for item in records
        if not any(node_key(item.get(field, "")) in hidden for field in ("name", "hostname", "fqdn") if item.get(field))
    ]


def active_node_mapping(records: dict) -> dict:
    return {item["name"]: item["value"] for item in active_nodes([{"name": name, "value": value} for name, value in records.items()])}


def set_archived(name: str, archived: bool) -> None:
    validate_node_name(name)
    with ARCHIVE_LOCK:
        records = archived_nodes()
        if archived and any(node_key(item["name"]) == node_key(name) for item in records):
            return
        records = [item for item in records if node_key(item["name"]) != node_key(name)]
        if archived:
            records.append({"name": name, "archived_at": datetime.now(timezone.utc).isoformat()})
        _atomic_write_text(ARCHIVE_FILE, json.dumps(records, indent=2))
