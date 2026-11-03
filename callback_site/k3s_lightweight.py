"""K3s roles for the Pi 5, Pi 4 and Pi 3B+, and the settings that go with each. BOARDS is the board registry.

Every board can be a worker, run no Kubernetes ("utility"), or be a K3s server
("control-plane"). Defaults differ because the boards do:
- Pi 5 (NVMe, plenty of RAM): default "worker"; as a control-plane it keeps every
  packaged component on, so it matches the NucBox server without extra flags.
- Pi 4 (1-8 GB, usually SD or USB): same shape as the Pi 5. As a control-plane it needs an SSD,
  because K3s documents that SD cards and eMMC cannot handle etcd's IO load.
- Pi 3B+ (about 900 MB RAM, SD card, below K3s's 2 GB server floor): default
  "light-worker" (a tainted K3s agent with a pod cap). As a control-plane it gets
  the lightweight server profile, and that role is not recommended.

Single source of truth for the option set. The SD-card form, the Node Ops
module specs, and the backend merge all read from here; the Ansible roles
mirror the same rules (tests keep them in agreement).

Facts this encodes (K3s docs, "Packaged Components" and "server" CLI):
- ``--disable`` actively UNINSTALLS the component from the running cluster.
- servicelb, disable-network-policy, disable-helm-controller and
  disable-cloud-controller must be identical on every server in the cluster.
  The other packaged components are kept identical too, so servers never
  fight over re-deploying manifests.
- Traefik is installed through the Helm controller, so the Helm controller can
  only be disabled when ingress is "none".
- ingress-nginx is deliberately not offered: the Kubernetes project retired it
  in March 2026 (no releases, no security fixes).
- etcd wants an odd number of servers: the NucBox plus two Pi 5 makes three.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

ROLE_OPTION_ID = "k3s_role"
ROLE_WORKER = "worker"
ROLE_LIGHT_WORKER = "light-worker"
ROLE_UTILITY = "utility"
ROLE_CONTROL_PLANE = "control-plane"

# "roles": choices on the SD form (utility there is simply the Standard distribution).
# "node_ops_roles": choices in Node Ops. "taint"/"max_pods": board defaults; blank max_pods keeps K3s's 110.
# "disable_defaults": whether the lightweight server options start switched on.
# "label"/"tailscale_tag": display name and the Tailscale tag the nodes advertise by default (SD script, Ansible group_vars).
BOARDS: Dict[str, Dict[str, Any]] = {
    "pi3bplus": {
        "label": "Pi 3B+",
        "tailscale_tag": "tag:rpi-3bplus",
        "group": "rpi_3bplus_active",
        "role": ROLE_LIGHT_WORKER,
        "roles": (ROLE_LIGHT_WORKER, ROLE_CONTROL_PLANE),
        "node_ops_roles": (ROLE_LIGHT_WORKER, ROLE_UTILITY, ROLE_CONTROL_PLANE),
        "taint": True,
        "max_pods": "20",
        "disable_defaults": True,
    },
    "pi5": {
        "label": "Pi 5",
        "tailscale_tag": "tag:rpi5",
        "group": "rpi5_active",
        "role": ROLE_WORKER,
        "roles": (ROLE_WORKER, ROLE_CONTROL_PLANE),
        "node_ops_roles": (ROLE_WORKER, ROLE_CONTROL_PLANE, ROLE_UTILITY),
        "taint": False,
        "max_pods": "",
        "disable_defaults": False,
    },
    # Pi 4 spans 1-8 GB RAM. It is a full worker or control-plane like the Pi 5, but K3s says etcd needs
    # an SSD (SD cards and eMMC cannot handle the IO), so control-plane on an SD-booted Pi 4 is a poor idea.
    "pi4": {
        "label": "Pi 4",
        "tailscale_tag": "tag:rpi4",
        "group": "rpi4_active",
        "role": ROLE_WORKER,
        "roles": (ROLE_WORKER, ROLE_CONTROL_PLANE),
        "node_ops_roles": (ROLE_WORKER, ROLE_CONTROL_PLANE, ROLE_UTILITY),
        "taint": False,
        "max_pods": "",
        "disable_defaults": False,
    },
}
ROLE_PROFILES = tuple(BOARDS)
GROUP_TO_PROFILE = {board["group"]: profile for profile, board in BOARDS.items()}
NODE_GROUPS = tuple(board["group"] for board in BOARDS.values())
DEFAULT_SERVER_PROFILE = "pi3bplus"


def group_for_profile(profile: Any, default: str = "") -> str:
    board = BOARDS.get(str(profile or "").strip().lower())
    return board["group"] if board else default


def profile_for_group(group: Any, default: str = "") -> str:
    """Match an inventory group (or any name containing the family) to a profile."""
    text = str(group or "").strip().lower()
    if text in GROUP_TO_PROFILE:
        return GROUP_TO_PROFILE[text]
    if "3bplus" in text or "3b+" in text:
        return "pi3bplus"
    if "rpi4" in text or "pi4" in text:
        return "pi4"
    if "rpi5" in text or "pi5" in text:
        return "pi5"
    return default


ROLE_LABELS = {
    ROLE_WORKER: "role=worker",
    ROLE_LIGHT_WORKER: "role=light-worker",
    ROLE_CONTROL_PLANE: "role=control-plane",
}
ROLE_TAINTS = {
    ROLE_WORKER: "rack/dedicated=true:NoSchedule",
    ROLE_LIGHT_WORKER: "rack/light=true:NoSchedule",
    ROLE_CONTROL_PLANE: "node-role.kubernetes.io/control-plane:NoSchedule",
}
MAX_PODS_LIMIT = 110

INGRESS_OPTION_ID = "k3s_ingress"
INGRESS_CHOICES = ("traefik", "none")
INGRESS_DEFAULT = "traefik"

# "disable": value appended to the K3s ``disable:`` list when the option is on.
# "flag": boolean K3s config key set to true when the option is on.
LIGHTWEIGHT_OPTIONS: List[Dict[str, Any]] = [
    {
        "id": "k3s_disable_servicelb",
        "label": "Disable ServiceLB (klipper-lb)",
        "hint": "No svclb pods; LoadBalancer Services stay <pending>. Must match on every server.",
        "disable": "servicelb",
        "default": True,
    },
    {
        "id": "k3s_disable_metrics_server",
        "label": "Disable metrics-server",
        "hint": "No kubectl top / HPA metrics.",
        "disable": "metrics-server",
        "default": True,
    },
    {
        "id": "k3s_disable_local_storage",
        "label": "Disable local-path storage provisioner",
        "hint": "No default StorageClass; PVCs need another provisioner.",
        "disable": "local-storage",
        "default": True,
    },
    {
        "id": "k3s_disable_network_policy",
        "label": "Disable network policy controller",
        "hint": "NetworkPolicy objects are no longer enforced. Must match on every server.",
        "flag": "disable-network-policy",
        "default": True,
    },
    {
        "id": "k3s_disable_cloud_controller",
        "label": "Disable K3s cloud controller",
        "hint": "Off by default. Must match on every server.",
        "flag": "disable-cloud-controller",
        "default": False,
    },
    {
        "id": "k3s_disable_helm_controller",
        "label": "Disable Helm controller",
        "hint": "Off by default. Only valid with ingress 'none' (Traefik is installed by Helm). Must match on every server.",
        "flag": "disable-helm-controller",
        "default": False,
    },
]

# Server-only options: shown and kept only for the control-plane role.
ALLOW_SD_ETCD_OPTION_ID = "k3s_allow_sd_etcd"
LIGHTWEIGHT_OPTION_IDS = tuple(opt["id"] for opt in LIGHTWEIGHT_OPTIONS) + (INGRESS_OPTION_ID, ALLOW_SD_ETCD_OPTION_ID)

# Optional pin for the K3s the SD first boot installs. Blank means "match the K3s on this machine".
K3S_VERSION_OPTION_ID = "k3s_version"
K3S_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+\+k3s\d+$")
K3S_VERSION_UNPINNED = "none"

TAINT_OPTION_ID = "k3s_taint"
MAX_PODS_OPTION_ID = "k3s_max_pods"
COMMON_OPTION_IDS = (TAINT_OPTION_ID, MAX_PODS_OPTION_ID)
ALL_ROLE_OPTION_IDS = LIGHTWEIGHT_OPTION_IDS + COMMON_OPTION_IDS + (ROLE_OPTION_ID,)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def is_role_profile(profile: Any) -> bool:
    return str(profile or "").strip().lower() in BOARDS


def _board(profile: Any) -> Dict[str, Any]:
    return BOARDS[str(profile or "").strip().lower()]


def _server_default(profile: str, opt: Dict[str, Any]) -> bool:
    return bool(opt["default"]) and bool(_board(profile)["disable_defaults"])


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def resolve_lightweight(options: Dict[str, Any] | None, profile: str = DEFAULT_SERVER_PROFILE) -> Dict[str, Any]:
    """Resolve option values (missing -> the board's default) into disable list, flags, errors."""
    options = options or {}
    ingress = str(options.get(INGRESS_OPTION_ID) or INGRESS_DEFAULT).strip().lower()
    errors: List[str] = []
    if ingress not in INGRESS_CHOICES:
        errors.append(f"Unsupported ingress '{ingress}'. Choose one of: {', '.join(INGRESS_CHOICES)}.")
        ingress = INGRESS_DEFAULT

    disable: List[str] = []
    flags: List[str] = []
    for opt in LIGHTWEIGHT_OPTIONS:
        if not _as_bool(options.get(opt["id"]), _server_default(profile, opt)):
            continue
        if "disable" in opt:
            disable.append(opt["disable"])
        else:
            flags.append(opt["flag"])
    if ingress == "none":
        disable.append("traefik")
    if "disable-helm-controller" in flags and ingress != "none":
        errors.append(
            "The Helm controller can only be disabled when ingress is 'none': "
            "Traefik is installed through it."
        )
    return {"ingress": ingress, "disable": disable, "flags": flags, "errors": errors}


def _present_keys(config_text: str) -> set:
    return {m.group(1).lower() for m in re.finditer(r"(?m)^([A-Za-z][A-Za-z0-9-]*)\s*:", config_text or "")}


def _server_lines(resolved: Dict[str, Any], present: set) -> List[str]:
    lines: List[str] = []
    if resolved["disable"] and "disable" not in present:
        lines.append("disable:")
        lines.extend(f"  - {name}" for name in resolved["disable"])
    for flag in resolved["flags"]:
        if flag not in present:
            lines.append(f"{flag}: true")
    return lines


def _append_lines(config_text: str, lines: List[str]) -> str:
    if not lines:
        return config_text
    return (config_text or "").rstrip("\n") + "\n" + "\n".join(lines) + "\n"


def apply_to_k3s_config(config_text: str, options: Dict[str, Any] | None, profile: str = DEFAULT_SERVER_PROFILE) -> Tuple[str, List[str]]:
    """Append only the lightweight server settings (disable list and flags) to a K3s config.

    Keys the user already wrote in the config win and are never duplicated. Returns (text, errors).
    """
    resolved = resolve_lightweight(options, profile)
    lines = _server_lines(resolved, _present_keys(config_text))
    return _append_lines(config_text, lines), resolved["errors"]


def strip_options(options: Dict[str, Any], ids: Tuple[str, ...] = ALL_ROLE_OPTION_IDS) -> None:
    """Remove option keys in place (used for profiles or roles that must not get them)."""
    for key in ids:
        options.pop(key, None)


def resolve_role(options: Dict[str, Any] | None, profile: str) -> Tuple[str, List[str]]:
    board = _board(profile)
    role = str((options or {}).get(ROLE_OPTION_ID) or board["role"]).strip().lower()
    if role not in board["roles"]:
        return board["role"], [f"Unsupported role '{role}' for {profile}. Choose one of: {', '.join(board['roles'])}."]
    return role, []


def k3s_mode_for_role(role: str) -> str:
    return "server" if role == ROLE_CONTROL_PLANE else "agent"


def resolve_k3s_version(options: Dict[str, Any] | None, detected: str = "") -> Tuple[str, List[str]]:
    """The K3s version to install: an explicit pin, the detected local version, or "" for the release channel.

    A node running a newer K3s than the servers is unsupported and a new server older than the cluster is risky,
    so the default is the version the existing server runs.
    """
    raw = str((options or {}).get(K3S_VERSION_OPTION_ID) or "").strip()
    if not raw:
        return detected, []
    if raw.lower() == K3S_VERSION_UNPINNED:
        return "", []
    if K3S_VERSION_RE.match(raw):
        return raw, []
    return detected, [f"K3s version '{raw}' must look like v1.36.4+k3s1 (or 'none' to follow the release channel)."]


def resolve_common(options: Dict[str, Any] | None, profile: str) -> Dict[str, Any]:
    """Taint and pod cap shared by every K3s role. Blank or missing means the board's default."""
    options = options or {}
    board = _board(profile)
    taint = _as_bool(options.get(TAINT_OPTION_ID), bool(board["taint"]))
    raw = options.get(MAX_PODS_OPTION_ID)
    raw_pods = str(raw).strip() if raw is not None else ""
    raw_pods = raw_pods or str(board["max_pods"])
    errors: List[str] = []
    max_pods = None
    if raw_pods:
        if raw_pods.isdigit() and 1 <= int(raw_pods) <= MAX_PODS_LIMIT:
            max_pods = int(raw_pods)
        else:
            errors.append(f"Max pods must be a whole number from 1 to {MAX_PODS_LIMIT}, or blank (got '{raw_pods}').")
    return {"taint": taint, "max_pods": max_pods, "errors": errors}


def apply_role_to_k3s_config(config_text: str, options: Dict[str, Any] | None, profile: str) -> Tuple[str, str, List[str]]:
    """Apply the selected role's settings. Returns (text, role, errors).

    Keys the user already wrote in the config win and are never duplicated.
    """
    role, errors = resolve_role(options, profile)
    if errors:
        return config_text, role, errors
    present = _present_keys(config_text)
    lines: List[str] = []
    if role == ROLE_CONTROL_PLANE:
        server = resolve_lightweight(options, profile)
        errors += server["errors"]
        lines += _server_lines(server, present)
    common = resolve_common(options, profile)
    errors += common["errors"]
    if "node-label" not in present:
        lines += ["node-label:", f"  - {ROLE_LABELS[role]}"]
    if common["taint"] and "node-taint" not in present:
        lines += ["node-taint:", f"  - {ROLE_TAINTS[role]}"]
    if common["max_pods"] and "kubelet-arg" not in present:
        lines += ["kubelet-arg:", f"  - max-pods={common['max_pods']}"]
    return _append_lines(config_text, lines), role, errors


def ui_defaults() -> Dict[str, Any]:
    """Per-board defaults for the Home page, which resets its controls when the board or role changes."""
    out: Dict[str, Any] = {}
    for profile, board in BOARDS.items():
        out[profile] = {
            "roles": list(board["roles"]),
            "role": board["role"],
            "taint": bool(board["taint"]),
            "max_pods": str(board["max_pods"]),
            "ingress": INGRESS_DEFAULT,
            "server": {opt["id"]: _server_default(profile, opt) for opt in LIGHTWEIGHT_OPTIONS},
        }
    return out


def node_role_spec_parts() -> Dict[str, Any]:
    """Per-board choices and defaults for the Node Ops `node_role` select."""
    return {
        "options_by_group": {board["group"]: list(board["node_ops_roles"]) for board in BOARDS.values()},
        "default_by_group": {board["group"]: board["role"] for board in BOARDS.values()},
    }


def _groups() -> List[str]:
    return [board["group"] for board in BOARDS.values()]


def _role_specs() -> List[Dict[str, Any]]:
    k3s_roles = [ROLE_WORKER, ROLE_LIGHT_WORKER, ROLE_CONTROL_PLANE]
    return [
        {
            "id": TAINT_OPTION_ID,
            "label": "Keep general workloads off this node (taint)",
            "input_type": "checkbox",
            "default": "false",
            "default_by_group": {b["group"]: "true" if b["taint"] else "false" for b in BOARDS.values()},
            "hint": (
                f"Adds {ROLE_TAINTS[ROLE_WORKER]} on a worker, {ROLE_TAINTS[ROLE_LIGHT_WORKER]} on a light worker, "
                f"or {ROLE_TAINTS[ROLE_CONTROL_PLANE]} on a control-plane node; only pods that tolerate it are scheduled here."
            ),
            "only_groups": _groups(),
            "only_roles": k3s_roles,
        },
        {
            "id": MAX_PODS_OPTION_ID,
            "label": "Max pods on this node",
            "input_type": "text",
            "default": "",
            "default_by_group": {b["group"]: str(b["max_pods"]) for b in BOARDS.values()},
            "placeholder": "blank = board default",
            "hint": f"Whole number from 1 to {MAX_PODS_LIMIT}. Blank uses the board default: 20 on a Pi 3B+ (it cannot carry 110), the K3s default of 110 on a Pi 5 or Pi 4.",
            "only_groups": _groups(),
            "only_roles": k3s_roles,
        },
    ]


def node_ops_specs() -> List[Dict[str, Any]]:
    """Node Ops module input specs for both boards; each is shown only for the roles it applies to."""
    specs: List[Dict[str, Any]] = _role_specs()
    for opt in LIGHTWEIGHT_OPTIONS:
        specs.append(
            {
                "id": opt["id"],
                "label": opt["label"],
                "input_type": "checkbox",
                "default": "false",
                "default_by_group": {
                    board["group"]: "true" if _server_default(profile, opt) else "false"
                    for profile, board in BOARDS.items()
                },
                "hint": opt["hint"],
                "only_groups": _groups(),
                "only_roles": [ROLE_CONTROL_PLANE],
            }
        )
    specs.append(
        {
            "id": ALLOW_SD_ETCD_OPTION_ID,
            "label": "Allow etcd on an SD card (not recommended)",
            "input_type": "checkbox",
            "default": "false",
            "hint": "K3s says SD cards and eMMC cannot handle etcd's write load, and an unhealthy voter can break quorum. "
            "A control-plane node refuses to apply while /var/lib is on an SD card unless this is ticked.",
            "only_groups": _groups(),
            "only_roles": [ROLE_CONTROL_PLANE],
        }
    )
    specs.append(
        {
            "id": INGRESS_OPTION_ID,
            "label": "Ingress controller",
            "input_type": "select",
            "options": list(INGRESS_CHOICES),
            "default": INGRESS_DEFAULT,
            "hint": "'none' also disables Traefik. ingress-nginx is retired and not offered.",
            "only_groups": _groups(),
            "only_roles": [ROLE_CONTROL_PLANE],
        }
    )
    return specs
