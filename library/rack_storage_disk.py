#!/usr/bin/python
"""Ansible module: find, format and mount a spare disk, and report how to find it again."""

import json
import os
import time

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r"""
---
module: rack_storage_disk
short_description: Prepare a spare disk for node storage and report where it ended up
description:
  - Finds a blank disk (or uses the one named), partitions and formats it, mounts it by UUID through /etc/fstab,
    and returns the mount path with the disk's UUID and by-id names.
  - Never touches a disk that has partitions, a filesystem or a mount. The one exception is a filesystem that
    already carries I(label), which is reused and only mounted, so reruns change nothing.
options:
  path:
    description: Mount point, for example /var/lib/longhorn.
    type: path
    required: true
  device:
    description:
      - V(auto) picks the single blank disk. Anything else is a block device or /dev/disk/by-id link for a whole disk.
    type: str
    default: auto
  label:
    description: Filesystem label, also how a disk prepared earlier is recognised (12 characters at most).
    type: str
    default: storage
  fstype:
    description: Filesystem for a new disk.
    type: str
    default: ext4
    choices: [ext4, xfs]
  mount_options:
    description: Options written to /etc/fstab.
    type: str
    default: defaults,noatime,nofail
  identify_by:
    description: Which stable name goes into RV(device_ref).
    type: str
    default: uuid
    choices: [uuid, id]
  fstab:
    description: fstab file to update.
    type: path
    default: /etc/fstab
"""

RETURN = r"""
path:
  description: The mount point.
  returned: always
  type: str
device_ref:
  description: The disk named by I(identify_by), as a /dev/disk/by-uuid or /dev/disk/by-id path.
  returned: always
  type: str
by_uuid:
  description: /dev/disk/by-uuid path of the filesystem.
  returned: when the filesystem exists
  type: str
by_id:
  description: /dev/disk/by-id path of the partition, when the disk has one.
  returned: when the filesystem exists
  type: str
uuid:
  description: Filesystem UUID.
  returned: when the filesystem exists
  type: str
device:
  description: Partition the filesystem lives on.
  returned: when the filesystem exists
  type: str
disk:
  description: Whole disk the partition belongs to.
  returned: always
  type: str
formatted:
  description: Whether a new partition table and filesystem were written.
  returned: always
  type: bool
"""

LSBLK_COLUMNS = "NAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,RM,RO"
# Never auto-picked: loop/ram devices, the SD card, optical and floppy drives, device-mapper and md volumes.
IGNORED_PREFIXES = ("loop", "ram", "zram", "mmcblk", "sr", "fd", "dm-", "md")
WEAK_ID_PREFIXES = ("nvme-eui.", "wwn-")


class PlanError(Exception):
    pass


def truthy(value):
    return value in (True, 1, "1", "true")


def mounts_of(node):
    mounts = node.get("mountpoints")
    if mounts is None:
        mounts = [node.get("mountpoint")]
    return [m for m in mounts if m]


def walk(nodes, top=None):
    """Yield (node, whole_disk) for every device in the lsblk tree."""
    for node in nodes:
        disk = top or node
        yield node, disk
        yield from walk(node.get("children") or [], disk)


def tree_mounts(disk):
    return [m for node, _top in walk([disk]) for m in mounts_of(node)]


def is_blank(disk):
    return not disk.get("children") and not disk.get("fstype") and not mounts_of(disk)


def is_spare(disk):
    name = os.path.basename(disk["name"])
    return (
        disk.get("type") == "disk"
        and not name.startswith(IGNORED_PREFIXES)
        and not truthy(disk.get("rm"))
        and not truthy(disk.get("ro"))
        and is_blank(disk)
    )


def describe(disk):
    size_gib = (disk.get("size") or 0) / 1024 ** 3
    return "%s (%.1f GiB)" % (disk["name"], size_gib)


