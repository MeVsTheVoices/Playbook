"""On-demand Tailscale status as observed by the selected node, not the portal."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from services.live_config import _run_remote_script

logger = logging.getLogger(__name__)

STATUS_SCRIPT = r'''
if ! command -v tailscale >/dev/null 2>&1; then
  echo "Tailscale is not installed or is not in the SSH user's PATH." >&2
  exit 1
fi
tailscale status --json
'''

_STATUS_FIELDS = (
    "Version", "BackendState", "TUN", "TailscaleIPs", "Health",
    "MagicDNSSuffix", "CurrentTailnet", "ExitNodeStatus",
)
_PEER_FIELDS = (
    "ID", "HostName", "DNSName", "OS", "TailscaleIPs", "Tags",
    "PrimaryRoutes", "AllowedIPs", "Online", "Active", "InNetworkMap",
    "InMagicSock", "InEngine", "CurAddr", "Relay", "PeerRelay",
    "RxBytes", "TxBytes", "LastSeen", "LastHandshake", "ExitNode",
    "ExitNodeOption", "NoFileSharingReason",
)


def _peer_fields(peer: dict[str, Any]) -> dict[str, Any]:
    return {key: peer[key] for key in _PEER_FIELDS if key in peer}


def read_node_tailscale(node_name: str) -> dict[str, Any]:
    run = _run_remote_script(node_name, STATUS_SCRIPT)
    result: dict[str, Any] = {
        "ok": False,
        "node": node_name,
        "target": run["target"],
        "read_at": datetime.now(timezone.utc).isoformat(),
        "error": run["error"],
    }
    if not result["error"]:
        try:
            status = json.loads(run["stdout"])
        except ValueError:
            result["error"] = "The node returned invalid Tailscale status JSON."
        else:
            if (
                not isinstance(status, dict)
                or not isinstance(status.get("BackendState"), str)
                or not isinstance(status.get("Self"), (dict, type(None)))
                or not isinstance(status.get("Peer"), (dict, type(None)))
                or any(not isinstance(peer, dict) for peer in (status.get("Peer") or {}).values())
            ):
                result["error"] = "The node returned an unexpected Tailscale status structure."
            else:
                # Do not expose AuthURL, user records, public keys, or future unknown fields.
                snapshot = {key: status[key] for key in _STATUS_FIELDS if key in status}
                snapshot["Self"] = _peer_fields(status.get("Self") or {})
                snapshot["Peer"] = [_peer_fields(peer) for peer in (status.get("Peer") or {}).values()]
                result.update(ok=True, status=snapshot)
    if result["error"]:
        logger.warning("Tailscale read failed for node=%s via %s: %s", node_name, run["target"], result["error"])
    else:
        logger.info("Read Tailscale perspective for node=%s via %s", node_name, run["target"])
    return result
