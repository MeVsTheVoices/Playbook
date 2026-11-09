"""Builds the Tailscale policy for the rack. Pure functions, no network calls.

The Tailscale page posts its inputs here and shows the result. Doing it in
Python (not in the browser) keeps the rack facts in one place and testable:
board tags come from the BOARDS registry in k3s_lightweight.py, and the K3s
ports come from the K3s networking docs.

Facts this encodes (Tailscale policy syntax and K3s requirements docs):
- Prefer ``grants`` (``src``, ``dst``, ``ip`` such as ``tcp:6443``) over legacy ``acls``; they can coexist.
- A tag must be defined in ``tagOwners`` before a device can authenticate with it,
  and an auth key must be created with matching tags.
- ``autoApprovers`` only act when a route or exit node is first advertised;
  re-advertise to approve one that already exists.
- Tailscale SSH needs a network rule to port 22 AND an ``ssh`` rule. The ``ssh``
  ``dst`` must be a tag, a group or self (never ``*``), and granting ``root``
  is broader than the default ``autogroup:nonroot``.
- ``tests`` and ``sshTests`` reject the whole policy when an assertion fails.
  Their sources must be a user, group, tag or host (no autogroups, no CIDR, no
  wildcards), so deny assertions are opt-in: a leftover allow-all grant would fail them.
- K3s inbound: tcp 6443 (nodes to a server), tcp 10250 (kubelet), udp 8472
  (flannel VXLAN), and tcp 2379-2380 only between servers (embedded etcd).
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from k3s_lightweight import BOARDS

HOMELAB_TAG = "tag:homelab"
DEFAULT_ADMIN = "group:admins"
DEFAULT_SUBNETS = ("192.168.174.0/24", "192.168.175.0/24")
# The callback site listens here; Pis that report over Tailscale need to reach the NucBox on it.
DEFAULT_EXTRA_PORTS = ("tcp:8088",)

TRAFFIC_K3S = "k3s"
TRAFFIC_ALL = "all"

K3S_PORTS: Tuple[Tuple[str, str], ...] = (
    ("tcp:6443", "Kubernetes API (nodes to a server)"),
    ("tcp:10250", "kubelet (logs, exec, metrics)"),
    ("udp:8472", "flannel VXLAN overlay"),
)
K3S_ETCD_PORTS = ("tcp:2379-2380", "etcd between servers")

_TAG_RE = re.compile(r"^tag:[A-Za-z][A-Za-z0-9-]*$")
_GROUP_RE = re.compile(r"^group:[A-Za-z0-9._@-]+$")
_AUTOGROUP_ADMIN_RE = re.compile(r"^autogroup:(admin|member|owner|network-admin|it-admin)$")
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+$")
_PORT_RE = re.compile(r"^(tcp|udp):(\d{1,5})(?:-(\d{1,5}))?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_TRUE = {"true", "1", "yes", "on"}
_DENY_PROBE_PORTS = (23, 3389, 5900)


def board_tags() -> Dict[str, str]:
    return {profile: board["tailscale_tag"] for profile, board in BOARDS.items()}


def board_info() -> List[Dict[str, str]]:
    return [
        {"profile": profile, "label": board["label"], "tag": board["tailscale_tag"], "group": board["group"]}
        for profile, board in BOARDS.items()
    ]


def policy_defaults() -> Dict[str, Any]:
    return {
        "tailnet": "tail00c2ce.ts.net",
        "admin_group": DEFAULT_ADMIN,
        "homelab_tag": HOMELAB_TAG,
        "subnets": list(DEFAULT_SUBNETS),
        "extra_ports": list(DEFAULT_EXTRA_PORTS),
        "board_tags": board_tags(),
    }


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


def _items(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[\s,]+", str(value))
    return [part.strip() for part in parts if part.strip()]


def _dedupe(values: List[str]) -> List[str]:
    return list(dict.fromkeys(values))


def _valid_principal(value: str) -> bool:
    return bool(
        _TAG_RE.match(value) or _GROUP_RE.match(value) or _AUTOGROUP_ADMIN_RE.match(value) or _EMAIL_RE.match(value)
    )


def _normalize_port(value: str) -> Optional[str]:
    text = value.strip().lower()
    if text == "icmp:*":
        return text
    match = _PORT_RE.match(text)
    if not match:
        return None
    low = int(match.group(2))
    high = int(match.group(3)) if match.group(3) else low
    if not (1 <= low <= 65535 and 1 <= high <= 65535 and low <= high):
        return None
    return text


def _port_covers(granted: List[str], proto: str, port: int) -> bool:
    for entry in granted:
        match = _PORT_RE.match(entry)
        if not match or match.group(1) != proto:
            continue
        low = int(match.group(2))
        high = int(match.group(3)) if match.group(3) else low
        if low <= port <= high:
            return True
    return False


def _ordered(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Sections in the order people read a policy (the API JSON encoder would sort them alphabetically)."""
    order = ("groups", "tagOwners", "nodeAttrs", "grants", "ssh", "autoApprovers", "tests", "sshTests")
    return {key: policy[key] for key in order if key in policy}


