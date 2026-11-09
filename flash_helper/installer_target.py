"""Runs inside the installer before any storage configuration is applied."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


MIN_BYTES = 16 * 1024 ** 3


def descendants(node: dict) -> list[dict]:
    return [node, *(child for node in node.get("children", []) for child in descendants(node))]


def choose_disks(snapshot: dict) -> dict:
    candidates = []
    installer_found = False
    for disk in snapshot["blockdevices"]:
        nodes = descendants(disk)
        mounts = [mount for node in nodes for mount in node.get("mountpoints") or [] if mount]
        installer = any(mount == "/cdrom" or mount.startswith(("/run/install", "/run/media", "/isodevice"))
                        for mount in mounts)
        installer_found |= installer
        if disk.get("type") != "disk" or installer:
            continue
        if disk.get("ro") or disk.get("rm") or disk.get("tran") == "usb":
            continue
        if any(node.get("type") in {"crypt", "lvm", "raid0", "raid1", "raid5", "raid6", "raid10", "mpath"}
               for node in nodes):
            raise ValueError("An active mapped/RAID device exists; refusing automatic installation.")
        if int(disk.get("size") or 0) < MIN_BYTES or mounts:
            continue
        candidates.append(disk)
    if not installer_found:
        raise ValueError("Cannot identify the booted installer media; refusing automatic installation.")
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one eligible non-installer disk, found {len(candidates)}.")
    disk = candidates[0]
    if not disk.get("serial") and not disk.get("wwn"):
        raise ValueError("Destination disk has no stable serial/WWN; refusing automatic installation.")
    return disk


def main(mode: str) -> None:
    if not Path("/sys/firmware/efi").is_dir():
        raise ValueError("This media supports UEFI boot only.")
    result = subprocess.run(
        ["lsblk", "--json", "--bytes", "--paths", "--output",
         "PATH,TYPE,SIZE,RO,RM,TRAN,SERIAL,WWN,MOUNTPOINTS"],
        capture_output=True, text=True, check=True,
    )
    disk = choose_disks(json.loads(result.stdout))
    if mode == "ubuntu":
        import yaml

        config = Path("/autoinstall.yaml")
        document = yaml.safe_load(config.read_text())
        autoinstall = document.get("autoinstall", document)
        storage = autoinstall["storage"]
        storage["layout"]["match"] = {"serial": disk["serial"]} if disk.get("serial") else {"path": disk["path"]}
        config.write_text(yaml.safe_dump(document, sort_keys=False))
    elif mode == "rocky":
        name = Path(disk["path"]).name
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in name):
            raise ValueError("Unsupported destination disk name.")
        Path("/tmp/playbook-storage.ks").write_text(
            f"ignoredisk --only-use={name}\n"
            f"clearpart --all --initlabel --drives={name}\n"
            f"bootloader --boot-drive={name}\n"
            "autopart --type=lvm\n"
        )
    else:
        raise ValueError("Unknown installer mode.")
    print(f"Authorized unattended destination: {disk['path']} ({disk['size']} bytes).")


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(f"INSTALLATION BLOCKED: {exc}")
