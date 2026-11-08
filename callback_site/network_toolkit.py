"""Pi 3B+ network appliance settings shared by imaging and Node Ops."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from package_options import package_names

PROFILE = "network-toolkit"
GROUP = "network_toolkit_active"
ROLE_DIR = Path(__file__).resolve().parents[1] / "roles" / "network_toolkit"
IMAGE_URL = "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/2026-10-06-raspios-trixie-arm64-lite.img.xz"
INPUT_SPECS = [
    {
        "id": "toolkit_terminal_password_hash",
        "label": "Browser login password hash (SHA-512 crypt)",
        "input_type": "text",
        "placeholder": "Generate with: openssl passwd -6 -stdin",
        "required": True,
        "sensitive": True,
    },
    {
        "id": "toolkit_enable_funnel",
        "label": "Publish authenticated terminal to the internet via Funnel",
        "input_type": "checkbox",
        "default": False,
        "required": False,
    },
]
BOOT_INPUT_SPECS = INPUT_SPECS + [
    {"id": "toolkit_wifi_ssid", "label": "Wi-Fi uplink SSID", "input_type": "text", "required": True},
    {"id": "toolkit_wifi_password", "label": "Wi-Fi password", "input_type": "text", "required": True, "sensitive": True},
    {"id": "toolkit_wifi_country", "label": "Wi-Fi country (two letters)", "input_type": "text", "default": "US", "required": True},
]
EXTRA_PACKAGES_OPTION = "toolkit_extra_packages"
SECRET_OPTIONS = {"toolkit_terminal_password_hash", "toolkit_wifi_password"}
TEMPLATES = {
    "/etc/systemd/journald.conf.d/60-rack-toolkit.conf": ("journald.conf.j2", "0644"),
    "/etc/nginx/sites-enabled/network-toolkit": ("nginx.conf.j2", "0644"),
    "/etc/nginx/network-toolkit.htpasswd": ("htpasswd.j2", "0640"),
    "/etc/systemd/system/rack-toolkit-terminal.service": ("terminal.service.j2", "0644"),
    "/etc/systemd/system/rack-toolkit-funnel.service": ("funnel.service.j2", "0644"),
    "/etc/systemd/system/rack-toolkit-firewall.service": ("firewall.service.j2", "0644"),
    "/etc/rack-toolkit.nft": ("firewall.nft.j2", "0644"),
    "/etc/NetworkManager/system-connections/rack-device.nmconnection": ("device.nmconnection.j2", "0600"),
    "/etc/sudoers.d/network-toolkit": ("sudoers.j2", "0440"),
    "/etc/ssh/sshd_config.d/00-network-toolkit.conf": ("sshd.conf.j2", "0644"),
}


def extra_packages(options: dict[str, Any]) -> list[str]:
    return package_names(options.get(EXTRA_PACKAGES_OPTION, []), "Extra diagnostic tools")


def validate_options(options: dict[str, Any], *, boot: bool = False) -> list[str]:
    errors = []
    try:
        extra_packages(options)
    except ValueError as exc:
        errors.append(str(exc))
    password_hash = str(options.get("toolkit_terminal_password_hash") or "")
    if not re.fullmatch(r"\$6\$(?:rounds=[1-9][0-9]{3,8}\$)?[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}", password_hash):
        errors.append("Browser password hash must be SHA-512 crypt, generated with openssl passwd -6 -stdin.")
    funnel = options.get("toolkit_enable_funnel")
    if funnel is not None and str(funnel).lower() not in ("false", "true"):
        errors.append("toolkit_enable_funnel must be true or false.")
    if boot:
        ssid = str(options.get("toolkit_wifi_ssid") or "")
        if not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(c) < 32 for c in ssid):
            errors.append("Wi-Fi SSID must be 1-32 bytes without control characters.")
        password = str(options.get("toolkit_wifi_password") or "")
        if not (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)) and not re.fullmatch(r"[A-Fa-f0-9]{64}", password):
            errors.append("Wi-Fi password must be 8-63 printable ASCII characters or a 64-digit hex PSK.")
        if not re.fullmatch(r"[A-Z]{2}", str(options.get("toolkit_wifi_country") or "")):
            errors.append("Wi-Fi country must be two uppercase letters.")
    return errors


def render_files(options: dict[str, Any]) -> list[dict[str, str]]:
    errors = validate_options(options, boot=True)
    if errors:
        raise ValueError("; ".join(errors))
    env = Environment(loader=FileSystemLoader(ROLE_DIR / "templates"), undefined=StrictUndefined, keep_trailing_newline=True)
    files = []
    for path, (template, mode) in TEMPLATES.items():
        entry = {"path": path, "permissions": mode, "content": env.get_template(template).render(**options)}
        files.append(entry)
    for source in sorted((ROLE_DIR / "files").iterdir()):
        files.append({"path": f"/usr/local/bin/{source.name}", "permissions": "0755", "content": source.read_text()})
    files.append({
        "path": "/etc/default/crda", "permissions": "0644",
        "content": f"REGDOMAIN={options['toolkit_wifi_country']}\n",
    })
    return files


def network_config(options: dict[str, Any]) -> dict[str, Any]:
    errors = validate_options(options, boot=True)
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "version": 2,
        "renderer": "NetworkManager",
        "wifis": {"wlan0": {
            "dhcp4": True, "optional": True,
            "regulatory-domain": options["toolkit_wifi_country"],
            "access-points": {options["toolkit_wifi_ssid"]: {"password": options["toolkit_wifi_password"]}},
        }},
        "ethernets": {"eth0": {"dhcp4": False, "dhcp6": False, "optional": True, "link-local": ["ipv4"]}},
    }


def main() -> None:
    options = {spec["id"]: os.environ.get(f"CLOUD_INIT_{spec['id'].upper()}", spec.get("default", "")) for spec in BOOT_INPUT_SPECS}
    options[EXTRA_PACKAGES_OPTION] = os.environ.get("CLOUD_INIT_TOOLKIT_EXTRA_PACKAGES", "")
    errors = validate_options(options, boot=True)
    if errors:
        raise SystemExit("network-toolkit: " + "; ".join(errors))
    action = sys.argv[1]
    if action == "packages":
        packages = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text())["toolkit_packages"]
        print(" ".join(dict.fromkeys(packages + extra_packages(options))))
    elif action == "files":
        text = yaml.safe_dump(render_files(options), sort_keys=False, allow_unicode=False)
        print("\n".join("  " + line for line in text.splitlines()))
    elif action == "network":
        print(yaml.safe_dump(network_config(options), sort_keys=False))
    elif action == "ttyd":
        defaults = yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text())
        print(defaults["toolkit_ttyd_url"], defaults["toolkit_ttyd_sha256"])
    else:
        raise SystemExit(f"Unknown network-toolkit render action: {action}")


if __name__ == "__main__":
    main()