def make_plan(devices, device, path, label):
    """Decide whether to reuse a mount, mount a labelled filesystem, or format a blank disk."""
    nodes = list(walk(devices))
    by_name = {node["name"]: (node, disk) for node, disk in nodes}

    wanted = None
    if device != "auto":
        real = os.path.realpath(device)
        if real not in by_name:
            raise PlanError("%s is not a block device that lsblk can see" % device)
        wanted = by_name[real]

    mounted = next(((n, d) for n, d in nodes if path in mounts_of(n)), None)
    if mounted:
        if wanted and wanted[1]["name"] != mounted[1]["name"]:
            raise PlanError("%s is already mounted from %s, not from %s" % (path, mounted[0]["name"], device))
        return {"action": "present", "node": mounted[0], "disk": mounted[1]}

    def labelled(disk_filter):
        return next(
            ((n, d) for n, d in nodes if n.get("fstype") and n.get("label") == label and not mounts_of(n) and disk_filter(d)),
            None,
        )

    if wanted:
        disk = wanted[1]
        hit = labelled(lambda d: d["name"] == disk["name"])
        if hit:
            return {"action": "mount", "node": hit[0], "disk": hit[1]}
        if wanted[0]["name"] == disk["name"] and is_blank(disk):
            return {"action": "format", "node": None, "disk": disk}
        raise PlanError(
            "%s is not blank (partitions, a filesystem or a mount were found) and has no filesystem labelled '%s'; "
            "refusing to touch it" % (device, label)
        )

    hit = labelled(lambda d: d.get("type") == "disk")
    if hit:
        return {"action": "mount", "node": hit[0], "disk": hit[1]}

    spare = [d for d in devices if is_spare(d)]
    if not spare:
        raise PlanError("no blank disk found")
    if len(spare) > 1:
        raise PlanError("more than one blank disk found (%s); set device to choose one" % ", ".join(describe(d) for d in spare))
    return {"action": "format", "node": None, "disk": spare[0]}


def update_fstab(text, uuid, path, fstype, options):
    """Return (new_text, changed) with exactly one entry for this UUID and mount point."""
    desired = "UUID=%s %s %s %s 0 2" % (uuid, path, fstype, options)
    kept = []
    found = False
    for line in text.splitlines():
        fields = line.split()
        clashes = fields and not fields[0].startswith("#") and len(fields) >= 2 and (fields[1] == path or fields[0] == "UUID=" + uuid)
        if not clashes:
            kept.append(line)
        elif not found and fields == desired.split():
            kept.append(line)
            found = True
    if not found:
        kept.append(desired)
    new_text = "\n".join(kept) + "\n"
    return new_text, new_text.rstrip("\n") != text.rstrip("\n")


def find_by_id(device_path, by_id_dir="/dev/disk/by-id"):
    try:
        names = os.listdir(by_id_dir)
    except OSError:
        return None
    links = sorted(
        (name for name in names if os.path.realpath(os.path.join(by_id_dir, name)) == device_path),
        key=lambda name: (name.startswith(WEAK_ID_PREFIXES), name),
    )
    return os.path.join(by_id_dir, links[0]) if links else None


def read_blockdevices(module):
    err = ""
    # Older util-linux has MOUNTPOINT but not MOUNTPOINTS.
    for columns in (LSBLK_COLUMNS, LSBLK_COLUMNS.replace("MOUNTPOINTS", "MOUNTPOINT")):
        rc, out, err = module.run_command(["lsblk", "--json", "--bytes", "--paths", "--output", columns])
        if rc == 0:
            return json.loads(out)["blockdevices"]
    module.fail_json(msg="lsblk failed: %s" % err)


def settle(module):
    udevadm = module.get_bin_path("udevadm")
    if udevadm:
        module.run_command([udevadm, "settle"])


def wait_for(module, finder, what, attempts=15):
    for _ in range(attempts):
        found = finder(read_blockdevices(module))
        if found:
            return found
        time.sleep(1)
    module.fail_json(msg="timed out waiting for %s" % what)


