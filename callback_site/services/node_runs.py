"""Node Ops execution: Ansible runs, hardware checks and the Telegraf check."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib import error as urlerror, parse as urlparse, request as urlrequest

from k3s_lightweight import profile_for_group as _k3s_profile_for_group

from services.network import _default_callback_url, _preferred_node_connect_target
from services.node_inventory import _node_ops_inventory_view
from services.node_state import (
    _load_node_config,
    _load_node_provisioning,
    _node_profile_inputs,
    _profile_map,
    _redact_profile_inputs,
    _save_node_provisioning,
)
from services.ssh_keys import (
    _ansible_known_hosts_args,
    _is_safe_key_name,
    _seed_known_host,
    _ssh_private_key_preflight,
)
from services.storage import BASE_DIR, INVENTORY_FILE, KEYS_DIR

logger = logging.getLogger(__name__)


def _run_node_profile(
    node_name: str,
    profile: Dict[str, Any],
    mode: str,
    run_ansible: bool,
    profile_inputs: Dict[str, str],
    node_group: str | None = None,
    node_address: str | None = None,
    node_username: str | None = None,
    node_certificate: str | None = None,
) -> Dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    profile_id = str(profile.get("id", ""))
    tags = profile.get("tags") or []

    logger.info(
        "Starting provisioning for node=%s profile=%s mode=%s run_ansible=%s",
        node_name,
        profile_id,
        mode,
        run_ansible,
    )

    if not run_ansible:
        return {
            "profile_id": profile_id,
            "status": "planned",
            "mode": mode,
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": "Ansible execution disabled for this run (plan-only).",
        }

    ansible_cmd = shutil.which("ansible-playbook")
    if not ansible_cmd:
        venv_ansible = BASE_DIR.parent / ".venv" / "bin" / "ansible-playbook"
        if venv_ansible.exists() and venv_ansible.is_file():
            ansible_cmd = str(venv_ansible)

    node_config = _load_node_config()
    ssh_user = str(node_username or "").strip() or str(node_config.get("username", "") or "").strip() or "jdizzle"
    cert_name = str(node_certificate or "").strip()
    ssh_key_path = str(KEYS_DIR / "rack-bootstrap-ed25519")
    if cert_name and _is_safe_key_name(cert_name):
        ssh_key_path = str(KEYS_DIR / cert_name)

    inventory_path = str(INVENTORY_FILE)
    limit_target = node_name
    temporary_inventory_path = None
    host_target = str(node_address or "").strip()
    if host_target:
        _seed_known_host(host_target)

    key_preflight_error = _ssh_private_key_preflight(ssh_key_path)
    if key_preflight_error:
        return {
            "profile_id": profile_id,
            "status": "failed",
            "mode": mode,
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": key_preflight_error,
        }

    resolved_group = str(node_group or "").strip()
    if host_target:
        known_hosts_args = _ansible_known_hosts_args()
        temp_inventory = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8")
        temp_inventory.write("all:\n")
        temp_inventory.write("  children:\n")
        family_group = resolved_group[:-len("_active")] if resolved_group.endswith("_active") else ""
        if family_group:
            # Nest the active group under its family group so family-level
            # group_vars (e.g. group_vars/rpi_3bplus) apply to the run.
            temp_inventory.write(f"    {family_group}:\n")
            temp_inventory.write("      children:\n")
            temp_inventory.write(f"        {resolved_group}:\n")
            temp_inventory.write("          hosts:\n")
            host_indent = "            "
            var_indent = "              "
        else:
            temp_inventory.write(f"    {resolved_group or 'dynamic_provisioning'}:\n")
            temp_inventory.write("      hosts:\n")
            host_indent = "        "
            var_indent = "          "
        temp_inventory.write(f"{host_indent}{node_name}:\n")
        temp_inventory.write(f"{var_indent}ansible_host: {host_target}\n")
        temp_inventory.write(f"{var_indent}ansible_user: {ssh_user}\n")
        temp_inventory.write(f"{var_indent}ansible_ssh_private_key_file: {ssh_key_path}\n")
        temp_inventory.write(f"{var_indent}ansible_ssh_common_args: '{known_hosts_args}'\n")
        temp_inventory.write(f"{var_indent}ansible_connection: ssh\n")
        temp_inventory.write(f"{var_indent}ansible_ssh_extra_args: '-o PreferredAuthentications=publickey'\n")
        temp_inventory.flush()
        temp_inventory.close()
        temporary_inventory_path = temp_inventory.name
        inventory_path = temporary_inventory_path
        limit_target = node_name

    cmd = [
        ansible_cmd or "ansible-playbook",
        "-i",
        inventory_path,
        "site.yml",
        "--limit",
        limit_target,
    ]
    env = os.environ.copy()
    env.pop("ANSIBLE_ASK_VAULT_PASS", None)
    env.pop("ANSIBLE_VAULT_IDENTITY_LIST", None)
    env.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)
    if tags:
        cmd.extend(["--tags", ",".join(str(t) for t in tags)])
    if mode == "check":
        cmd.append("--check")

    extra_vars_payload = {
        "provision_profile": profile_id,
        "provision_inputs": profile_inputs,
        "node_name": node_name,
    }
    cmd.extend(["--extra-vars", json.dumps(extra_vars_payload)])
    cmd_for_log = cmd[:-1] + ["<redacted-provision-inputs>"]

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=420,
            cwd=str(BASE_DIR.parent),
            env=env,
        )
        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        logger.info(
            "Completed provisioning for node=%s profile=%s mode=%s status=%s exit_code=%s",
            node_name,
            profile_id,
            mode,
            "ok" if proc.returncode == 0 else "failed",
            proc.returncode,
        )
        if proc.returncode != 0 and ("vault" in output.lower() or "password" in output.lower() or "prompt" in output.lower()):
            return {
                "profile_id": profile_id,
                "status": "failed",
                "mode": mode,
                "checked_at": checked_at,
                "command": cmd_for_log,
                "exit_code": proc.returncode,
                "inputs": _redact_profile_inputs(profile_id, profile_inputs),
                "output": output[-12000:] or "Ansible failed before provisioning completed. Check vault/password prompts and credentials.",
            }
        if proc.returncode != 0 and (
            "permission denied" in output.lower()
            or "unreachable!" in output.lower()
            or "failed to connect" in output.lower()
        ):
            ssh_hint = (
                "SSH authentication or reachability failed. Ensure the target node has the bootstrap SSH key installed "
                "for the expected user (often root or ops) and that the node is reachable at the configured address."
            )
            return {
                "profile_id": profile_id,
                "status": "failed",
                "mode": mode,
                "checked_at": checked_at,
                "command": cmd_for_log,
                "exit_code": proc.returncode,
                "inputs": _redact_profile_inputs(profile_id, profile_inputs),
                "output": f"{output[-12000:]}\n\n{ssh_hint}",
            }
        lowered_output = output.lower()
        if proc.returncode == 0 and (
            "skipping: no hosts matched" in lowered_output
            or "could not match supplied host pattern" in lowered_output
        ):
            # ansible-playbook exits 0 when the host pattern matches nothing, so no task ever ran.
            return {
                "profile_id": profile_id,
                "status": "failed",
                "mode": mode,
                "checked_at": checked_at,
                "command": cmd_for_log,
                "exit_code": proc.returncode,
                "inputs": _redact_profile_inputs(profile_id, profile_inputs),
                "output": (
                    f"{output[-12000:]}\n\nNo hosts matched, so nothing was applied to this node. "
                    "Check the node address and its inventory group."
                ),
            }
        return {
            "profile_id": profile_id,
            "status": "ok" if proc.returncode == 0 else "failed",
            "mode": mode,
            "checked_at": checked_at,
            "command": cmd_for_log,
            "exit_code": proc.returncode,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": output[-12000:],
        }
    except FileNotFoundError:
        return {
            "profile_id": profile_id,
            "status": "unavailable",
            "mode": mode,
            "checked_at": checked_at,
            "command": cmd_for_log,
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": "ansible-playbook command not found on host.",
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return {
            "profile_id": profile_id,
            "status": "timeout",
            "mode": mode,
            "checked_at": checked_at,
            "command": cmd_for_log,
            "exit_code": None,
            "inputs": _redact_profile_inputs(profile_id, profile_inputs),
            "output": ((stdout + "\n" + stderr).strip() or "Provisioning run timed out.")[-12000:],
        }


def _run_hardware_check_callback(node_name: str, node_group: str | None = None, node_address: str | None = None) -> Dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    host_target = str(node_address or "").strip()

    node_entry = next(
        (item for item in _node_ops_inventory_view() if isinstance(item, dict) and str(item.get("name", "")).strip() == node_name),
        {},
    )
    node_config = _load_node_config()
    ssh_user = str(node_entry.get("node_username", "") or "").strip() or str(node_config.get("username", "") or "").strip() or "jdizzle"
    cert_name = str(node_entry.get("certificate", "") or "").strip()
    callback_source_ip = str(node_entry.get("callback_connect_ip", "") or node_entry.get("callback_source_ip", "") or "").strip()
    host_target, target_source = _preferred_node_connect_target(node_name, host_target, callback_source_ip)
    if not host_target:
        return {
            "profile_id": "hardware_check_trigger",
            "status": "failed",
            "mode": "check",
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": {},
            "output": "Cannot trigger hardware-check callback because node address is empty.",
        }

    ssh_key_path = str(KEYS_DIR / "rack-bootstrap-ed25519")
    if cert_name and _is_safe_key_name(cert_name):
        ssh_key_path = str(KEYS_DIR / cert_name)

    _seed_known_host(host_target)

    key_preflight_error = _ssh_private_key_preflight(ssh_key_path)
    if key_preflight_error:
        return {
            "profile_id": "hardware_check_trigger",
            "status": "failed",
            "mode": "check",
            "checked_at": checked_at,
            "command": [],
            "exit_code": None,
            "inputs": {},
            "output": key_preflight_error,
        }

    known_hosts_args = _ansible_known_hosts_args()

    ansible_cmd = shutil.which("ansible")
    if not ansible_cmd:
        venv_ansible = BASE_DIR.parent / ".venv" / "bin" / "ansible"
        if venv_ansible.exists() and venv_ansible.is_file():
            ansible_cmd = str(venv_ansible)

    temp_inventory = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8")
    temp_inventory.write("all:\n")
    temp_inventory.write("  children:\n")
    temp_inventory.write(f"    {str(node_group or '').strip() or 'dynamic_provisioning'}:\n")
    temp_inventory.write("      hosts:\n")
    temp_inventory.write(f"        {node_name}:\n")
    temp_inventory.write(f"          ansible_host: {host_target}\n")
    temp_inventory.write(f"          ansible_user: {ssh_user}\n")
    temp_inventory.write(f"          ansible_ssh_private_key_file: {ssh_key_path}\n")
    temp_inventory.write(f"          ansible_ssh_common_args: '{known_hosts_args}'\n")
    temp_inventory.write("          ansible_connection: ssh\n")
    temp_inventory.write("          ansible_ssh_extra_args: '-o PreferredAuthentications=publickey'\n")
    temp_inventory.flush()
    temp_inventory.close()

    group_l = str(node_group or "").lower()
    profile_value = _k3s_profile_for_group(group_l, "pi5")
    callback_url = _default_callback_url()
    cmd = [
        ansible_cmd or "ansible",
        "-i",
        temp_inventory.name,
        node_name,
        "-m",
        "ansible.builtin.setup",
        "-a",
        "gather_subset=hardware,network",
        "-o",
    ]
    env = os.environ.copy()
    env.pop("ANSIBLE_ASK_VAULT_PASS", None)
    env.pop("ANSIBLE_VAULT_IDENTITY_LIST", None)
    env.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=150,
            cwd=str(BASE_DIR.parent),
            env=env,
        )
        raw_output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        fact_payload: Dict[str, Any] = {}
        if proc.returncode == 0:
            try:
                parsed = json.loads(raw_output.split("=>", 1)[1].strip()) if "=>" in raw_output else json.loads(raw_output)
                if isinstance(parsed, dict):
                    fact_payload = parsed.get("ansible_facts", {}) if isinstance(parsed.get("ansible_facts"), dict) else parsed
            except (ValueError, IndexError):
                fact_payload = {}

        if proc.returncode != 0 or not isinstance(fact_payload, dict):
            extra_hint = ""
            lowered = raw_output.lower()
            if "permission denied" in lowered:
                extra_hint += "\n\nSSH authentication failed (publickey/password). Verify username, key, and target host mapping."
            if "store now, decrypt later" in lowered or "post-quantum" in lowered:
                extra_hint += "\nThe post-quantum warning is informational; it is not the root cause of this SSH failure."
            return {
                "profile_id": "hardware_check_trigger",
                "status": "failed" if proc.returncode != 0 else "failed",
                "mode": "check",
                "checked_at": checked_at,
                "command": cmd,
                "exit_code": proc.returncode,
                "inputs": {},
                "output": (raw_output[-12000:] or "Ansible setup failed to return hardware facts.") + extra_hint + f"\nConnect target source: {target_source}.",
            }

        model = str(
            fact_payload.get("ansible_product_name")
            or fact_payload.get("ansible_model")
            or (fact_payload.get("ansible_lsb") or {}).get("description")
            or ""
        ).strip()
        ram_mb = 0
        raw_ram_value = fact_payload.get("ansible_memtotal_mb")
        if raw_ram_value is None:
            mem_mb = fact_payload.get("ansible_memory_mb")
            if isinstance(mem_mb, dict):
                real = mem_mb.get("real") if isinstance(mem_mb.get("real"), dict) else {}
                raw_ram_value = real.get("total") if isinstance(real, dict) else mem_mb.get("total")
        try:
            ram_mb = int(float(str(raw_ram_value or 0).strip()))
        except (TypeError, ValueError):
            ram_mb = 0

        device_info = fact_payload.get("ansible_devices") if isinstance(fact_payload.get("ansible_devices"), dict) else {}
        sd_capacity_gb = 0
        nvme_capacity_gb = 0
        for device_name, metadata in device_info.items():
            if not isinstance(metadata, dict):
                continue
            size_txt = str(metadata.get("size") or "").strip()
            value = re.search(r"(\d+(?:\.\d+)?)\s*(?:G|GB)", size_txt, flags=re.IGNORECASE)
            size_gb = 0
            if value:
                try:
                    size_gb = int(float(value.group(1)))
                except ValueError:
                    size_gb = 0
            if str(device_name).startswith("mmcblk") and size_gb:
                sd_capacity_gb = max(sd_capacity_gb, size_gb)
            if str(device_name).startswith("nvme") and size_gb:
                nvme_capacity_gb = max(nvme_capacity_gb, size_gb)
        nvme_present = any(str(name).startswith("nvme") for name in device_info)
        payload = {
            "instance_id": f"{node_name}-hw",
            "hostname": str(fact_payload.get("ansible_hostname") or node_name).strip() or node_name,
            "profile": profile_value,
            "source_ip": host_target,
            "stage": "hardware-check",
            "node_report_b64": "",
            "hardware": {
                "model": model or "unknown",
                "ram_mb": ram_mb,
                "sd_capacity_gb": sd_capacity_gb,
                "nvme": nvme_present,
                "nvme_capacity_gb": nvme_capacity_gb,
                "hat": False,
                "poe": False,
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urlrequest.Request(callback_url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urlrequest.urlopen(req, timeout=12) as resp:
                callback_response = resp.read().decode("utf-8", errors="replace").strip()
        except Exception as exc:
            callback_response = json.dumps({"ok": False, "error": str(exc)})

        output = callback_response or json.dumps({"ok": True, "facts": {"model": model, "ram_mb": ram_mb}})
        return {
            "profile_id": "hardware_check_trigger",
            "status": "ok",
            "mode": "check",
            "checked_at": checked_at,
            "command": cmd,
            "exit_code": 0,
            "inputs": {},
            "output": output[-12000:],
        }
    except FileNotFoundError:
        return {
            "profile_id": "hardware_check_trigger",
            "status": "unavailable",
            "mode": "check",
            "checked_at": checked_at,
            "command": cmd,
            "exit_code": None,
            "inputs": {},
            "output": "ansible command not found on host.",
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return {
            "profile_id": "hardware_check_trigger",
            "status": "timeout",
            "mode": "check",
            "checked_at": checked_at,
            "command": cmd,
            "exit_code": None,
            "inputs": {},
            "output": ((stdout + "\n" + stderr).strip() or "Hardware check trigger timed out.")[-12000:],
        }
    finally:
        try:
            Path(temp_inventory.name).unlink(missing_ok=True)
        except OSError:
            pass


def _apply_node_provisioning(node_name: str, profile_ids: List[str], mode: str, run_ansible: bool) -> Dict[str, Any]:
    profile_by_id = _profile_map()
    state = _load_node_provisioning()
    valid_profile_ids = [pid for pid in profile_ids if pid in profile_by_id]
    inputs_by_profile = _node_profile_inputs(node_name, state)
    node_view = {item.get("name"): item for item in _node_ops_inventory_view() if isinstance(item, dict) and item.get("name")}
    node_entry = node_view.get(node_name, {})
    node_group = str(node_entry.get("group", "") or "").strip()
    node_address = str(node_entry.get("address", "") or "").strip()
    callback_source_ip = str(node_entry.get("callback_connect_ip", "") or node_entry.get("callback_source_ip", "") or "").strip()
    preferred_target, _preferred_source = _preferred_node_connect_target(node_name, node_address, callback_source_ip)
    if preferred_target:
        node_address = preferred_target
    node_username = str(node_entry.get("node_username", "") or "").strip()
    node_certificate = str(node_entry.get("certificate", "") or "").strip()
    node_results: Dict[str, Any] = {}
    for pid in valid_profile_ids:
        profile_inputs = inputs_by_profile.get(pid, {})
        result = _run_node_profile(
            node_name,
            profile_by_id[pid],
            mode=mode,
            run_ansible=run_ansible,
            profile_inputs=profile_inputs,
            node_group=node_group or None,
            node_address=node_address or None,
            node_username=node_username or None,
            node_certificate=node_certificate or None,
        )
        node_results[pid] = result

    ordered_result_ids = list(valid_profile_ids)
    if mode == "check" and run_ansible:
        hardware_trigger_result = _run_hardware_check_callback(
            node_name,
            node_group=node_group or None,
            node_address=node_address or None,
        )
        node_results["hardware_check_trigger"] = hardware_trigger_result
        ordered_result_ids.append("hardware_check_trigger")

    state_results = state.setdefault("results", {})
    current_results = {pid: node_results[pid] for pid in ordered_result_ids if pid in node_results}
    state_results[node_name] = current_results
    _save_node_provisioning(state)

    return {
        "node": node_name,
        "mode": mode,
        "run_ansible": run_ansible,
        "results": [node_results[pid] for pid in ordered_result_ids if pid in node_results],
    }


def _telegraf_influx_dummy_check(inputs: Dict[str, str], timeout: int = 8) -> Dict[str, Any]:
    endpoint = str(inputs.get("influx_endpoint", "")).strip().rstrip("/")
    bucket = str(inputs.get("influx_bucket", "")).strip()
    org = str(inputs.get("influx_org", "")).strip()
    access_key = str(inputs.get("influx_access_key", "")).strip()
    secret_key = str(inputs.get("influx_secret_key", "")).strip()

    required = {
        "influx_endpoint": endpoint,
        "influx_bucket": bucket,
        "influx_org": org,
        "influx_access_key": access_key,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        return {
            "ok": False,
            "error": "missing required telegraf inputs",
            "missing": missing,
            "checks": [],
        }

    parsed = urlparse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "ok": False,
            "error": "invalid influx endpoint; expected http(s) URL",
            "checks": [],
        }

    checks: List[Dict[str, Any]] = []

    def _http_error_message(exc: Exception) -> str:
        if isinstance(exc, urlerror.HTTPError):
            body = ""
            try:
                body = exc.read(512).decode("utf-8", errors="replace").strip()
            except Exception:
                body = ""
            if body:
                return f"HTTP {exc.code}: {body}"
            return f"HTTP {exc.code}"
        return str(exc)

    health_url = f"{endpoint}/health"
    try:
        req = urlrequest.Request(health_url, headers={"Accept": "application/json"}, method="GET")
        with urlrequest.urlopen(req, timeout=timeout) as resp:  # nosec B310
            checks.append(
                {
                    "name": "health",
                    "ok": 200 <= int(getattr(resp, "status", 0) or 0) < 300,
                    "status": int(getattr(resp, "status", 0) or 0),
                    "url": health_url,
                }
            )
    except Exception as exc:
        checks.append(
            {
                "name": "health",
                "ok": False,
                "status": None,
                "url": health_url,
                "error": _http_error_message(exc),
            }
        )

    write_params = urlparse.urlencode({"org": org, "bucket": bucket, "precision": "s"})
    write_url = f"{endpoint}/api/v2/write?{write_params}"
    line = f"callback_site_dummy,source=node_ops,host={socket.gethostname()} value=1i {int(time.time())}"

    token_candidates: List[Dict[str, str]] = [{"label": "access_key", "token": access_key}]
    combo = f"{access_key}:{secret_key}"
    if secret_key:
        token_candidates.append({"label": "access_key:secret_key", "token": combo})

    write_ok = False
    auth_mode = ""
    last_write_error = ""
    last_status: int | None = None

    for candidate in token_candidates:
        try:
            req = urlrequest.Request(
                write_url,
                data=line.encode("utf-8"),
                headers={
                    "Authorization": f"Token {candidate['token']}",
                    "Content-Type": "text/plain; charset=utf-8",
                    "Accept": "application/json",
                },
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=timeout) as resp:  # nosec B310
                status = int(getattr(resp, "status", 0) or 0)
                last_status = status
                if status in {204, 202, 200}:
                    write_ok = True
                    auth_mode = candidate["label"]
                    checks.append(
                        {
                            "name": "dummy_write",
                            "ok": True,
                            "status": status,
                            "url": write_url,
                            "auth_mode": candidate["label"],
                        }
                    )
                    break
                last_write_error = f"unexpected status {status}"
        except Exception as exc:
            last_write_error = _http_error_message(exc)

    if not write_ok:
        checks.append(
            {
                "name": "dummy_write",
                "ok": False,
                "status": last_status,
                "url": write_url,
                "error": last_write_error or "write failed",
            }
        )

    ok = bool(checks and all(bool(c.get("ok")) for c in checks))
    return {
        "ok": ok,
        "endpoint": endpoint,
        "bucket": bucket,
        "org": org,
        "auth_mode": auth_mode,
        "checks": checks,
    }


def _send_webhook(url: str, payload: Dict[str, Any], verify_tls: bool = True) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    context = None
    if not verify_tls and url.startswith("https://"):
        import ssl

        context = ssl._create_unverified_context()
    try:
        with urlrequest.urlopen(req, timeout=10, context=context) as resp:  # nosec B310
            return {"ok": True, "status": resp.status}
    except (urlerror.URLError, TimeoutError) as exc:
        return {"ok": False, "error": str(exc)}
