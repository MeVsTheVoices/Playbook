"""Read a module's current settings straight off a node over SSH, so Node Ops can show what is really there."""

from __future__ import annotations

import base64
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict

from services.network import _preferred_node_connect_target
from services.node_inventory import _node_ops_inventory_view
from services.node_state import _load_node_config, _profile_map
from services.ssh_keys import (
    _ansible_known_hosts_args,
    _is_safe_key_name,
    _seed_known_host,
    _ssh_private_key_preflight,
)
from services.storage import KEYS_DIR

logger = logging.getLogger(__name__)

SSH_TIMEOUT_SECONDS = 25
MAX_VALUE_CHARS = 500
TRUE_WORDS = {"1", "true", "yes", "on"}

# Runs on the node and only reads. Each module prints `field=value` lines named after its input ids.
# Secrets (auth keys, tokens) are never read: values are also filtered against the module's non-sensitive inputs.
_PREAMBLE = r'''
S=""
if [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null; then S="sudo -n"; fi
emit() { if [ -n "$2" ]; then printf '%s=%s\n' "$1" "$(printf '%s' "$2" | tr '\n\r' '  ')"; fi; return 0; }
yesno() { if "$@" >/dev/null 2>&1; then echo true; else echo false; fi; }
'''

_READERS: Dict[str, str] = {
    "baseline": r'''
emit timezone "$(timedatectl show -p Timezone --value 2>/dev/null)"
emit ntp_servers "$(cat /etc/systemd/timesyncd.conf /etc/systemd/timesyncd.conf.d/*.conf 2>/dev/null | sed -n 's/^NTP=//p' | tail -n1 | tr -s ' ' ',')"
emit enable_fail2ban "$(yesno systemctl is-enabled --quiet fail2ban)"
if dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null | grep -q 'install ok installed'; then
  emit enable_unattended_upgrades true
else
  emit enable_unattended_upgrades false
fi
''',
    "tailscale": r'''
if command -v tailscale >/dev/null 2>&1; then
  PREFS="$($S tailscale debug prefs 2>/dev/null)"
  STATUS="$(tailscale status --json --peers=false 2>/dev/null || tailscale status --json 2>/dev/null)"
  export PREFS STATUS
  python3 -c '
import json, os
def load(name):
    try:
        return json.loads(os.environ.get(name) or "{}")
    except ValueError:
        return {}
prefs, status = load("PREFS"), load("STATUS")
def out(key, value):
    if value not in (None, ""):
        print("%s=%s" % (key, value))
out("hostname", prefs.get("Hostname") or (status.get("Self") or {}).get("HostName"))
out("advertise_tags", ",".join(prefs.get("AdvertiseTags") or []))
if prefs:
    out("advertise_exit_node", "true" if "0.0.0.0/0" in (prefs.get("AdvertiseRoutes") or []) else "false")
    out("enable_ssh", "true" if prefs.get("RunSSH") else "false")
out("tailnet", (status.get("CurrentTailnet") or {}).get("MagicDNSSuffix"))
' 2>/dev/null
  emit node_ip "$(tailscale ip -4 2>/dev/null | head -n1)"
fi
''',
    "docker": r'''
if command -v docker >/dev/null 2>&1; then
  emit docker_data_root "$($S docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
fi
DJ="$($S cat /etc/docker/daemon.json 2>/dev/null)"
if [ -n "$DJ" ]; then
  export DJ
  python3 -c '
import json, os
try:
    mirrors = json.loads(os.environ["DJ"]).get("registry-mirrors") or []
except ValueError:
    mirrors = []
if mirrors:
    print("registry_mirror=%s" % mirrors[0])
' 2>/dev/null
fi
''',
    "kubernetes": r'''
ROLE=""
if systemctl is-active --quiet k3s 2>/dev/null; then ROLE=control-plane
elif systemctl is-active --quiet k3s-agent 2>/dev/null; then ROLE=worker; fi
emit node_role "$ROLE"
CFG="$($S cat /etc/rancher/k3s/config.yaml 2>/dev/null)"
if [ -n "$CFG" ]; then
  key() { printf '%s\n' "$CFG" | sed -n "s/^$1: *//p" | head -n1 | tr -d "'\""; }
  has() { printf '%s\n' "$CFG" | grep -q "$1"; }
  emit server_endpoint "$(key server)"
  emit node_ip "$(key node-ip)"
  emit k3s_taint "$(yesno has '^node-taint:')"
  emit k3s_max_pods "$(printf '%s\n' "$CFG" | sed -n 's/.*max-pods=\([0-9]*\).*/\1/p' | head -n1)"
  if [ "$ROLE" = control-plane ]; then
    DISABLED="$(printf '%s\n' "$CFG" | awk '/^disable:/{f=1;next} f&&/^- /{print $2;next} {f=0}')"
    listed() { printf '%s\n' "$DISABLED" | grep -qx "$1" && echo true || echo false; }
    emit k3s_disable_servicelb "$(listed servicelb)"
    emit k3s_disable_metrics_server "$(listed metrics-server)"
    emit k3s_disable_local_storage "$(listed local-storage)"
    emit k3s_disable_network_policy "$(yesno has '^disable-network-policy: true')"
    emit k3s_disable_cloud_controller "$(yesno has '^disable-cloud-controller: true')"
    emit k3s_disable_helm_controller "$(yesno has '^disable-helm-controller: true')"
    emit k3s_security_baseline "$(yesno has '^secrets-encryption: true')"
    if [ "$(listed traefik)" = true ]; then emit k3s_ingress none; else emit k3s_ingress traefik; fi
  fi
fi
''',
    "longhorn": r'''
MNT="$(findmnt -rn -S LABEL=longhorn -o TARGET 2>/dev/null | head -n1)"
if [ -n "$MNT" ]; then
  emit storage_path "$MNT"
  SRC="$(findmnt -rn -S LABEL=longhorn -o SOURCE 2>/dev/null | head -n1)"
  PARENT="$(lsblk -no PKNAME "$SRC" 2>/dev/null | head -n1)"
  emit storage_disk "${PARENT:+/dev/$PARENT}"
elif [ -d /var/lib/longhorn ]; then
  emit storage_path /var/lib/longhorn
fi
''',
    "telegraf": r'''
CONF="$($S cat /etc/telegraf/telegraf.conf 2>/dev/null)"
if [ -n "$CONF" ]; then
  pick() { printf '%s\n' "$CONF" | sed -n "$1" | head -n1; }
  emit influx_endpoint "$(pick 's/^ *urls *= *\["\([^"]*\)".*/\1/p')"
  emit influx_org "$(pick 's/^ *organization *= *"\([^"]*\)".*/\1/p')"
  emit influx_bucket "$(pick 's/^ *bucket *= *"\([^"]*\)".*/\1/p')"
fi
''',
}


