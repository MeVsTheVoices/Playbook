from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from services.settings import _load_app_settings

try:
    # Third-party package used for future Tailscale API calls.
    import tailscale as tailscale_sdk  # type: ignore
except Exception:  # pragma: no cover - fallback when dependency is not installed
    tailscale_sdk = None


@dataclass(frozen=True)
class TailscaleClientConfig:
    tailnet: str
    api_key: str


def load_tailscale_client_config() -> TailscaleClientConfig:
    settings = _load_app_settings()
    return TailscaleClientConfig(
        tailnet=str(settings.get("tailscale_tailnet", "")).strip(),
        api_key=str(settings.get("tailscale_api_key", "")).strip(),
    )


def tailscale_client_ready() -> bool:
    cfg = load_tailscale_client_config()
    return bool(tailscale_sdk is not None and cfg.api_key and cfg.tailnet)


def build_tailscale_client() -> Any:
    cfg = load_tailscale_client_config()
    if tailscale_sdk is None:
        raise RuntimeError("tailscale dependency not installed")
    if not cfg.api_key:
        raise RuntimeError("tailscale_api_key is not configured")
    if not cfg.tailnet:
        raise RuntimeError("tailscale_tailnet is not configured")

    client_cls = getattr(tailscale_sdk, "Tailscale", None)
    if client_cls is not None:
        try:
            return client_cls(api_key=cfg.api_key, tailnet=cfg.tailnet)
        except TypeError:
            return client_cls(cfg.api_key)

    raise RuntimeError("Unsupported tailscale package API: missing Tailscale client class")


def get_tailscale_framework_status() -> Dict[str, Any]:
    cfg = load_tailscale_client_config()
    return {
        "library_available": tailscale_sdk is not None,
        "tailnet": cfg.tailnet,
        "api_key_set": bool(cfg.api_key),
        "client_ready": tailscale_client_ready(),
    }
