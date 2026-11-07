from __future__ import annotations

from typing import Any, Callable, Dict, List

from flask import Blueprint, redirect, render_template

from k3s_lightweight import INGRESS_CHOICES as K3S_INGRESS_CHOICES
from k3s_lightweight import INGRESS_DEFAULT as K3S_INGRESS_DEFAULT
from k3s_lightweight import LIGHTWEIGHT_OPTIONS as K3S_LIGHTWEIGHT_OPTIONS
from k3s_lightweight import ROLE_PROFILES as K3S_ROLE_PROFILES
from k3s_lightweight import ROLE_TAINTS as K3S_ROLE_TAINTS
from k3s_lightweight import ui_defaults as k3s_role_ui_defaults
from services.events import _read_events
from services.image_catalog import _image_state_summary
from services.images import _read_image_history
from services.k3s_config import _default_k3s_config
from services.kubernetes import _local_k3s_version
from services.network import _default_callback_url
from services.sd_devices import _ensure_device_monitor_started, _list_sd_devices
from services.sd_profiles import _load_sd_option_profiles
from services.settings import _load_app_settings, _tailscale_policy_defaults
from network_toolkit import BOOT_INPUT_SPECS as TOOLKIT_SPECS


def register_page_routes(app: Any, *, catalog_entries_for_ui: Callable[[], List[Dict[str, Any]]]) -> None:
    pages = Blueprint("pages", __name__)

    @pages.get("/")
    def index() -> str:
        _ensure_device_monitor_started()
        events = _read_events(limit=200)
        rack_name = _load_app_settings().get("rack_name", "homelab")
        option_profiles = _load_sd_option_profiles()
        image_catalog = catalog_entries_for_ui()
        return render_template(
            "index.html",
            events=events,
            image_history=_read_image_history(limit=150),
            image_summary=_image_state_summary(),
            hostname_prefix=f"{rack_name}-",
            sd_option_profiles=option_profiles,
            devices=_list_sd_devices(),
            callback_url_default=_default_callback_url(),
            k3s_config_default=_default_k3s_config(),
            k3s_version_default=_local_k3s_version(),
            k3s_lightweight_options=K3S_LIGHTWEIGHT_OPTIONS,
            k3s_ingress_choices=K3S_INGRESS_CHOICES,
            k3s_ingress_default=K3S_INGRESS_DEFAULT,
            k3s_lightweight_profiles=" ".join(K3S_ROLE_PROFILES),
            k3s_role_defaults=k3s_role_ui_defaults(),
            k3s_role_taints=K3S_ROLE_TAINTS,
            image_catalog=image_catalog,
            toolkit_specs=TOOLKIT_SPECS,
        )

    @pages.get("/tailscale-acl")
    def tailscale_acl() -> str:
        return render_template("tailscale_acl.html")

    @pages.get("/openwrt")
    def openwrt_page() -> Any:
        return redirect("/nodes#openwrt-host")

    @pages.get("/fragments/tailscale-acl/overview")
    def tailscale_acl_overview_fragment() -> str:
        return render_template("fragments/tailscale_acl_overview.html")

    @pages.get("/fragments/tailscale-acl/inputs")
    def tailscale_acl_inputs_fragment() -> str:
        return render_template("fragments/tailscale_acl_inputs.html", defaults=_tailscale_policy_defaults())

    @pages.get("/fragments/tailscale-acl/output")
    def tailscale_acl_output_fragment() -> str:
        return render_template("fragments/tailscale_acl_output.html")

    @pages.get("/fragments/tailscale-acl/apply-guide")
    def tailscale_acl_apply_guide_fragment() -> str:
        return render_template("fragments/tailscale_acl_apply_guide.html")

    @pages.get("/nodes")
    def nodes_page() -> str:
        return render_template("nodes.html")

    @pages.get("/monitor")
    def monitor_page() -> str:
        return render_template("monitor.html")

    @pages.get("/kubernetes")
    def kubernetes_page() -> str:
        return render_template("kubernetes.html")

    @pages.get("/test-terminal")
    def test_terminal_page() -> str:
        return render_template("test_terminal.html")

    @pages.get("/images")
    def images_page() -> str:
        image_history = _read_image_history(limit=500)
        image_catalog = catalog_entries_for_ui()
        return render_template("images.html", image_catalog=image_catalog, image_history=image_history, image_records=image_history)

    @pages.get("/settings")
    def settings_page() -> str:
        return render_template("settings.html")

    @pages.get("/fragments/cloud-init/event-rows")
    def cloud_init_event_rows() -> str:
        return render_template("fragments/callback_rows.html", events=_read_events(limit=200))

    @pages.get("/fragments/sd/image-catalog-rows")
    def sd_image_catalog_rows() -> str:
        catalog_rows = []
        for entry in catalog_entries_for_ui():
            catalog_rows.append(
                {
                    "id": entry.get("id", ""),
                    "label": entry.get("label") or entry.get("id") or "Catalog entry",
                    "profile": ", ".join(str(p) for p in entry.get("profiles", []) if str(p).strip()) or "-",
                    "source": "image_url",
                    "source_value": entry.get("image_url") or "-",
                    "hash_status": "verified" if entry.get("image_sha256") else "sha-missing",
                    "observed_sha256": entry.get("image_sha256") or "",
                    "expected_sha256": entry.get("image_sha256") or "",
                    "job_status": "catalog",
                    "exit_code": None,
                    "local_available": bool(entry.get("local_available", False)),
                    "download_path": entry.get("cache_path") or "",
                    "recorded_at": "catalog",
                }
            )
        return render_template("fragments/image_catalog_rows.html", image_history=catalog_rows)

    @pages.get("/fragments/sd/images-rows")
    def sd_images_rows() -> str:
        return render_template("fragments/managed_image_rows.html", image_records=_read_image_history(limit=500))

    app.register_blueprint(pages)