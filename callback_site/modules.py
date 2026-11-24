"""Optional portal modules, switched on with PLAYBOOK_MODULES (comma separated).

The core portal (callbacks, imaging, Node Ops, monitor, terminal, settings) always runs.
"""

from __future__ import annotations

import os

OPTIONAL_MODULES = {
    "kubernetes": "Cluster page, Helm configurations, Tailscale operator and RBAC",
}


def enabled_modules() -> frozenset[str]:
    requested = {name.strip().lower() for name in os.getenv("PLAYBOOK_MODULES", "").split(",") if name.strip()}
    unknown = requested - OPTIONAL_MODULES.keys()
    if unknown:
        raise RuntimeError(f"Unknown PLAYBOOK_MODULES entries: {', '.join(sorted(unknown))}. Known: {', '.join(OPTIONAL_MODULES)}.")
    return frozenset(requested)
