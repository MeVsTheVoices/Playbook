"""Removable SD device listing and the device-change monitor."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from flash_helper.devices import discover_removable

logger = logging.getLogger(__name__)

DEVICE_SUBSCRIBERS: List[queue.Queue[str]] = []


DEVICE_SUBSCRIBERS_LOCK = threading.Lock()


DEVICE_MONITOR_STARTED = False


DEVICE_MONITOR_LOCK = threading.Lock()


def _list_sd_devices() -> List[Dict[str, Any]]:
    from services import sd_flasher_client

    if sd_flasher_client.enabled():
        return sd_flasher_client.list_devices()
    return _local_sd_devices()


def _local_sd_devices() -> List[Dict[str, Any]]:
    return discover_removable()


def _device_signature(devices: List[Dict[str, Any]]) -> str:
    reduced = [
        {
            "path": d.get("path", ""),
            "size": d.get("size", ""),
            "model": d.get("model", ""),
            "transport": d.get("transport", ""),
            "mountpoints": d.get("mountpoints", []),
            "identity": d.get("identity", ""),
        }
        for d in devices
    ]
    reduced.sort(key=lambda item: item.get("path", ""))
    return json.dumps(reduced, sort_keys=True, separators=(",", ":"))


def _device_event_message(devices: List[Dict[str, Any]], error: str = "") -> str:
    payload = {
        "devices": devices,
        "count": len(devices),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }
    return f"event: devices\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _broadcast_device_update(devices: List[Dict[str, Any]], error: str = "") -> None:
    message = _device_event_message(devices, error)
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
    last_error = ""
    while True:
        try:
            devices = _list_sd_devices()
            last_error = ""
        except (OSError, ValueError, KeyError, subprocess.SubprocessError, RuntimeError) as exc:
            if str(exc) != last_error:
                logger.warning("SD device discovery failed: %s", exc)
                last_error = str(exc)
            devices = []
        sig = _device_signature(devices) + last_error
        if sig != last_sig:
            last_sig = sig
            _broadcast_device_update(devices, last_error)
        time.sleep(2)


def _ensure_device_monitor_started() -> None:
    global DEVICE_MONITOR_STARTED
    with DEVICE_MONITOR_LOCK:
        if DEVICE_MONITOR_STARTED:
            return
        thread = threading.Thread(target=_device_monitor_loop, daemon=True)
        thread.start()
        DEVICE_MONITOR_STARTED = True
