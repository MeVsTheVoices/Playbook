"""Validate Wi-Fi credentials and render cloud-init network configuration."""

import json
import re
from typing import Any

import yaml


def parse_networks(value: Any) -> list[dict[str, str]]:
    if value is None or value == "":
        return []
    if not isinstance(value, str):
        raise ValueError("Wi-Fi networks must be encoded as a JSON string.")
    try:
        networks = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Wi-Fi networks are not valid JSON.") from exc
    if not isinstance(networks, list):
        raise ValueError("Wi-Fi networks must be a list.")
    result = []
    seen = set()
    for network in networks:
        if not isinstance(network, dict) or set(network) != {"ssid", "password"}:
            raise ValueError("Each Wi-Fi network needs an SSID and password.")
        ssid = network["ssid"]
        password = network["password"]
        try:
            ssid_length = len(ssid.encode("utf-8")) if isinstance(ssid, str) else 0
        except UnicodeEncodeError:
            ssid_length = 0
        if not 1 <= ssid_length <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
            raise ValueError("Wi-Fi SSIDs must be 1-32 bytes without control characters.")
        if not isinstance(password, str) or not (
            8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)
            or re.fullmatch(r"[A-Fa-f0-9]{64}", password)
        ):
            raise ValueError("Wi-Fi passwords must be 8-63 printable ASCII characters or a 64-digit hex PSK.")
        if ssid in seen:
            raise ValueError("Wi-Fi SSIDs must be unique.")
        seen.add(ssid)
        result.append({"ssid": ssid, "password": password})
    return result


def combined_networks(options: dict[str, Any], *, toolkit: bool = False) -> list[dict[str, str]]:
    networks = parse_networks(options.get("wifi_networks"))
    ssid = options.get("toolkit_wifi_ssid" if toolkit else "wifi_ssid") or ""
    password = options.get("toolkit_wifi_password" if toolkit else "wifi_psk") or ""
    if ssid or password:
        primary = parse_networks(json.dumps([{"ssid": ssid, "password": password}]))
        networks = primary + networks
    if len({network["ssid"] for network in networks}) != len(networks):
        raise ValueError("Wi-Fi SSIDs must be unique.")
    return networks


def network_config(networks: list[dict[str, str]], country: str, *, renderer: str, interface: str, wired_bootstrap: bool = False) -> dict[str, Any]:
    wifi = {
        "dhcp4": True,
        "optional": True,
        "access-points": {network["ssid"]: {"password": network["password"]} for network in networks},
    }
    if country:
        wifi["regulatory-domain"] = country
    ethernet = {"ethernet": {"match": {"name": "e*"}, "dhcp4": True, "optional": not wired_bootstrap}}
    return {"version": 2, "renderer": renderer, "wifis": {interface: wifi}, "ethernets": ethernet}


def validate_interface(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", value):
        raise ValueError("Wi-Fi interface must be a Linux interface name of at most 15 characters.")
    return value


def main() -> None:
    import os
    try:
        options = {
            "wifi_ssid": os.environ.get("CLOUD_INIT_WIFI_SSID", ""),
            "wifi_psk": os.environ.get("CLOUD_INIT_WIFI_PSK", ""),
            "wifi_networks": os.environ.get("CLOUD_INIT_WIFI_NETWORKS", ""),
        }
        networks = combined_networks(options)
        country = os.environ.get("CLOUD_INIT_WIFI_COUNTRY", "")
        if networks and not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("Wi-Fi country must be two uppercase letters.")
        if networks:
            renderer = os.environ.get("CLOUD_INIT_WIFI_RENDERER", "")
            if renderer not in {"networkd", "NetworkManager"}:
                raise ValueError("Wi-Fi image renderer is unknown; select a supported catalog image or set CLOUD_INIT_WIFI_RENDERER explicitly.")
            interface = validate_interface(os.environ.get("CLOUD_INIT_WIFI_INTERFACE", "wlan0"))
            wired_bootstrap = os.environ.get("CLOUD_INIT_WIFI_WIRED_BOOTSTRAP") == "true"
            print(yaml.safe_dump(network_config(networks, country, renderer=renderer, interface=interface, wired_bootstrap=wired_bootstrap), sort_keys=False))
    except ValueError as exc:
        raise SystemExit(f"Wi-Fi configuration: {exc}") from exc


if __name__ == "__main__":
    main()
