"""SD-card prepare payload sanitising, env mapping and the prepare job runner."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import threading
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

from services.images import _record_image_history
from services.k3s_config import _default_k3s_config, _normalize_k3s_node_name
from services.kubernetes import _local_k3s_version
from services.ssh_keys import _archive_ssh_public_key
from services.storage import INVENTORY_FILE, SCRIPT_PATH

logger = logging.getLogger(__name__)


JOBS: Dict[str, Dict[str, Any]] = {}


JOBS_LOCK = threading.Lock()


def _ensure_prereqs() -> List[str]:
    errors: List[str] = []
    if not SCRIPT_PATH.exists():
        errors.append(f"Missing script: {SCRIPT_PATH}")
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
        "callback_url",
        "image_url",
        "image_file",
        "image_sha256",
        "workspace",
        "sd_options_profile",
        "pi_only",
        "cloud_init_options",
    }
    cleaned: Dict[str, Any] = {}
    for key in allowed_keys:
        if key in payload:
            cleaned[key] = payload.get(key)
    if not isinstance(cleaned.get("cloud_init_options"), dict):
        cleaned["cloud_init_options"] = {}
    cleaned["sd_options_profile"] = str(cleaned.get("sd_options_profile", "")).strip()
    cleaned["pi_only"] = bool(cleaned.get("pi_only", False))
    cloud_options = cleaned["cloud_init_options"]
    distribution = str(cleaned.get("distribution", "standard")).strip().lower() or "standard"
    k3s_config = str(cloud_options.get("k3s_config", "") or "").strip()
    if distribution != "k3s" and k3s_config:
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
    return cleaned


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
    """Copy of SD cloud-init options without the Tailscale auth key.

    Job records and auto-saved templates are readable through the unauthenticated API
    and stored in plaintext; an auth key can join tagged devices, so it never goes there.
    """
    if not isinstance(options, dict):
        return {}
    return {key: value for key, value in options.items() if key != "tailscale_auth_key"}


def _map_cloud_init_options_to_env(env: Dict[str, str], distribution: str, cloud_init_options: Dict[str, Any]) -> List[str]:
    enabled_options: List[str] = []

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
        workspace = form.get("workspace", "").strip()
        if image_url:
            cmd.extend(["--image-url", image_url])
        if image_file:
            cmd.extend(["--image-file", image_file])
        if image_sha256:
            cmd.extend(["--image-sha256", image_sha256])
        if workspace:
            cmd.extend(["--workspace", workspace])

        run_cmd: List[str]
        if os.geteuid() == 0:
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
        distribution = str(form.get("distribution", "standard")).strip().lower() or "standard"
        env["CLOUD_INIT_DISTRIBUTION"] = distribution

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

        _record_image_history(form, final_stdout, proc.returncode)