def create_filesystem(module, disk, label, fstype):
    parted = module.get_bin_path("parted", required=True)
    mkfs = module.get_bin_path("mkfs." + fstype, required=True)
    disk_path = disk["name"]

    module.run_command([parted, "--script", "--align", "optimal", disk_path, "mklabel", "gpt", "mkpart", label, fstype, "1MiB", "100%"], check_rc=True)
    settle(module)

    def first_partition(devices):
        for node, top in walk(devices):
            if top["name"] == disk_path and node.get("type") == "part":
                return node
        return None

    partition = wait_for(module, first_partition, "the new partition on %s" % disk_path)

    force = "-F" if fstype == "ext4" else "-f"
    module.run_command([mkfs, force, "-L", label, partition["name"]], check_rc=True)
    settle(module)

    def with_uuid(devices):
        for node, _top in walk(devices):
            if node["name"] == partition["name"] and node.get("uuid"):
                return node
        return None

    return wait_for(module, with_uuid, "the new filesystem UUID on %s" % partition["name"])


def write_fstab(module, fstab, uuid, path, fstype, options):
    try:
        with open(fstab, encoding="utf-8") as handle:
            current = handle.read()
    except FileNotFoundError:
        current = ""
    new_text, changed = update_fstab(current, uuid, path, fstype, options)
    if changed and not module.check_mode:
        temp = fstab + ".rack-tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        if os.path.exists(fstab):
            os.chmod(temp, os.stat(fstab).st_mode & 0o7777)
        module.atomic_move(temp, fstab)
        systemctl = module.get_bin_path("systemctl")
        if systemctl:
            module.run_command([systemctl, "daemon-reload"])
    return changed


def main():
    module = AnsibleModule(
        argument_spec=dict(
            path=dict(type="path", required=True),
            device=dict(type="str", default="auto"),
            label=dict(type="str", default="storage"),
            fstype=dict(type="str", default="ext4", choices=["ext4", "xfs"]),
            mount_options=dict(type="str", default="defaults,noatime,nofail"),
            identify_by=dict(type="str", default="uuid", choices=["uuid", "id"]),
            fstab=dict(type="path", default="/etc/fstab"),
        ),
        supports_check_mode=True,
    )
    path = os.path.normpath(module.params["path"])
    label = module.params["label"]
    if not os.path.isabs(path) or path == "/":
        module.fail_json(msg="path must be an absolute mount point other than /")
    if not 0 < len(label) <= 12:
        module.fail_json(msg="label must be 1 to 12 characters")

    devices = read_blockdevices(module)
    try:
        plan = make_plan(devices, module.params["device"], path, label)
    except PlanError as exc:
        disks = [describe(d) for d in devices if d.get("type") == "disk"]
        module.fail_json(msg=str(exc), disks=disks)

    disk = plan["disk"]
    node = plan["node"]
    changed = False
    formatted = plan["action"] == "format"
    result = dict(path=path, disk=disk["name"], action=plan["action"], formatted=formatted, label=label)

    if formatted:
        changed = True
        if module.check_mode:
            result.update(changed=True, device=None, uuid=None, device_ref=None, msg="would partition, format and mount %s" % describe(disk))
            module.exit_json(**result)
        node = create_filesystem(module, disk, label, module.params["fstype"])

    uuid = node["uuid"]
    fstype = node["fstype"]
    if write_fstab(module, module.params["fstab"], uuid, path, fstype, module.params["mount_options"]):
        changed = True

    if path not in mounts_of(node):
        changed = True
        if not module.check_mode:
            if os.path.isdir(path) and os.listdir(path):
                module.warn("%s already holds files; they are hidden while the disk is mounted there" % path)
            os.makedirs(path, exist_ok=True)
            module.run_command(["mount", path], check_rc=True)
            if not os.path.ismount(path):
                module.fail_json(msg="%s is not a mount point after mounting" % path)

    by_uuid = "/dev/disk/by-uuid/" + uuid
    by_id = find_by_id(node["name"])
    device_ref = by_id if module.params["identify_by"] == "id" else by_uuid
    if device_ref is None:
        module.warn("no /dev/disk/by-id name exists for %s; reporting the UUID path instead" % node["name"])
        device_ref = by_uuid

    result.update(
        changed=changed,
        device=node["name"],
        uuid=uuid,
        fstype=fstype,
        size_bytes=node.get("size"),
        by_uuid=by_uuid,
        by_id=by_id,
        device_ref=device_ref,
        identify_by=module.params["identify_by"],
        msg="%s is on %s" % (path, device_ref),
    )
    module.exit_json(**result)


if __name__ == "__main__":
    main()
