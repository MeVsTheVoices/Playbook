"""Background Node Ops provisioning jobs."""

from __future__ import annotations

import threading
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from services.node_inventory import _node_ops_inventory_view
from services.node_runs import _apply_node_provisioning
from services.node_state import _load_node_provisioning, _missing_required_inputs, _profiles_to_run


PROVISION_JOBS: Dict[str, Dict[str, Any]] = {}


PROVISION_JOBS_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


def _start_node_provision_job(
    target_node_names: list[str], mode: str, run_ansible: bool,
    profile_filter: list[str] | None = None,
) -> str:
    job_id = uuid.uuid4().hex
    stamp = datetime.now(timezone.utc).isoformat()
    with PROVISION_JOBS_LOCK:
        PROVISION_JOBS[job_id] = {
            "id": job_id, "status": "queued", "mode": mode, "run_ansible": run_ansible,
            "target_nodes": list(target_node_names), "profile_filter": list(profile_filter or []),
            "results": [], "created_at": stamp, "updated_at": stamp,
        }
    try:
        threading.Thread(
            target=_run_node_provision_job,
            args=(job_id, target_node_names, mode, run_ansible, profile_filter),
            daemon=True,
        ).start()
    except RuntimeError as exc:
        with PROVISION_JOBS_LOCK:
            PROVISION_JOBS[job_id].update(status="failed", error=str(exc), finished_at=stamp, updated_at=stamp)
        logger.error("Could not start provisioning job %s: %s", job_id, exc)
        raise
    return job_id


def _run_node_provision_job(
    job_id: str,
    target_node_names: list[str],
    mode: str,
    run_ansible: bool,
    profile_filter: list[str] | None = None,
) -> None:
    started_at = datetime.now(timezone.utc).isoformat()
    with PROVISION_JOBS_LOCK:
        job = PROVISION_JOBS.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        job["started_at"] = started_at
        job["updated_at"] = started_at

    try:
        node_lookup = {
            item.get("name"): item
            for item in _node_ops_inventory_view()
            if isinstance(item, dict) and item.get("name")
        }
        results: list[Dict[str, Any]] = []
        if not target_node_names:
            raise ValueError("No target nodes were supplied.")
        for node_name in target_node_names:
            node = node_lookup.get(node_name)
            if not node:
                raise ValueError(f"Node {node_name} is no longer in inventory. Refresh before running again.")
            state = _load_node_provisioning()
            profile_ids = _profiles_to_run(node, state, profile_filter)
            if not profile_ids:
                raise ValueError(f"No provisioning profiles are available for node {node_name}.")
            if mode == "apply":
                missing = _missing_required_inputs(node_name, profile_ids, state)
                if missing:
                    with PROVISION_JOBS_LOCK:
                        job = PROVISION_JOBS.get(job_id)
                        if job:
                            job["status"] = "failed"
                            job["error"] = "missing required provisioning inputs"
                            job["missing_inputs"] = missing
                            job["finished_at"] = datetime.now(timezone.utc).isoformat()
                            job["updated_at"] = job["finished_at"]
                    return
            results.append(_apply_node_provisioning(node_name, profile_ids, mode=mode, run_ansible=run_ansible))
            with PROVISION_JOBS_LOCK:
                PROVISION_JOBS[job_id]["results"] = list(results)

        with PROVISION_JOBS_LOCK:
            job = PROVISION_JOBS.get(job_id)
            if job:
                job["status"] = "completed"
                job["results"] = results
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["updated_at"] = job["finished_at"]
    except Exception as exc:
        logger.exception("Provisioning job %s failed", job_id)
        with PROVISION_JOBS_LOCK:
            job = PROVISION_JOBS.get(job_id)
            if job:
                job["status"] = "failed"
                job["error"] = str(exc)
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["updated_at"] = job["finished_at"]
