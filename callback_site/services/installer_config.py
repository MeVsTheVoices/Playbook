"""Shared validation for explicitly authorized unattended UEFI installer media."""

from __future__ import annotations

import re
from typing import Any

from domain_defaults import IMAGE_CATALOG

UEFI_PROFILES = ("uefi-x86_64", "uefi-arm64")


def is_installer(payload: dict[str, Any]) -> bool:
    return payload.get("workflow") == "uefi-installer" or payload.get("profile") in UEFI_PROFILES


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("workflow") != "uefi-installer":
        raise ValueError("UEFI profiles require workflow=uefi-installer.")
    if payload.get("install_authorized") is not True:
        raise ValueError("Explicit authorization to erase the target machine's sole eligible disk is required at USB creation.")
    entry = next((entry for entry in IMAGE_CATALOG if entry["id"] == payload.get("catalog_id")), None)
    if not entry or entry.get("workflow") != "uefi-installer" or payload.get("profile") not in entry["profiles"]:
        raise ValueError("Select an architecture-compatible unattended installer catalog entry.")
    if payload.get("distribution", "standard") != "standard":
        raise ValueError("UEFI installation currently provisions a baseline OS, not the Pi/K3s workflow.")
    if payload.get("image_file") or payload.get("workspace"):
        raise ValueError("UEFI mode accepts the pinned catalog ISO, not local image or workspace overrides.")
    for field in ("image_url", "image_sha256"):
        if payload.get(field) and payload[field] != entry[field]:
            raise ValueError("UEFI media requires the pinned catalog URL and checksum.")
    options = payload.get("cloud_init_options") or {}
    if not isinstance(options, dict):
        raise ValueError("cloud_init_options must be an object.")
    unsupported = [key for key, value in options.items()
                   if key not in {"username", "timezone", "locale"} and value not in (False, None, "", [], {})]
    if unsupported:
        raise ValueError("UEFI baseline does not support Pi-specific options: " + ", ".join(sorted(unsupported)))
    username = options.get("username") or "rackadmin"
    if not isinstance(username, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", username) or username in {"root", "nobody"}:
        raise ValueError("Select a non-root administrative username.")
    timezone = options.get("timezone") or "UTC"
    if not isinstance(timezone, str) or not re.fullmatch(r"[A-Za-z0-9_+/-]+", timezone) or ".." in timezone:
        raise ValueError("Invalid timezone.")
    locale = options.get("locale") or "en_US.UTF-8"
    if not isinstance(locale, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+", locale):
        raise ValueError("Invalid locale.")
    payload.update(image_url=entry["image_url"], image_sha256=entry["image_sha256"],
                   cloud_init_options={"username": username, "timezone": timezone, "locale": locale})
    return entry
