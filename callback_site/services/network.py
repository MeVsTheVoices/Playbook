"""Callback URL, local IP and source-IP resolution."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List
from urllib import parse as urlparse

from flask import request


def _detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def _default_callback_url() -> str:
    scheme = "http"
    full_url = os.getenv("CALLBACK_PUBLIC_URL", "").strip()
    if full_url:
        parsed = urlparse.urlparse(full_url)
        if parsed.scheme and parsed.netloc:
            return parsed._replace(path="/api/cloud-init/callback", params="", query="", fragment="").geturl()

    host = os.getenv("CALLBACK_PUBLIC_HOST", "").strip()
    if not host:
        host = _detect_local_ip()

    if "://" in host:
        parsed = urlparse.urlparse(host)
        if parsed.hostname:
            if parsed.scheme:
                scheme = parsed.scheme
            host = parsed.hostname or host
            supplied_port = parsed.port
            if supplied_port:
                port = str(supplied_port)
            else:
                port = os.getenv("CALLBACK_PUBLIC_PORT", "").strip() or os.getenv("PORT", "8088").strip() or "8088"
            if host and ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"{scheme}://{host}:{port}/api/cloud-init/callback"

    port = (
        os.getenv("CALLBACK_PUBLIC_PORT", "").strip()
        or os.getenv("PORT", "8088").strip()
        or "8088"
    )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}/api/cloud-init/callback"


def _first_forwarded_ip(value: str) -> str:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts[0] if parts else ""


def _normalize_ip(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ""


def _is_transit_hop_ip(ip: str) -> bool:
    parsed = _normalize_ip(ip)
    if not parsed:
        return False
    try:
        addr = ipaddress.ip_address(parsed)
    except ValueError:
        return False

    # Common non-LAN transit ranges observed behind VPN/proxy edges.
    return (
        addr in ipaddress.ip_network("10.255.0.0/16")
        or addr in ipaddress.ip_network("100.64.0.0/10")
    )


_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_tailscale_ip(ip: str) -> bool:
    parsed = _normalize_ip(ip)
    return bool(parsed) and ipaddress.ip_address(parsed) in _TAILSCALE_NET


_TAILNET_CACHE: Dict[str, Any] = {"at": -1e9, "peers": {}}


_TAILNET_LOCK = threading.Lock()


TAILNET_CACHE_SECONDS = 20


def _tailscale_peers() -> Dict[str, str]:
    """Online tailnet peers as lowercase hostname -> IPv4, from the local `tailscale status`."""
    with _TAILNET_LOCK:
        if time.monotonic() - float(_TAILNET_CACHE["at"]) < TAILNET_CACHE_SECONDS:
            return dict(_TAILNET_CACHE["peers"])
        try:
            proc = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5, check=False)
            data = json.loads(proc.stdout or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            data = {}
        peers: Dict[str, str] = {}
        for peer in (data.get("Peer") or {}).values():
            if not isinstance(peer, dict) or not peer.get("Online"):
                continue
            ipv4 = next((ip for ip in peer.get("TailscaleIPs") or [] if ":" not in str(ip) and _is_tailscale_ip(str(ip))), "")
            if not ipv4:
                continue
            for name in (str(peer.get("HostName", "")), str(peer.get("DNSName", "")).split(".")[0]):
                if name.strip():
                    peers.setdefault(name.strip().lower(), ipv4)
        _TAILNET_CACHE.update({"at": time.monotonic(), "peers": peers})
        return dict(peers)


def _tailscale_peer_ip(node_name: str) -> str:
    return _tailscale_peers().get(str(node_name or "").strip().lower(), "")


def _is_usable_callback_ip(ip: str) -> bool:
    parsed = _normalize_ip(ip)
    if not parsed:
        return False
    if _is_tailscale_ip(parsed):
        return True
    try:
        addr = ipaddress.ip_address(parsed)
    except ValueError:
        return False
    return addr.is_private and not _is_transit_hop_ip(parsed) and not addr.is_loopback and not addr.is_link_local


def _preferred_connect_ip_from_history(current_ip: str, ip_history: List[str]) -> str:
    current_ip = str(current_ip or "").strip()
    if _is_usable_callback_ip(current_ip):
        return current_ip

    for raw in ip_history:
        candidate = str(raw or "").strip()
        if _is_usable_callback_ip(candidate):
            return candidate

    return current_ip


def _preferred_node_connect_target(node_name: str, inventory_address: str, callback_source_ip: str = "") -> tuple[str, str]:
    node_name = str(node_name or "").strip()
    inventory_address = str(inventory_address or "").strip()
    callback_source_ip = str(callback_source_ip or "").strip()

    # The NucBox cannot route to NATed LAN addresses; the tailnet address always works.
    if _is_tailscale_ip(callback_source_ip):
        return callback_source_ip, "callback"
    if _is_tailscale_ip(inventory_address):
        return inventory_address, "inventory"

    # Callbacks only carry the LAN/NAT address; a node already on the tailnet is reachable at its tailnet IP.
    tailnet_ip = _tailscale_peer_ip(node_name)
    if tailnet_ip:
        return tailnet_ip, "tailscale"

    if _is_usable_callback_ip(callback_source_ip):
        return callback_source_ip, "callback"

    if inventory_address and not _is_transit_hop_ip(inventory_address):
        return inventory_address, "inventory"

    if callback_source_ip:
        return callback_source_ip, "callback_recent"

    resolved_ip, resolved_source = _resolve_lan_ip_from_names(node_name)
    if resolved_ip:
        return resolved_ip, resolved_source or "dns"

    if inventory_address:
        return inventory_address, "inventory_transit"
    if node_name:
        return node_name, "hostname"
    return "", "none"


def _resolve_lan_ip_from_names(*names: str) -> tuple[str, str]:
    seen: set[str] = set()
    for name in names:
        target = str(name or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        try:
            infos = socket.getaddrinfo(target, None, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            continue

        for info in infos:
            sockaddr = info[4] if len(info) > 4 else ()
            candidate = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else ""
            normalized = _normalize_ip(candidate)
            if not normalized:
                continue
            try:
                addr = ipaddress.ip_address(normalized)
            except ValueError:
                continue
            if addr.is_private and not addr.is_loopback and not addr.is_link_local:
                return normalized, f"dns:{target}"
    return "", ""


def _resolve_source_ip(payload: Dict[str, Any], hostname: str = "", fqdn: str = "") -> tuple[str, str]:
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded_for:
        ip = _normalize_ip(_first_forwarded_ip(forwarded_for))
        if ip:
            return ip, "x-forwarded-for"

    for header in ("X-Real-IP", "CF-Connecting-IP", "True-Client-IP"):
        value = _normalize_ip(request.headers.get(header, ""))
        if value:
            return value, header.lower()

    for key in ("source_ip", "client_ip", "ip", "remote_addr"):
        value = _normalize_ip(str(payload.get(key, "")).strip())
        if value:
            return value, f"payload:{key}"

    remote = _normalize_ip(request.remote_addr or "")
    if remote and _is_transit_hop_ip(remote):
        return remote, "remote_addr_transit"

    return remote, "remote_addr"


def _header_or_payload(payload: Dict[str, Any], *keys: str) -> str:
    def candidate_names(name: str) -> List[str]:
        raw = str(name or "").strip()
        if not raw:
            return []
        pool: List[str] = []
        seen: set[str] = set()
        for value in (raw, raw.lower(), raw.upper(), raw.replace("-", "_"), raw.replace("_", "-")):
            for item in (value, value.upper(), value.lower()):
                if item and item not in seen:
                    seen.add(item)
                    pool.append(item)
        if raw.startswith("X-"):
            short = raw[2:]
            short_candidates = candidate_names(short)
            for candidate in short_candidates:
                if candidate not in seen:
                    seen.add(candidate)
                    pool.append(candidate)
        if raw.startswith("X_"):
            short = raw[2:]
            for candidate in candidate_names(short):
                if candidate not in seen:
                    seen.add(candidate)
                    pool.append(candidate)
        if raw.startswith("X-SSL-Client-"):
            suffix = raw[len("X-SSL-Client-"):]
            env_name = f"SSL_CLIENT_{suffix.upper().replace('-', '_')}"
            if env_name not in seen:
                seen.add(env_name)
                pool.append(env_name)
        if raw.startswith("X-Client-Cert-"):
            suffix = raw[len("X-Client-Cert-"):]
            env_name = f"SSL_CLIENT_{suffix.upper().replace('-', '_')}"
            if env_name not in seen:
                seen.add(env_name)
                pool.append(env_name)
        if raw.startswith("X-SSL-Client") and raw not in {"X-SSL-Client-Cert", "X-SSL-Client-Subject", "X-SSL-Client-Issuer", "X-SSL-Client-Fingerprint"}:
            env_name = f"SSL_CLIENT_{raw[len('X-SSL-Client-'):].upper().replace('-', '_')}"
            if env_name not in seen:
                seen.add(env_name)
                pool.append(env_name)
        return pool

    for key in keys:
        for candidate in candidate_names(key):
            if candidate in request.headers:
                value = request.headers.get(candidate)
                if value:
                    return str(value).strip()
            if candidate in payload and payload.get(candidate) not in (None, ""):
                return str(payload.get(candidate)).strip()
            env_value = request.environ.get(candidate)
            if env_value not in (None, ""):
                return str(env_value).strip()
            env_value = os.environ.get(candidate)
            if env_value not in (None, ""):
                return str(env_value).strip()
            http_name = f"HTTP_{candidate.replace('-', '_').replace('.', '_')}"
            env_value = request.environ.get(http_name)
            if env_value not in (None, ""):
                return str(env_value).strip()
            env_value = os.environ.get(http_name)
            if env_value not in (None, ""):
                return str(env_value).strip()
    return ""
