"""Package-name validation shared by imaging profiles and Node Ops."""

from __future__ import annotations

import re
from typing import Any


def package_names(raw: Any, label: str = "Extra packages", *, debian_only: bool = True) -> list[str]:
    kind = "Debian package names" if debian_only else "Linux package names"
    pattern = r"[a-z0-9][a-z0-9+.-]+" if debian_only else r"[A-Za-z0-9][A-Za-z0-9+_.:-]*"
    if isinstance(raw, str):
        items = re.split(r"[,\s]+", raw.strip()) if raw.strip() else []
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(f"{label} must be a list of {kind}.")
    packages = []
    for item in items:
        if not isinstance(item, str) or not re.fullmatch(pattern, item.strip()):
            raise ValueError(f"{label} must contain only {kind} (no versions, URLs or command options).")
        package = item.strip()
        if debian_only and package == "nvim":
            raise ValueError(f"{label}: use 'neovim', the Debian/Ubuntu package name, not 'nvim'.")
        if package not in packages:
            packages.append(package)
    return packages
