"""SD-card prepare payload sanitising, env mapping and the prepare job runner."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from k3s_lightweight import (
    apply_role_to_k3s_config as _apply_k3s_role_config,
    is_role_profile as _is_k3s_role_profile,
    k3s_mode_for_role as _k3s_mode_for_role,
    LIGHTWEIGHT_OPTION_IDS as _K3S_SERVER_OPTION_IDS,
    resolve_k3s_version as _resolve_k3s_version,
    ROLE_CONTROL_PLANE as K3S_ROLE_CONTROL_PLANE,
    ROLE_OPTION_ID as _K3S_ROLE_OPTION_ID,
    strip_options as _strip_k3s_lightweight_options,
)

from services import sd_flasher_client
from services.images import _record_image_history
from services.image_catalog import _catalog_entry_with_availability
from domain_defaults import IMAGE_CATALOG
from services.k3s_config import _default_k3s_config, _normalize_k3s_node_name
from services.kubernetes import _local_k3s_version
from services.ssh_keys import _archive_ssh_public_key
from services.storage import IMAGE_CACHE_DIR, INVENTORY_FILE, SCRIPT_PATH
from network_toolkit import BOOT_INPUT_SPECS as TOOLKIT_SPECS, PROFILE as TOOLKIT_PROFILE, SECRET_OPTIONS as TOOLKIT_SECRETS, validate_options as validate_toolkit_options
from network_toolkit import EXTRA_PACKAGES_OPTION, extra_packages
from playbook_server import INPUT_SPECS as SERVER_SPECS, PROFILE as SERVER_PROFILE, validate_options as validate_server_options
from package_options import package_names
from services import flash_helper
from services.installer_config import is_installer, validate as validate_installer

logger = logging.getLogger(__name__)


JOBS: Dict[str, Dict[str, Any]] = {}


JOBS_LOCK = threading.Lock()
# Profiles whose default image comes from the catalog, so it is cached and shown like a picked image.
_DEFAULT_CATALOG_ENTRY = {
    TOOLKIT_PROFILE: "network-toolkit-raspios-trixie-arm64-lite",
    SERVER_PROFILE: "playbook-server-ubuntu-noble-amd64",
}
SD_SECRET_OPTIONS = {"tailscale_auth_key", "password", "telegraf_influx_token", *TOOLKIT_SECRETS}


def _ensure_prereqs() -> List[str]:
    errors: List[str] = []
    if sd_flasher_client.enabled():
        errors.extend(sd_flasher_client.prereq_errors())
    else:
        if not SCRIPT_PATH.exists():
            errors.append(f"Missing script: {SCRIPT_PATH}")
        if sd_flasher_client.running_in_container():
            errors.append(
                "This dashboard runs in a container and cannot see host SD/USB devices. "
                "Start the host flasher with scripts/run_sd_flasher.sh and set SD_FLASHER_URL."
            )
    if not INVENTORY_FILE.exists():
        errors.append(f"Missing inventory file: {INVENTORY_FILE}")
    return errors


def _strip_dataset_fields(payload: Any) -> Any:
    # Drop bulky/sensitive dataset blobs from inbound payloads before persistence.
    if isinstance(payload, dict):
        cleaned: Dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).strip().lower() == "dataset":
                continue
            cleaned[str(key)] = _strip_dataset_fields(value)
        return cleaned
    if isinstance(payload, list):
        return [_strip_dataset_fields(item) for item in payload]
    return payload


def _sanitize_sd_prepare_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = {
        "profile",
        "distribution",
        "hostname",
        "device",
        "ssh_public_key",
        "ssh_key_name",
        "callback_url",
        "image_url",
        "image_file",
        "image_sha256",
        "workspace",
        "sd_options_profile",
        "pi_only",
        "cloud_init_options",
        "device_identity",
        "confirm_device",
        "workflow",
        "catalog_id",
        "install_authorized",
    }
    cleaned: Dict[str, Any] = {}
    for key in allowed_keys:
        if key in payload:
            cleaned[key] = payload.get(key)
    if not isinstance(cleaned.get("cloud_init_options"), dict):
        cleaned["cloud_init_options"] = {}
    cleaned["sd_options_profile"] = str(cleaned.get("sd_options_profile", "")).strip()
    cleaned["pi_only"] = bool(cleaned.get("pi_only", False))
    if is_installer(cleaned):
        try:
            validate_installer(cleaned)
        except ValueError as exc:
            cleaned["config_errors"] = [str(exc)]
        return cleaned
    cloud_options = cleaned["cloud_init_options"]
    if cleaned.get("profile") != TOOLKIT_PROFILE:
        option_errors = []
        try:
            package_names(cloud_options.get("packages", []))
        except ValueError as exc:
            option_errors.append(str(exc))
        for key in ("configure_neovim", "install_cli_tools"):
            if key in cloud_options and not isinstance(cloud_options[key], bool):
                option_errors.append(f"{key} must be true or false.")
        if option_errors:
            cleaned["config_errors"] = option_errors
    else:
        for key in ("packages", "configure_neovim", "install_cli_tools"):
            cloud_options.pop(key, None)
    distribution = str(cleaned.get("distribution", "standard")).strip().lower() or "standard"
    k3s_config = str(cloud_options.get("k3s_config", "") or "").strip()
    if cleaned.get("profile") == TOOLKIT_PROFILE:
        errors = validate_toolkit_options(cloud_options, boot=True)
        if not re.fullmatch(r"tskey-[A-Za-z0-9_-]+", str(cloud_options.get("tailscale_auth_key") or "")):
            errors.append("network-toolkit requires a Tailscale auth key authorized for tag:network-toolkit.")
        if distribution != "standard":
            errors.append("network-toolkit requires the standard distribution, not K3s.")
        if cloud_options.get("tailscale_advertise_routes") or cloud_options.get("tailscale_exit_node"):
            errors.append("network-toolkit cannot advertise routes or use an exit node; Ethernet is for device configuration.")
        if str(cloud_options.get("ssh_port") or "22") != "22":
            errors.append("network-toolkit keeps private SSH on port 22.")
        if str(cloud_options.get("username") or "") in {"root", "network-console"}:
            errors.append("Use a separate non-root administrative user, not network-console.")
        cloud_options.pop("k3s_config", None)
        cloud_options.setdefault("toolkit_enable_funnel", False)
        cloud_options.update({
            "tailscale_enable": True, "disable_ssh_password_auth": True,
            "disable_root_ssh": True, "enable_hardware_checks": True,
            "tailscale_advertise_tags": ["tag:network-toolkit"],
            "fancy_motd": False, "install_telegraf": False,
            "enable_firewall_baseline": False,
        })
        if errors:
            cleaned["config_errors"] = errors
    elif cleaned.get("profile") == SERVER_PROFILE:
        errors = validate_server_options(cloud_options, boot=True)
        if distribution != "standard" or k3s_config:
            errors.append("playbook-server requires the standard distribution, not K3s.")
        cloud_options.pop("k3s_config", None)
        if errors:
            cleaned["config_errors"] = errors
    elif distribution != "k3s" and k3s_config:
        # If the UI sent an explicit k3s config block, treat this as k3s intent
        # even if the distribution select was left on standard.
        distribution = "k3s"
    if distribution == "k3s":
        canonical_name = _normalize_k3s_node_name(cleaned.get("hostname") or None)
        if not k3s_config:
            cloud_options["k3s_config"] = _default_k3s_config(canonical_name).strip()
        else:
            lines = []
            seen_node_name = False
            for raw_line in k3s_config.splitlines():
                line = raw_line.rstrip()
                stripped = line.strip()
                if re.match(r"(?i)^node-name\s*:\s*.*$", stripped):
                    if not seen_node_name:
                        lines.append(f"node-name: {canonical_name}")
                        seen_node_name = True
                    continue
                if re.match(r"(?i)^node-ip\s*:\s*.*$", stripped):
                    continue
                if stripped:
                    lines.append(line)
            if not seen_node_name:
                lines.insert(0, f"node-name: {canonical_name}")
            k3s_config = "\n".join(lines).strip() + "\n"
            cloud_options["k3s_config"] = k3s_config
    cleaned["distribution"] = distribution
    if distribution == "k3s" and not cloud_options.get("k3s_config"):
        cloud_options["k3s_config"] = _default_k3s_config(cleaned.get("hostname") or None).strip()
    _apply_k3s_lightweight_to_payload(cleaned)
    image_errors = _sd_image_selection_errors(cleaned)
    if image_errors:
        cleaned["config_errors"] = list(cleaned.get("config_errors") or []) + image_errors
    return cleaned


def _sd_image_selection_errors(payload: Dict[str, Any]) -> List[str]:
    image_url = str(payload.get("image_url") or "").strip()
    image_file = str(payload.get("image_file") or "").strip()
    profile = str(payload.get("profile") or "").strip()
    if not image_url and not image_file:
        return []
    for entry in IMAGE_CATALOG:
        if not entry.get("image_url"):
            continue
        if entry.get("cloud_init_supported") is not False and profile in entry.get("profiles", []):
            continue
        selected = image_url == entry["image_url"] if not image_file else (
            Path(image_file).expanduser() == Path(_catalog_entry_with_availability(entry)["cache_path"])
        )
        if selected:
            if entry.get("cloud_init_supported") is False:
                return [f"{entry['label']} does not support cloud-init. Choose Ubuntu Server or cloud-init-enabled Pi OS Trixie."]
            return [f"{entry['label']} is not compatible with the {profile} profile."]
    return []


def _apply_k3s_lightweight_to_payload(cleaned: Dict[str, Any]) -> None:
    """Apply the selected role to the K3s config for the Pi boards; strip role options for anything else."""
    cloud_options = cleaned["cloud_init_options"]
    profile = str(cleaned.get("profile", "")).strip().lower()
    if cleaned.get("distribution") == "k3s":
        version_errors = _resolve_k3s_version(cloud_options, "")[1]
        if version_errors:
            cleaned["config_errors"] = list(cleaned.get("config_errors") or []) + version_errors
    if cleaned.get("distribution") != "k3s" or not _is_k3s_role_profile(profile):
        _strip_k3s_lightweight_options(cloud_options)
        return
    merged, role, errors = _apply_k3s_role_config(str(cloud_options.get("k3s_config", "")), cloud_options, profile)
    cloud_options["k3s_config"] = merged
    cloud_options[_K3S_ROLE_OPTION_ID] = role
    # Server-only options must not linger when the chosen role is not a server.
    if role != K3S_ROLE_CONTROL_PLANE:
        _strip_k3s_lightweight_options(cloud_options, _K3S_SERVER_OPTION_IDS)
    if errors:
        cleaned["config_errors"] = list(cleaned.get("config_errors") or []) + errors


def _redact_sd_secret_options(options: Any) -> Dict[str, Any]:
    """Copy of SD cloud-init options without join or toolkit credentials.

    Public templates and job records never contain credentials. Browser-session
    credentials are retained separately and resolved only for explicit preparation.
    """
    if not isinstance(options, dict):
        return {}
    return {key: value for key, value in options.items() if key not in SD_SECRET_OPTIONS}


def _map_cloud_init_options_to_env(env: Dict[str, str], distribution: str, cloud_init_options: Dict[str, Any]) -> List[str]:
    enabled_options: List[str] = []
    env["CLOUD_INIT_EXTRA_PACKAGES"] = " ".join(package_names(cloud_init_options.get("packages", [])))
    if "packages" in cloud_init_options:
        enabled_options.append("packages")
    if EXTRA_PACKAGES_OPTION in cloud_init_options:
        env["CLOUD_INIT_TOOLKIT_EXTRA_PACKAGES"] = " ".join(extra_packages(cloud_init_options))
        enabled_options.append(EXTRA_PACKAGES_OPTION)
    for spec in (*TOOLKIT_SPECS, *SERVER_SPECS):
        key = spec["id"]
        if key in cloud_init_options:
            value = cloud_init_options[key]
            if spec["input_type"] == "checkbox":
                env[f"CLOUD_INIT_{key.upper()}"] = "true" if str(value).lower() == "true" else "false"
            else:
                env[f"CLOUD_INIT_{key.upper()}"] = str(value)
            enabled_options.append(key)

    for key in ("configure_neovim", "install_cli_tools"):
        enabled = cloud_init_options.get(key, False) is True
        env[f"CLOUD_INIT_{key.upper()}"] = "true" if enabled else "false"
        if enabled:
            enabled_options.append(key)
    bool_env_fields = {
        "disable_ssh_password_auth": "CLOUD_INIT_DISABLE_SSH_PASSWORD_AUTH",
        "disable_root_ssh": "CLOUD_INIT_DISABLE_ROOT_SSH",
        "enable_fail2ban": "CLOUD_INIT_ENABLE_FAIL2BAN",
        "enable_unattended_upgrades": "CLOUD_INIT_ENABLE_UNATTENDED_UPGRADES",
        "reboot_after_bootstrap": "CLOUD_INIT_REBOOT_AFTER_BOOTSTRAP",
        "enable_sysctl_hardening": "CLOUD_INIT_ENABLE_SYSCTL_HARDENING",
        "enable_firewall_baseline": "CLOUD_INIT_ENABLE_FIREWALL_BASELINE",
        "enable_selinux_enforcing": "CLOUD_INIT_ENABLE_SELINUX_ENFORCING",
        "k3s_security_baseline": "CLOUD_INIT_K3S_SECURITY_BASELINE",
        "k3s_allow_sd_etcd": "CLOUD_INIT_K3S_ALLOW_SD_ETCD",
        "usb_max_current_enable": "CLOUD_INIT_USB_MAX_CURRENT_ENABLE",
        "pciex1_enable": "CLOUD_INIT_PCIEX1_ENABLE",
        "pciex1_gen3": "CLOUD_INIT_PCIEX1_GEN3",
    }
    for option_key, env_key in bool_env_fields.items():
        if bool(cloud_init_options.get(option_key, False)):
            env[env_key] = "true"
            enabled_options.append(option_key)

    # This option defaults true in the script, so preserve explicit false from the UI.
    if "enable_power_restore_callback" in cloud_init_options:
        power_restore_enabled = bool(cloud_init_options.get("enable_power_restore_callback", False))
        env["CLOUD_INIT_ENABLE_POWER_RESTORE_CALLBACK"] = "true" if power_restore_enabled else "false"
        if power_restore_enabled:
            enabled_options.append("enable_power_restore_callback")

    if "boot_summary" in cloud_init_options:
        env["CLOUD_INIT_BOOT_SUMMARY"] = "true" if bool(cloud_init_options.get("boot_summary", False)) else "false"
        enabled_options.append("boot_summary")

    if distribution == "k3s":
        k3s_config = str(cloud_init_options.get("k3s_config", "") or "").strip()
        if not k3s_config:
            k3s_config = _default_k3s_config().strip()
        env["CLOUD_INIT_K3S_CONFIG"] = k3s_config
        enabled_options.append("k3s_config")
        # Default to the K3s this machine runs, so a new node is never newer than the servers.
        k3s_version, _version_errors = _resolve_k3s_version(cloud_init_options, _local_k3s_version())
        if k3s_version:
            env["CLOUD_INIT_K3S_VERSION"] = k3s_version
            enabled_options.append("k3s_version")
        role = str(cloud_init_options.get(_K3S_ROLE_OPTION_ID, "") or "").strip().lower()
        if role:
            # An explicit role beats the script's guess from the config text.
            env["CLOUD_INIT_K3S_MODE"] = _k3s_mode_for_role(role)
            enabled_options.append(_K3S_ROLE_OPTION_ID)

    if cloud_init_options.get("enable_hardware_checks"):
        env["CLOUD_INIT_ENABLE_HARDWARE_CHECKS"] = "true"
        enabled_options.append("hardware_checks")
    if cloud_init_options.get("fancy_motd"):
        env["CLOUD_INIT_FANCY_MOTD"] = "true"
        enabled_options.append("fancy_motd")
    if cloud_init_options.get("install_telegraf"):
        env["CLOUD_INIT_INSTALL_TELEGRAF"] = "true"
        enabled_options.append("install_telegraf")

    user_fields = {
        "username": "CLOUD_INIT_USERNAME",
        "password": "CLOUD_INIT_USER_PASSWORD",
    }
    for option_key, env_key in user_fields.items():
        value = str(cloud_init_options.get(option_key, "")).strip()
        if value:
            env[env_key] = value
            enabled_options.append(option_key)

    telegraf_fields = {
        "telegraf_influx_url": "CLOUD_INIT_TELEGRAF_INFLUX_URL",
        "telegraf_influx_token": "CLOUD_INIT_TELEGRAF_INFLUX_TOKEN",
        "telegraf_influx_org": "CLOUD_INIT_TELEGRAF_INFLUX_ORG",
        "telegraf_influx_bucket": "CLOUD_INIT_TELEGRAF_INFLUX_BUCKET",
        "telegraf_config_url": "CLOUD_INIT_TELEGRAF_CONFIG_URL",
    }
    for option_key, env_key in telegraf_fields.items():
        value = str(cloud_init_options.get(option_key, "")).strip()
        if value:
            env[env_key] = value
            enabled_options.append(option_key)

    ssh_scalar_fields = {
        "ssh_port": "CLOUD_INIT_SSH_PORT",
    }
    for option_key, env_key in ssh_scalar_fields.items():
        value = str(cloud_init_options.get(option_key, "")).strip()
        if value:
            env[env_key] = value
            enabled_options.append(option_key)

    # First-boot Tailscale. The auth key is only handed to the script through its
    # environment; the script writes it to a root-only file on the node.
    if cloud_init_options.get("tailscale_enable"):
        env["CLOUD_INIT_TAILSCALE_ENABLE"] = "true"
        enabled_options.append("tailscale_enable")
        if cloud_init_options.get("tailscale_ssh"):
            env["CLOUD_INIT_TAILSCALE_SSH"] = "true"
            enabled_options.append("tailscale_ssh")
        for option_key, env_key in {
            "tailscale_auth_key": "CLOUD_INIT_TAILSCALE_AUTH_KEY",
            "tailscale_hostname": "CLOUD_INIT_TAILSCALE_HOSTNAME",
            "tailscale_exit_node": "CLOUD_INIT_TAILSCALE_EXIT_NODE",
        }.items():
            value = str(cloud_init_options.get(option_key, "") or "").strip()
            if value:
                env[env_key] = value
                enabled_options.append(option_key)
        for option_key, env_key in {
            "tailscale_advertise_tags": "CLOUD_INIT_TAILSCALE_TAGS",
            "tailscale_advertise_routes": "CLOUD_INIT_TAILSCALE_ROUTES",
        }.items():
            raw_value = cloud_init_options.get(option_key)
            items = raw_value if isinstance(raw_value, list) else str(raw_value or "").split(",")
            cleaned_items = [str(item).strip() for item in items if str(item).strip()]
            if cleaned_items:
                env[env_key] = ",".join(cleaned_items)
                enabled_options.append(option_key)

    ssh_list_fields = {
        "ssh_allow_groups": "CLOUD_INIT_SSH_ALLOW_GROUPS",
        "extra_ssh_keys": "CLOUD_INIT_EXTRA_SSH_KEYS",
    }
    for option_key, env_key in ssh_list_fields.items():
        raw_value = cloud_init_options.get(option_key)
        if isinstance(raw_value, list):
            cleaned_items = [str(item).strip() for item in raw_value if str(item).strip()]
            if cleaned_items:
                env[env_key] = "\n".join(cleaned_items) if option_key == "extra_ssh_keys" else ",".join(cleaned_items)
                enabled_options.append(option_key)
        else:
            value = str(raw_value or "").strip()
            if value:
                env[env_key] = value
                enabled_options.append(option_key)

    return enabled_options


def _run_prepare_job(job_id: str, form: Dict[str, Any]) -> None:
    if flash_helper.enabled():
        flash_helper.run_prepare_job(job_id, form)
        return
    if is_installer(form):
        raise RuntimeError("UEFI installation must run in the isolated flashing helper.")
    hostname = form.get("hostname", "unknown")
    profile = form.get("profile", "unknown")
    device = form.get("device", "unknown")
    logger.info(f"SD preparation job started: job_id={job_id}, hostname={hostname}, profile={profile}, device={device}")
    
    with tempfile.TemporaryDirectory(prefix="sdprep-key-") as tdir:
        pubkey_file = Path(tdir) / "id.pub"
        ssh_public_key = form["ssh_public_key"].strip()
        pubkey_file.write_text(ssh_public_key + "\n", encoding="utf-8")
        archive_info = _archive_ssh_public_key(job_id, form, ssh_public_key)

        cmd = [
            str(SCRIPT_PATH),
            "--profile",
            form["profile"],
            "--hostname",
            form["hostname"],
            "--device",
            form["device"],
            "--ssh-key-file",
            str(pubkey_file),
            "--callback-url",
            form["callback_url"],
            "--yes",
        ]

        image_url = form.get("image_url", "").strip()
        image_file = form.get("image_file", "").strip()
        image_sha256 = form.get("image_sha256", "").strip()
        catalog_entry = next((entry for entry in IMAGE_CATALOG if
            (image_url and entry.get("image_url") == image_url) or
            (not image_url and not image_file and entry.get("id") == _DEFAULT_CATALOG_ENTRY.get(form["profile"]))), None)
        if catalog_entry and not image_file:
            image_url = str(catalog_entry["image_url"])
            image_sha256 = image_sha256 or str(catalog_entry.get("image_sha256") or "")
        workspace = form.get("workspace", "").strip()
        if image_url:
            cmd.extend(["--image-url", image_url])
        if image_file:
            cmd.extend(["--image-file", image_file])
        if image_sha256:
            cmd.extend(["--image-sha256", image_sha256])
        if workspace:
            cmd.extend(["--workspace", workspace])

        remote = sd_flasher_client.enabled()
        run_cmd: List[str]
        if remote or os.geteuid() == 0:
            run_cmd = cmd
        else:
            suggested = " ".join(["sudo"] + cmd)
            logger.error(f"SD preparation requires root privileges: job_id={job_id}, hostname={hostname}")
            with JOBS_LOCK:
                JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
                JOBS[job_id]["stdout"] = ""
                JOBS[job_id]["stderr"] = (
                    "SD preparation requires root privileges. "
                    "Run the callback site as root (e.g. sudo PORT=9821 .venv/bin/python3 app.py) "
                    f"or run this command manually: {suggested}"
                )
                JOBS[job_id]["exit_code"] = 1
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["command"] = cmd
            return

        # Build environment with cloud-init options
        env = os.environ.copy()
        env["IMAGE_CACHE_DIR"] = str(IMAGE_CACHE_DIR)
        env.pop("IMAGE_CACHE_PATH", None)
        if catalog_entry and not image_file:
            env["IMAGE_CACHE_PATH"] = _catalog_entry_with_availability(catalog_entry)["cache_path"]
        distribution = str(form.get("distribution", "standard")).strip().lower() or "standard"
        env["CLOUD_INIT_DISTRIBUTION"] = distribution
        env["CLOUD_INIT_SSH_PUBLIC_KEY_URL"] = str(form.get("ssh_public_key_url", ""))
        if form.get("device_identity"):
            env["FLASH_EXPECTED_IDENTITY"] = str(form["device_identity"])

        cloud_init_options = form.get("cloud_init_options", {})
        if isinstance(cloud_init_options, dict):
            enabled_options = _map_cloud_init_options_to_env(
                env=env,
                distribution=distribution,
                cloud_init_options=cloud_init_options,
            )
            if enabled_options:
                logger.info(f"Cloud-init options enabled for {hostname}: {', '.join(enabled_options)}")

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["command"] = run_cmd
            JOBS[job_id]["stdout"] = (
                "Starting SD preparation.\n"
                f"Target device: {form.get('device', '')}\n"
                f"Cloud-init options profile: {form.get('sd_options_profile', '') or 'none'}\n"
                f"Archived SSH key: {archive_info.get('archive_path', '')}\n"
                "The selected device will be overwritten.\n"
            )

        if remote:
            _run_remote_prepare_job(job_id, form, env, ssh_public_key, image_url, image_file, image_sha256, catalog_entry)
            return

        proc = subprocess.Popen(
            run_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        output_chunks: List[str] = []
        if proc.stdout is not None:
            for line in proc.stdout:
                output_chunks.append(line)
                combined = "".join(output_chunks)[-50000:]
                with JOBS_LOCK:
                    JOBS[job_id]["stdout"] = combined

        proc.wait()
        final_stdout = "".join(output_chunks)[-50000:]

        with JOBS_LOCK:
            JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
            JOBS[job_id]["stdout"] = final_stdout
            JOBS[job_id]["stderr"] = ""
            JOBS[job_id]["exit_code"] = proc.returncode
            JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"

        if proc.returncode == 0:
            logger.info(f"SD preparation completed successfully: job_id={job_id}, hostname={hostname}")
        else:
            logger.error(f"SD preparation failed: job_id={job_id}, hostname={hostname}, exit_code={proc.returncode}")

        _record_image_history(form, "".join(output_chunks), proc.returncode)


REMOTE_POLL_SECONDS = 1.0
REMOTE_LOST_CONTACT_SECONDS = 60.0


def _remote_image_spec(image_url: str, image_file: str, image_sha256: str, catalog_entry: Any) -> Dict[str, str]:
    spec = {"image_url": image_url, "image_sha256": image_sha256, "image_name": ""}
    if image_file:
        path = Path(image_file).expanduser()
        if path.parent.resolve() != IMAGE_CACHE_DIR.resolve():
            raise ValueError("The host SD flasher can only use images from the image library cache.")
        spec["image_name"] = path.name
        match = next((entry for entry in IMAGE_CATALOG
                      if Path(_catalog_entry_with_availability(entry)["cache_path"]).name == path.name), None)
        if match:
            # Let the flasher download the same catalog image if its own cache lacks it.
            spec["image_url"] = str(match["image_url"])
            spec["image_sha256"] = image_sha256 or str(match.get("image_sha256") or "")
    elif catalog_entry:
        spec["image_name"] = Path(_catalog_entry_with_availability(catalog_entry)["cache_path"]).name
    return spec


def _finish_job(job_id: str, stdout: str, stderr: str, exit_code: int) -> None:
    with JOBS_LOCK:
        JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        JOBS[job_id]["stdout"] = stdout[-50000:]
        JOBS[job_id]["stderr"] = stderr
        JOBS[job_id]["exit_code"] = exit_code
        JOBS[job_id]["status"] = "completed" if exit_code == 0 else "failed"


def _run_remote_prepare_job(
    job_id: str,
    form: Dict[str, Any],
    env: Dict[str, str],
    ssh_public_key: str,
    image_url: str,
    image_file: str,
    image_sha256: str,
    catalog_entry: Any,
) -> None:
    with JOBS_LOCK:
        header = str(JOBS[job_id].get("stdout") or "")
    try:
        spec: Dict[str, Any] = {
            "profile": form["profile"],
            "hostname": form["hostname"],
            "device": form["device"],
            "callback_url": form["callback_url"],
            "ssh_public_key": ssh_public_key,
            **_remote_image_spec(image_url, image_file, image_sha256, catalog_entry),
            "env": {key: value for key, value in env.items() if key.startswith("CLOUD_INIT_")},
        }
        remote_id = sd_flasher_client.submit_job(spec)
    except (ValueError, sd_flasher_client.FlasherError) as exc:
        logger.error(f"SD preparation could not be delegated: job_id={job_id}: {exc}")
        _finish_job(job_id, header, f"Host SD flasher refused the job: {exc}", 1)
        return

    header += f"Delegated to the host SD flasher (job {remote_id}).\n"
    with JOBS_LOCK:
        JOBS[job_id]["remote_job_id"] = remote_id
        JOBS[job_id]["stdout"] = header
    output = ""
    last_contact = time.monotonic()
    while True:
        time.sleep(REMOTE_POLL_SECONDS)
        try:
            remote = sd_flasher_client.get_job(remote_id)
        except sd_flasher_client.FlasherError as exc:
            if time.monotonic() - last_contact < REMOTE_LOST_CONTACT_SECONDS:
                continue
            logger.error(f"Lost contact with SD flasher: job_id={job_id}, remote_job_id={remote_id}")
            _finish_job(job_id, header + output, (
                f"Lost contact with the host SD flasher ({exc}). The card may still be in use; "
                "check the flasher before retrying."
            ), 1)
            _record_image_history(form, output, 1)
            return
        last_contact = time.monotonic()
        output = str(remote.get("output") or "")
        with JOBS_LOCK:
            JOBS[job_id]["stdout"] = (header + output)[-50000:]
        if remote.get("status") in {"completed", "failed"}:
            code = remote.get("exit_code")
            exit_code = code if isinstance(code, int) else 1
            break

    _finish_job(job_id, header + output, "", exit_code)
    if exit_code == 0:
        logger.info(f"SD preparation completed by host flasher: job_id={job_id}")
    else:
        logger.error(f"SD preparation failed on host flasher: job_id={job_id}, exit_code={exit_code}")
    _record_image_history(form, output, exit_code)
