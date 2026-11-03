"""Removable SD device listing and the device-change monitor."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List


DEVICE_SUBSCRIBERS: List[queue.Queue[str]] = []


DEVICE_SUBSCRIBERS_LOCK = threading.Lock()


DEVICE_MONITOR_STARTED = False


DEVICE_MONITOR_LOCK = threading.Lock()


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


def _device_signature(devices: List[Dict[str, Any]]) -> str:
    reduced = [
        {
            "path": d.get("path", ""),
            "size": d.get("size", ""),
            "model": d.get("model", ""),
            "transport": d.get("transport", ""),
            "mountpoints": d.get("mountpoints", []),
        }
        for d in devices
    ]
    reduced.sort(key=lambda item: item.get("path", ""))
    return json.dumps(reduced, sort_keys=True, separators=(",", ":"))


def _device_event_message(devices: List[Dict[str, Any]]) -> str:
    payload = {
        "devices": devices,
        "count": len(devices),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return f"event: devices\\ndata: {json.dumps(payload, separators=(',', ':'))}\\n\\n"


def _broadcast_device_update(devices: List[Dict[str, Any]]) -> None:
    message = _device_event_message(devices)
    with DEVICE_SUBSCRIBERS_LOCK:
        subscribers = list(DEVICE_SUBSCRIBERS)
    for sub in subscribers:
        try:
            while sub.full():
                sub.get_nowait()
            sub.put_nowait(message)
        except queue.Empty:
            continue
        except queue.Full:
            continue


def _device_monitor_loop() -> None:
    last_sig = ""
    while True:
        devices = _list_sd_devices()
        sig = _device_signature(devices)
        if sig != last_sig:
            last_sig = sig
            _broadcast_device_update(devices)
        time.sleep(2)


def _ensure_device_monitor_started() -> None:
    global DEVICE_MONITOR_STARTED
    with DEVICE_MONITOR_LOCK:
        if DEVICE_MONITOR_STARTED:
            return
        thread = threading.Thread(target=_device_monitor_loop, daemon=True)
        thread.start()
        DEVICE_MONITOR_STARTED = True
