"""Debian package-name validation shared by imaging profiles."""

from __future__ import annotations

import re
from typing import Any


def package_names(raw: Any, label: str = "Extra packages") -> list[str]:
    if isinstance(raw, str):
        items = re.split(r"[,\s]+", raw.strip()) if raw.strip() else []
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(f"{label} must be a list of Debian package names.")
    packages = []
    for item in items:
        if not isinstance(item, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", item.strip()):
            raise ValueError(f"{label} must contain only Debian package names (no versions, URLs or command options).")
        package = item.strip()
        if package == "nvim":
            raise ValueError(f"{label}: use 'neovim', the Debian/Ubuntu package name, not 'nvim'.")
        if package not in packages:
            packages.append(package)
    return packages