def supported_modules() -> list[str]:
    return list(_READERS)


_PRINTED_ID = re.compile(r'emit (\w+)|out\("(\w+)"|print\("(\w+)=')


def readable_ids(module_id: str) -> set[str]:
    """Input ids the module's script can print, found by scanning it."""
    return {name for match in _PRINTED_ID.findall(_READERS.get(module_id, "")) for name in match if name}


def build_script(module_id: str) -> str:
    return _PREAMBLE + _READERS[module_id] + "\nexit 0\n"


def _wrap_for_ssh(script: str) -> str:
    # Passed as an argument, not on stdin, so nothing the script runs can swallow the rest of it.
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"bash -c \"$(printf %s '{encoded}' | base64 -d)\""


def _run_remote_script(node_name: str, script: str) -> Dict[str, Any]:
    """Returns {'stdout', 'target', 'error'}; error is empty when the node answered."""
    node_entry = next(
        (item for item in _node_ops_inventory_view() if isinstance(item, dict) and str(item.get("name", "")).strip() == node_name),
        {},
    )
    callback_ip = str(node_entry.get("callback_connect_ip", "") or node_entry.get("callback_source_ip", "") or "").strip()
    host, source = _preferred_node_connect_target(node_name, str(node_entry.get("address", "") or "").strip(), callback_ip)
    if not host:
        return {"stdout": "", "target": "", "error": "No node address known yet."}

    user = str(node_entry.get("node_username", "") or "").strip() or str(_load_node_config().get("username", "") or "").strip() or "jdizzle"
    cert = str(node_entry.get("certificate", "") or "").strip()
    key_path = str(KEYS_DIR / (cert if cert and _is_safe_key_name(cert) else "rack-bootstrap-ed25519"))
    target = f"{user}@{host} ({source})"

    key_error = _ssh_private_key_preflight(key_path)
    if key_error:
        return {"stdout": "", "target": target, "error": key_error}

    _seed_known_host(host)
    cmd = [
        "ssh",
        "-i", key_path,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=6",
        *_ansible_known_hosts_args().split(),
        f"{user}@{host}",
        _wrap_for_ssh(script),
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=SSH_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return {"stdout": "", "target": target, "error": "ssh command not found on this host."}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "target": target, "error": f"Timed out after {SSH_TIMEOUT_SECONDS}s reading the node."}
    stdout = proc.stdout or ""
    if proc.returncode != 0 and not stdout.strip():
        return {"stdout": "", "target": target, "error": (proc.stderr or "").strip()[-400:] or f"ssh exited with code {proc.returncode}."}
    return {"stdout": stdout, "target": target, "error": ""}


