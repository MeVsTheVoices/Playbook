"""Fail-closed discovery and identity checks for explicitly permitted Linux disks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/home", "/usr", "/var"}


def _unescape(path: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), path)


def _descendants(disk: dict[str, Any]) -> list[dict[str, Any]]:
    return [disk, *(child for node in disk.get("children", []) for child in _descendants(node))]


def discover() -> list[dict[str, Any]]:
    allowed = [Path(value.strip()) for value in os.environ.get("FLASH_ALLOWED_DEVICES", "").split(",") if value.strip()]
    if not allowed or any(str(path.parent) != "/dev/disk/by-id" or "-part" in path.name for path in allowed):
        raise ValueError("FLASH_ALLOWED_DEVICES must list whole-disk /dev/disk/by-id paths.")
    mountinfo_path = os.environ.get("FLASH_HOST_MOUNTINFO", "/proc/1/mountinfo")
    mountinfo = Path(mountinfo_path).read_text()
    mounts = [(line.split()[2], _unescape(line.split()[4])) for line in mountinfo.splitlines()]
    protected = {number for number, path in mounts if path in SYSTEM_MOUNTS}
    mounted = {number for number, path in mounts}
    if not protected:
        raise ValueError("Cannot identify host system mounts; refusing device discovery.")
    swaps = Path("/proc/swaps").read_text().splitlines()[1:]
    swap_paths = set()
    for line in swaps:
        source = _unescape(line.split()[0])
        if source.startswith("/dev/"):
            swap_paths.add(str(Path(source).resolve()))
        else:
            candidates = [(number, path) for number, path in mounts if
                          source == path or source.startswith(path.rstrip("/") + "/")]
            if not candidates:
                raise ValueError("Cannot identify the filesystem containing an active swapfile.")
            protected.add(max(candidates, key=lambda mount: len(mount[1]))[0])
    result = subprocess.run(
        ["lsblk", "--json", "--bytes", "--paths", "--output",
         "NAME,PATH,SIZE,MODEL,SERIAL,WWN,MAJ:MIN,TYPE,TRAN,RM,RO,HOTPLUG,MOUNTPOINTS"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    disks = json.loads(result.stdout)["blockdevices"]
    devices = []
    for alias in allowed:
        if not alias.exists():
            continue
        path = str(alias.resolve(strict=True))
        disk = next((disk for disk in disks if disk["path"] == path and disk["type"] == "disk"), None)
        if disk is None or disk.get("ro") or not (
            disk.get("rm") or disk.get("hotplug") or disk.get("tran") in {"usb", "mmc"}
        ):
            continue
        nodes = _descendants(disk)
        if any(node.get("maj:min") in protected or str(Path(node["path"]).resolve()) in swap_paths or
               any(mount in SYSTEM_MOUNTS or mount == "[SWAP]" for mount in node.get("mountpoints") or [])
               for node in nodes):
            continue
        if mountinfo_path != "/proc/1/mountinfo" and any(node.get("maj:min") in mounted for node in nodes):
            raise ValueError(f"{alias} is mounted on the Linux host. Unmount its partitions on that host first.")
        # diskseq changes on unplug/replug even when Linux reuses a device name.
        sequence = (Path("/sys/class/block") / Path(path).name / "diskseq").read_text().strip()
        if not sequence.isdecimal():
            raise ValueError("The kernel did not provide a usable disk sequence.")
        identity = hashlib.sha256(json.dumps([
            str(alias), path, disk["size"], disk.get("serial"), disk.get("wwn"),
            disk["maj:min"], sequence,
        ]).encode()).hexdigest()
        devices.append({
            "path": str(alias), "name": Path(path).name, "size": disk["size"],
            "model": disk.get("model") or "", "serial": disk.get("serial") or "",
            "transport": disk.get("tran") or "", "identity": identity,
            "mountpoints": [mount for node in nodes for mount in node.get("mountpoints") or [] if mount],
        })
    return devices


def verify(path: str, identity: str) -> None:
    if not identity or not any(device["path"] == path and device["identity"] == identity for device in discover()):
        raise ValueError("Target is absent, protected, or changed since confirmation. Refresh and confirm again.")


if __name__ == "__main__":
    try:
        verify(*sys.argv[1:])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        sys.exit(f"Flash target verification failed: {exc}")
