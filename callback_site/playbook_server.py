"""Self-hosting Ubuntu Server node settings shared by imaging, CI image builds and Node Ops."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROFILE = "playbook-server"
GROUP = "playbook_server_active"
ROLE_DIR = Path(__file__).resolve().parents[1] / "roles" / "playbook_server"
ARCHES = ("amd64", "arm64")
INPUT_SPECS = [
    {
        "id": "server_repo_url",
        "label": "Repository URL",
        "input_type": "text",
        "default": "https://github.com/MeVsTheVoices/Playbook.git",
        "placeholder": "https://github.com/OWNER/Playbook.git",
        "required": True,
    },
    {
        "id": "server_ref",
        "label": "Branch, tag or commit",
        "input_type": "text",
        "default": "main",
        "placeholder": "main",
        "required": True,
    },
    {
        "id": "server_port",
        "label": "Dashboard port",
        "input_type": "text",
        "default": "8088",
        "placeholder": "8088",
        "required": True,
    },
    {
        "id": "server_install_flasher",
        "label": "Install the host SD/USB flasher",
        "input_type": "checkbox",
        "default": True,
        "required": False,
    },
]
BOOT_INPUT_SPECS = INPUT_SPECS
TEMPLATES = {"/etc/playbook-server.env": ("playbook-server.env.j2", "0644")}
_REPO_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/%+-]+")
_REF_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,127}")


def _defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE_DIR / "defaults" / "main.yml").read_text(encoding="utf-8"))


def normalize_options(options: dict[str, Any]) -> dict[str, Any]:
    """Fill blanks with the role defaults so imaging and Ansible deploy the same thing."""
    defaults = _defaults()
    out = {}
    for spec in INPUT_SPECS:
        key = spec["id"]
        value = options.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = defaults[key]
        out[key] = value.strip() if isinstance(value, str) else value
    out["server_install_flasher"] = str(out["server_install_flasher"]).strip().lower()
    out["server_port"] = str(out["server_port"]).strip()
    out["server_dir"] = defaults["server_dir"]
    return out


def validate_options(options: dict[str, Any], *, boot: bool = False) -> list[str]:
    values = normalize_options(options)
    errors = []
    if not _REPO_RE.fullmatch(values["server_repo_url"]):
        errors.append("Repository URL must be an http(s) git URL without spaces, quotes or shell characters.")
    ref = values["server_ref"]
    if not _REF_RE.fullmatch(ref) or ".." in ref or ref.endswith((".lock", "/")):
        errors.append("Ref must be a branch, tag or commit (letters, digits, '.', '_', '-', '/').")
    port = values["server_port"]
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        errors.append("Dashboard port must be 1-65535.")
    if values["server_install_flasher"] not in ("true", "false"):
        errors.append("server_install_flasher must be true or false.")
    return errors


def image(arch: str) -> tuple[str, str]:
    if arch not in ARCHES:
        raise ValueError(f"Unsupported architecture {arch!r}; use one of {', '.join(ARCHES)}.")
    entry = _defaults()["server_images"][arch]
    return entry["url"], entry["sha256"]


def packages() -> list[str]:
    return list(_defaults()["server_packages"])


def render_files(options: dict[str, Any]) -> list[dict[str, str]]:
    errors = validate_options(options, boot=True)
    if errors:
        raise ValueError("; ".join(errors))
    values = normalize_options(options)
    env = Environment(loader=FileSystemLoader(ROLE_DIR / "templates"), undefined=StrictUndefined, keep_trailing_newline=True)
    files = [
        {"path": path, "permissions": mode, "content": env.get_template(template).render(**values)}
        for path, (template, mode) in TEMPLATES.items()
    ]
    for source in sorted((ROLE_DIR / "files").iterdir()):
        files.append({"path": f"/usr/local/bin/{source.name}", "permissions": "0755", "content": source.read_text(encoding="utf-8")})
    return files


def main() -> None:
    options = {spec["id"]: os.environ.get(f"CLOUD_INIT_{spec['id'].upper()}", "") for spec in INPUT_SPECS}
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "image":
        try:
            print(*image(sys.argv[2] if len(sys.argv) > 2 else "amd64"))
        except ValueError as exc:
            raise SystemExit(f"playbook-server: {exc}")
        return
    errors = validate_options(options, boot=True)
    if errors:
        raise SystemExit("playbook-server: " + "; ".join(errors))
    if action == "packages":
        print(" ".join(packages()))
    elif action == "files":
        text = yaml.safe_dump(render_files(options), sort_keys=False, allow_unicode=False)
        print("\n".join("  " + line for line in text.splitlines()))
    elif action == "port":
        print(normalize_options(options)["server_port"])
    elif action == "settings":
        print(json.dumps(normalize_options(options), sort_keys=True))
    else:
        raise SystemExit(f"Unknown playbook-server render action: {action}")


if __name__ == "__main__":
    main()