def _readable_specs(module_id: str, group: str) -> Dict[str, Dict[str, Any]]:
    profile = _profile_map().get(module_id) or {}
    specs = profile.get("input_specs") or []
    return {
        str(spec["id"]): spec
        for spec in specs
        if isinstance(spec, dict) and spec.get("id") and not spec.get("sensitive")
        and (not isinstance(spec.get("only_groups"), list) or group in spec["only_groups"])
    }


def _normalise(spec: Dict[str, Any], value: str, group: str) -> str:
    value = value.strip()[:MAX_VALUE_CHARS]
    kind = str(spec.get("input_type") or "text").lower()
    if kind == "checkbox":
        return "true" if value.lower() in TRUE_WORDS else "false"
    if kind == "select":
        by_group = (spec.get("options_by_group") or {}).get(group)
        options = by_group if isinstance(by_group, list) else (spec.get("options") or [])
        if spec.get("id") == "node_role" and group == "rpi_3bplus_active" and value == "worker":
            value = "light-worker"
        return value if value in options else ""
    return value


def parse_live_output(text: str, module_id: str, group: str) -> Dict[str, str]:
    specs = _readable_specs(module_id, group)
    found: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, raw = line.partition("=")
        key = key.strip()
        if not sep or key not in specs:
            continue
        value = _normalise(specs[key], raw, group)
        if value != "" or str(specs[key].get("input_type") or "").lower() == "checkbox":
            found[key] = value
    return found


def read_live_config(node_name: str, module_id: str) -> Dict[str, Any]:
    node_entry = next(
        (item for item in _node_ops_inventory_view() if isinstance(item, dict) and str(item.get("name", "")).strip() == node_name),
        {},
    )
    group = str(node_entry.get("group", "") or "")
    result: Dict[str, Any] = {
        "ok": False,
        "node": node_name,
        "module": module_id,
        "target": "",
        "read_at": datetime.now(timezone.utc).isoformat(),
        "fields": {},
        "readable": [],
        "error": "",
    }
    if module_id not in _READERS:
        result.update(ok=True, error="")
        return result

    run = _run_remote_script(node_name, build_script(module_id))
    result["target"] = run["target"]
    if run["error"]:
        result["error"] = run["error"]
        return result
    result["fields"] = parse_live_output(run["stdout"], module_id, group)
    result["readable"] = sorted(readable_ids(module_id) & set(_readable_specs(module_id, group)))
    result["ok"] = True
    logger.info("Read live %s config from node=%s via %s (%d field(s))", module_id, node_name, run["target"], len(result["fields"]))
    return result
