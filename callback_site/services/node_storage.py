"""Ask a node, through Ansible's rack_storage_disk module, which disks could hold Longhorn data."""

from __future__ import annotations

import json
import re
from typing import Any

from services.node_network import run_ansible_module

DEFAULT_PATH = "/var/lib/longhorn"
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,200}")
_SAFE_DEVICE = re.compile(r"auto|/dev/[A-Za-z0-9._/:-]{1,200}")


def read_longhorn_disks(node_name: str, path: str = "", device: str = "") -> dict[str, Any]:
    path = (path or "").strip() or DEFAULT_PATH
    device = (device or "").strip() or "auto"
    if not _SAFE_PATH.fullmatch(path) or ".." in path.split("/"):
        return {"ok": False, "node": node_name, "error": "Storage path must be an absolute path."}
    if not _SAFE_DEVICE.fullmatch(device):
        return {"ok": False, "node": node_name, "error": "Storage disk must be auto or a /dev path (Longhorn never uses none)."}
    args = json.dumps({"path": path, "device": device, "label": "longhorn", "nvme_only": True, "root_fallback": True, "report_only": True})
    result, response = run_ansible_module(node_name, "rack_storage_disk", args, alias="storage_target", library=True)
    if response is None:
        return result
    disks = response.get("disks")
    plan = response.get("plan")
    if not isinstance(disks, list) or not isinstance(plan, dict):
        result["error"] = "rack_storage_disk did not return a disk report; is the node running the current module?"
        return result
    result.update(ok=True, path=path, device=device, disks=disks, plan=plan)
    return result