def _format_policy(value: Any, level: int = 0, path: Tuple[str, ...] = ()) -> str:
    if isinstance(value, dict) and value:
        entries = [
            f"{'  ' * (level + 1)}{json.dumps(key)}: {_format_policy(item, level + 1, path + (key,))}"
            for key, item in value.items()
        ]
        return "{\n" + ",\n".join(entries) + "\n" + "  " * level + "}"
    if path[:2] == ("autoApprovers", "routes") and isinstance(value, list):
        entries = ", ".join(json.dumps(item) for item in value)
        return "[\n" + "  " * (level + 1) + entries + "\n" + "  " * level + "]"
    if isinstance(value, list) and any(isinstance(item, (dict, list)) for item in value):
        entries = [
            "  " * (level + 1) + _format_policy(item, level + 1, path + (str(index),))
            for index, item in enumerate(value)
        ]
        return "[\n" + ",\n".join(entries) + "\n" + "  " * level + "]"
    return json.dumps(value)


def build_policy(options: Mapping[str, Any]) -> Dict[str, Any]:
    """Return {"ok", "policy", "rules", "warnings", "errors", "summary"} for the given inputs."""
    errors: List[str] = []
    warnings: List[Dict[str, str]] = []

    def warn(text: str, level: str = "warn") -> None:
        warnings.append({"level": level, "text": text})

    admin = str(options.get("admin") or "").strip()
    if not admin:
        errors.append("Admin principal is required (a user email, group:name, autogroup:admin, or a tag).")
    elif not _valid_principal(admin):
        errors.append(f"Admin '{admin}' is not a user email, group:name, tag, or autogroup:admin/member/owner/network-admin/it-admin.")

    members = _items(options.get("admin_members"))
    for member in members:
        if not _EMAIL_RE.match(member):
            errors.append(f"Admin member '{member}' is not a login email address.")

    homelab = str(options.get("homelab_tag") or "").strip()
    if not _TAG_RE.match(homelab):
        errors.append("Homelab tag must look like tag:name (letters, digits and hyphens, starting with a letter).")

    raw_board_tags = options.get("board_tags") if isinstance(options.get("board_tags"), Mapping) else {}
    used_board_tags: Dict[str, str] = {}
    for profile, board in BOARDS.items():
        raw = raw_board_tags.get(profile)
        tag = (board["tailscale_tag"] if raw is None else str(raw)).strip()
        if not tag:
            continue  # a blank tag means "this board is not in the rack"
        if not _TAG_RE.match(tag):
            errors.append(f"{board['label']} tag '{tag}' must look like tag:name.")
            continue
        used_board_tags[profile] = tag

    traffic = str(options.get("traffic") or TRAFFIC_K3S).strip().lower()
    if traffic not in (TRAFFIC_K3S, TRAFFIC_ALL):
        errors.append("Rack traffic must be 'k3s' (K3s ports only) or 'all'.")
    pi_servers = _bool(options.get("pi_servers"), False)

    extra_ports: List[str] = []
    for raw_port in _items(options.get("extra_ports")):
        port = _normalize_port(raw_port)
        if port is None:
            errors.append(f"Extra port '{raw_port}' must look like tcp:8088, udp:5000-5010 or icmp:*.")
        else:
            extra_ports.append(port)

    subnets: List[str] = []
    for raw_subnet in _items(options.get("subnets")):
        try:
            subnets.append(str(ipaddress.ip_network(raw_subnet, strict=False)))
        except ValueError:
            errors.append(f"Subnet '{raw_subnet}' is not a valid CIDR range.")
    subnets = _dedupe(subnets)

    subnet_access = _bool(options.get("subnet_access"), False)
    exit_node = _bool(options.get("exit_node"), False)
    ssh_enabled = _bool(options.get("ssh"), True)
    ssh_root = _bool(options.get("ssh_root"), False)
    ssh_user = str(options.get("ssh_user") or "").strip()
    if ssh_user and not _USER_RE.match(ssh_user):
        errors.append(f"SSH user '{ssh_user}' is not a valid Linux user name.")
    if ssh_user == "root":
        errors.append("The SSH test user must not be root; root login is controlled by the 'allow root' option.")
    include_tests = _bool(options.get("tests"), True)
    deny_tests = _bool(options.get("deny_tests"), False)
    toolkit_funnel = _bool(options.get("toolkit_funnel"), False)
    if toolkit_funnel and ssh_user == "network-console":
        errors.append("Use a separate administrative SSH user, not the browser's network-console account.")
    if toolkit_funnel and "tag:network-toolkit" in (*used_board_tags.values(), homelab):
        errors.append("tag:network-toolkit must remain separate from the board and homelab tags.")

    if errors:
        return {"ok": False, "policy": None, "rules": [], "warnings": [], "errors": errors, "summary": {}}

    rack_tags = _dedupe(list(used_board_tags.values()) + [homelab])
    policy: Dict[str, Any] = {}
    rules: List[Dict[str, Any]] = []

    if members and _GROUP_RE.match(admin):
        policy["groups"] = {admin: members}
    elif members:
        warn("Admin members are only used when the admin is a group:name; they were ignored.")
    elif _GROUP_RE.match(admin):
        warn(
            f"{admin} must exist under 'groups' in your policy or Tailscale will reject it. "
            "Add the admin login emails below to have it emitted here, or define it in the admin console.",
        )

    policy["tagOwners"] = {tag: [admin] for tag in rack_tags}

    grants: List[Dict[str, Any]] = []
    if toolkit_funnel:
        policy["tagOwners"]["tag:network-toolkit"] = [admin]
        policy["nodeAttrs"] = [{"target": ["tag:network-toolkit"], "attr": ["funnel"]}]
        grants.append({"src": [admin], "dst": ["tag:network-toolkit"], "ip": ["tcp:22"]})
        rules.append({
            "name": "Network Toolkit private SSH",
            "src": [admin], "dst": ["tag:network-toolkit"], "ports": ["tcp:22"],
            "why": "Dedicated Pi 3B+ appliance; no rack-to-rack or subnet access. Funnel HTTPS still requires its browser login.",
        })
        warn("Network Toolkit Funnel publishes a service to the internet. Enable it on the node only after setting a strong browser password. MagicDNS and HTTPS must also be enabled.", "info")
    grants.append({"src": [admin], "dst": rack_tags + subnets, "ip": ["*"]})
    rules.append({
        "name": "Admin access",
        "src": [admin],
        "dst": rack_tags + subnets,
        "ports": ["*"],
        "why": "You reach every rack node (SSH, kubectl, dashboards)" + (" and the advertised subnets." if subnets else "."),
    })

    if traffic == TRAFFIC_ALL:
        rack_ports = ["*"]
        warn("Every port is open between rack nodes. 'K3s ports only' is tighter and is enough for a K3s rack.")
        why = "Rack nodes talk to each other on any port."
    else:
        rack_ports = [port for port, _ in K3S_PORTS]
        why = "K3s over Tailscale: " + "; ".join(f"{port} {label}" for port, label in K3S_PORTS)
        if pi_servers:
            rack_ports.append(K3S_ETCD_PORTS[0])
            why += f"; {K3S_ETCD_PORTS[0]} {K3S_ETCD_PORTS[1]}"
            warn(
                "etcd ports are open between every rack node because a tag cannot tell a K3s server from a worker. "
                "Keep the number of K3s servers odd (the NucBox plus two Pi servers makes three).",
                "info",
            )
        rack_ports += extra_ports
        if extra_ports:
            why += "; extra: " + ", ".join(extra_ports)
        if "icmp:*" not in rack_ports:
            rack_ports.append("icmp:*")
        rack_ports = _dedupe(rack_ports)
    grants.append({"src": rack_tags, "dst": rack_tags, "ip": rack_ports})
    rules.append({"name": "Rack to rack", "src": rack_tags, "dst": rack_tags, "ports": rack_ports, "why": why})

    if subnet_access and subnets:
        grants.append({"src": rack_tags, "dst": subnets, "ip": ["*"]})
        rules.append({
            "name": "Rack to subnets", "src": rack_tags, "dst": subnets, "ports": ["*"],
            "why": "Rack nodes may use the advertised subnet routes.",
        })
    elif subnet_access and not subnets:
        warn("'Rack nodes may reach subnets' has no effect: no subnets are listed.", "info")

    if exit_node:
        grants.append({"src": [admin], "dst": ["autogroup:internet"], "ip": ["*"]})
        rules.append({
            "name": "Exit node use", "src": [admin], "dst": ["autogroup:internet"], "ports": ["*"],
            "why": "You may send internet traffic through an exit node.",
        })
    policy["grants"] = grants

    if ssh_enabled:
        users = ["autogroup:nonroot"] + (["root"] if ssh_root else [])
        policy["ssh"] = [{"action": "accept", "src": [admin], "dst": rack_tags, "users": users}]
        if toolkit_funnel and ssh_user:
            policy["ssh"].append({"action": "accept", "src": [admin], "dst": ["tag:network-toolkit"], "users": [ssh_user]})
        elif toolkit_funnel:
            warn("Set the SSH Test User to the toolkit's administrative login before enabling Tailscale SSH on it. The browser's network-console user is intentionally not granted Tailscale SSH.", "info")
        rules.append({
            "name": "Tailscale SSH", "src": [admin], "dst": rack_tags, "ports": ["ssh: " + ", ".join(users)],
            "why": "Works only on nodes running tailscale up --ssh (Node Ops Tailscale module or the SD form option).",
        })
        if ssh_root:
            warn("Root login over Tailscale SSH is broader than the default (autogroup:nonroot).")

    if subnets or exit_node:
        approvers: Dict[str, Any] = {}
        if subnets:
            approvers["routes"] = {cidr: list(rack_tags) for cidr in subnets}
        if exit_node:
            approvers["exitNode"] = list(rack_tags)
        policy["autoApprovers"] = approvers
        warn(
            "Auto-approval only applies when a route or exit node is first advertised. For one that is already "
            "advertised, stop advertising it on the node and advertise it again.",
            "info",
        )
        warn("Routes and exit nodes are approved for every rack tag; narrow this to your subnet router if you have one.", "info")

    admin_testable = not admin.startswith("autogroup:")
    test_count = 0
    if include_tests:
        tests: List[Dict[str, Any]] = []
        if admin_testable:
            tests.append({"src": admin, "proto": "tcp", "accept": [f"{tag}:22" for tag in rack_tags]})
        else:
            warn(f"Tests with {admin} as the source were skipped: Tailscale tests need a user, group or tag source.", "info")
        tests.append({"src": homelab, "proto": "tcp", "accept": [f"{homelab}:6443", f"{homelab}:10250"]})
        tests.append({"src": homelab, "proto": "udp", "accept": [f"{homelab}:8472"]})
        for tag in _dedupe(list(used_board_tags.values())):
            if tag != homelab:
                tests.append({"src": tag, "proto": "tcp", "accept": [f"{homelab}:6443"]})
        if traffic == TRAFFIC_K3S:
            tests.append({"src": homelab, "proto": "icmp", "accept": [f"{homelab}:0"]})
        if deny_tests and traffic == TRAFFIC_K3S:
            probe = next((p for p in _DENY_PROBE_PORTS if not _port_covers(rack_ports, "tcp", p)), None)
            if probe:
                tests.append({"src": homelab, "proto": "tcp", "deny": [f"{homelab}:{probe}"]})
        policy["tests"] = tests
        test_count += len(tests)
        if deny_tests:
            warn(
                "Deny tests fail if any other rule (for example Tailscale's default allow-all grant) still opens that port. "
                "Remove the default grant first.",
                "info",
            )

        if ssh_enabled and admin_testable:
            ssh_test: Dict[str, Any] = {"src": admin, "dst": list(rack_tags)}
            if ssh_user:
                ssh_test["accept"] = [ssh_user]
            if deny_tests and not ssh_root:
                ssh_test["deny"] = ["root"]
            if "accept" in ssh_test or "deny" in ssh_test:
                policy["sshTests"] = [ssh_test]
                test_count += 1

    warn(
        "Tailscale's default policy ships an allow-all grant (* to *). These rules only restrict anything after that "
        "grant is removed, so keep it until the tests pass, then delete it.",
        "info",
    )
    warn(
        "Define the tags here before creating auth keys: a key must be created with the same tags the node advertises "
        f"({', '.join(rack_tags)}).",
        "info",
    )

    summary = {
        "tags": list(policy["tagOwners"]),
        "grants": len(grants),
        "routes": len(subnets),
        "tests": test_count,
        "ssh": bool(ssh_enabled),
    }
    return {
        "ok": True, "policy": policy, "policy_text": _format_policy(_ordered(policy)),
        "rules": rules, "warnings": warnings, "errors": [], "summary": summary,
    }
