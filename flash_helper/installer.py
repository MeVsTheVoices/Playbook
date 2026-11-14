"""Build UEFI autoinstall/Kickstart media and write it through the bounded helper."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any

import yaml

from flash_helper.devices import verify
from services.image_download import download
from services.installer_config import validate
from services.sd_prepare import JOBS, JOBS_LOCK
from services.storage import IMAGE_CACHE_DIR


def callback_script(form: dict[str, Any]) -> str:
    event = json.dumps({"hostname": form["hostname"], "profile": form["profile"],
                        "status": "ready", "workflow": "uefi-installer"})
    return (
        "#!/bin/sh\nset -eu\n"
        f"curl --fail --retry 8 --retry-delay 15 --connect-timeout 15 --max-time 120 "
        f"-H 'Content-Type: application/json' --data {shlex.quote(event)} {shlex.quote(form['callback_url'])}\n"
    )


def seed(form: dict[str, Any], family: str, root: Path) -> None:
    options = form["cloud_init_options"]
    user = options["username"]
    selector = (Path(__file__).with_name("installer_target.py")).read_text()
    callback = callback_script(form)
    if family == "ubuntu":
        config = {"autoinstall": {
            "version": 1, "interactive-sections": [], "refresh-installer": {"update": False},
            "locale": options["locale"], "timezone": options["timezone"],
            "keyboard": {"layout": "us"},
            "identity": {"hostname": form["hostname"], "username": user, "password": "!"},
            "ssh": {"install-server": True, "allow-pw": False,
                    "authorized-keys": form["ssh_public_key"].strip().splitlines()},
            "storage": {"layout": {"name": "direct", "match": {"serial": "PLAYBOOK-NO-DISK-UNTIL-PREFLIGHT"}}},
            "early-commands": ["python3 /cdrom/playbook/installer_target.py ubuntu"],
            "packages": ["curl"],
            "late-commands": [
                ["curtin", "in-target", "--", "sh", "-c",
                 f"printf '%s\\n' {shlex.quote(user + ' ALL=(ALL) NOPASSWD:ALL')} > /etc/sudoers.d/90-playbook; "
                 "chmod 440 /etc/sudoers.d/90-playbook"],
                ["curtin", "in-target", "--", "sh", "-c", callback],
            ],
            "shutdown": "poweroff",
        }}
        directory = root / "nocloud"
        directory.mkdir()
        (directory / "user-data").write_text("#cloud-config\n" + yaml.safe_dump(config, sort_keys=False))
        (directory / "meta-data").write_text(f"instance-id: playbook-{form['hostname']}\n")
    else:
        key_lines = []
        for key in form["ssh_public_key"].strip().splitlines():
            if not re.fullmatch(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]+(?: [^\r\n\"]*)?", key):
                raise ValueError("Invalid or unsupported Kickstart SSH public key.")
            key_lines.append(f'sshkey --username={user} "{key}"')
        (root / "ks.cfg").write_text(
            "#version=RHEL9\n"
            "text\ncdrom\nfirstboot --disable\n"
            f"lang {options['locale']}\nkeyboard us\ntimezone {options['timezone']} --utc\n"
            f"network --bootproto=dhcp --device=link --activate --hostname={form['hostname']}\n"
            "rootpw --lock\n"
            f"user --name={user} --groups=wheel --lock\n"
            + "\n".join(key_lines) + "\n"
            "firewall --enabled --service=ssh\nselinux --enforcing\n"
            "services --enabled=sshd\n"
            "%pre --interpreter=/usr/bin/python3 --erroronfail\n"
            + selector.split('if __name__ == "__main__":', 1)[0] + "\nmain('rocky')\n%end\n"
            "%include /tmp/playbook-storage.ks\n"
            "%packages\n@^minimal-environment\nopenssh-server\ncurl\nsudo\n%end\n"
            "%post --erroronfail\nset -eu\n"
            f"printf '%s\\n' {shlex.quote(user + ' ALL=(ALL) NOPASSWD:ALL')} > /etc/sudoers.d/90-playbook\n"
            "chmod 440 /etc/sudoers.d/90-playbook\n"
            "printf 'PasswordAuthentication no\\nPermitRootLogin no\\n' > /etc/ssh/sshd_config.d/90-playbook.conf\n"
            + callback + "\n%end\npoweroff\n"
        )
    playbook = root / "playbook"
    playbook.mkdir()
    (playbook / "installer_target.py").write_text(selector)


def patch_grub(text: str, family: str) -> str:
    lines = []
    count = 0
    for line in text.splitlines():
        if re.match(r"\s*linux(?:efi)?\s", line):
            line = re.sub(r"\b(?:rd.live.check|mediacheck)\b", "", line)
            args = r" autoinstall ds=nocloud\;s=/cdrom/nocloud/" if family == "ubuntu" else " inst.ks=cdrom:/ks.cfg"
            line = line.replace(" ---", args + " ---") if " ---" in line else line + args
            count += 1
        if re.match(r"\s*set timeout=", line):
            line = "set timeout=3"
        if re.match(r"\s*set default=", line):
            line = "set default=0"
        lines.append(line)
    if not count:
        raise ValueError("Installer GRUB configuration has no supported kernel entry.")
    return "set default=0\nset timeout=3\n" + "\n".join(lines) + "\n"


def build_iso(source: Path, output: Path, root: Path, family: str) -> None:
    grub_paths = ("/boot/grub/grub.cfg", "/boot/grub/loopback.cfg") if family == "ubuntu" else ("/EFI/BOOT/grub.cfg",)
    maps = []
    for index, iso_path in enumerate(grub_paths):
        extracted = root.parent / f"grub-{index}.cfg"
        subprocess.run(["xorriso", "-osirrox", "on", "-indev", str(source), "-extract", iso_path, str(extracted)],
                       check=True, capture_output=True, text=True)
        extracted.chmod(0o600)
        extracted.write_text(patch_grub(extracted.read_text(), family))
        maps.extend(["-map", str(extracted), iso_path])
    for child in root.iterdir():
        maps.extend(["-map", str(child), "/" + child.name])
    subprocess.run(["xorriso", "-indev", str(source), "-outdev", str(output),
                    "-overwrite", "on", *maps, "-boot_image", "any", "replay",
                    "-commit", "-end"], check=True, capture_output=True, text=True)


def run(job_id: str, form: dict[str, Any]) -> None:
    entry = validate(form)
    def log(message: str) -> None:
        with JOBS_LOCK:
            JOBS[job_id]["stdout"] = (JOBS[job_id].get("stdout", "") + message + "\n")[-50000:]
    with JOBS_LOCK:
        JOBS[job_id].update(status="running")
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    image = IMAGE_CACHE_DIR / (hashlib.sha256(entry["image_url"].encode()).hexdigest() + ".iso")
    with Path(str(image) + ".lock").open("a") as cache_lock:
        fcntl.flock(cache_lock, fcntl.LOCK_EX)
        if image.exists():
            log("Checking cached installer ISO checksum.")
            with image.open("rb") as source:
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
            if checksum != entry["image_sha256"]:
                raise ValueError("Cached installer checksum mismatch; remove the invalid cache through maintenance.")
        else:
            log("Downloading checksum-pinned installer ISO.")
            fd, name = tempfile.mkstemp(dir=IMAGE_CACHE_DIR, suffix=".part")
            os.close(fd)
            partial = Path(name)
            try:
                checksum = download(entry["image_url"], partial)
                if checksum != entry["image_sha256"]:
                    raise ValueError("Installer download checksum mismatch.")
                partial.replace(image)
                Path(str(image) + ".sha256").write_text(checksum + "\n")
            finally:
                partial.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(prefix="uefi-", dir=os.environ.get("FLASH_WORK_DIR")) as tmp:
            workspace = Path(tmp)
            config = workspace / "seed"
            config.mkdir()
            seed(form, entry["installer_family"], config)
            output = workspace / "installer.iso"
            log("Embedding authorized unattended installer and preserving UEFI boot images.")
            build_iso(image, output, config, entry["installer_family"])
            verify(form["device"], form["device_identity"])
            log("Writing and verifying unattended USB. Booting it authorizes erasing the sole eligible internal disk.")
            script = Path(__file__).resolve().parents[1] / "scripts/lib/flash.sh"
            # Share the existing lock, capacity, system-disk, unmount and descriptor-bound write checks.
            command = (
                'set -euo pipefail; DEVICE="$1"; IMG_TO_WRITE="$2"; SCRIPT_DIR="$3"; TMP_DIR="$4"; '
                'AUTO_CONFIRM=true; FLASH_MEDIA_ONLY=true; exec 8<"$DEVICE"; flock -n 8; '
                'source "$SCRIPT_DIR/lib/flash.sh"'
            )
            result = subprocess.run(["bash", "-c", command, "uefi", form["device"], str(output),
                                     str(script.parent.parent), tmp],
                                    env={**os.environ, "FLASH_EXPECTED_IDENTITY": form["device_identity"]},
                                    capture_output=True, text=True)
            log(result.stdout)
            if result.returncode:
                raise RuntimeError("Installer USB write/verification failed: " + result.stderr[-4000:])
            log("Verified unattended USB is ready. Remove it after the installer powers off.")
    with JOBS_LOCK:
        JOBS[job_id].update(status="completed", exit_code=0)
