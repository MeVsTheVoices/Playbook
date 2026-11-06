# Callback Site Functional Prototypes and Interdependencies

This document defines strict functional prototypes (inputs, outputs, side effects) for the callback-site app and maps the major cross-file dependencies.

## 1) Runtime Entry Points

- Flask service entry: callback_site/app.py (logging, app object, route registration)
- Route modules, one per area: callback_site/routes/ (pages, openwrt, settings, callbacks, sd, images, ssh, test_terminal, monitor, nodes)
- Domain logic: callback_site/services/ (see the module map below)
- Ansible execution entry: callback_site/services/node_runs.py via subprocess calls to ansible-playbook
- SD prep execution entry: scripts/prepare_sd_cloud_init.sh and scripts/lib/*.sh (invoked by callback_site/services/sd_prepare.py)

## 2) Backend Prototype Contracts (Python)

These are the highest-impact contracts that the UI and automation flows depend on.

### Monitoring and Node Summary

- _monitor_nodes_summary() -> Dict[str, Any]
  - Inputs: none (loads callback history and monitor config from disk)
  - Output keys: nodes, cloud_init_options, node_cloud_init
  - Side effects: none
  - Depends on: callbacks.jsonl, node_monitor_state.json, node_provisioning.json

- _node_ops_inventory_view() -> List[Dict[str, Any]]
  - Inputs: none (reads inventory + callback + provisioning state)
  - Output: enriched node rows for Node Ops UI
  - Side effects: none
  - Depends on: inventory.yml parse, callbacks.jsonl, node_provisioning.json

### Provisioning Execution

- _apply_node_provisioning(node_name: str, profile_ids: List[str], dry_run: bool = False, check_mode: bool = False, source: str = "manual") -> Dict[str, Any]
  - Inputs: target node, selected profile IDs, run mode flags
  - Output keys: ok, node_name, applied_at, profiles, source, results
  - Side effects: writes node_provisioning.json results
  - Depends on: _run_node_profile(), _run_hardware_check_callback() in check mode

- _run_node_profile(node_name: str, profile: Dict[str, Any], check_mode: bool = False, dry_run: bool = False) -> Dict[str, Any]
  - Inputs: node and profile metadata
  - Output keys: profile_id, command, returncode, stdout, stderr, changed
  - Side effects: invokes ansible-playbook subprocess
  - Depends on: generated inventory snippet + profile segment synthesis

### Durable State Writes (Strict Atomic Persistence)

All these functions now use atomic write semantics:

- _save_sd_option_profiles(profiles: Dict[str, Dict[str, Any]]) -> None
- _save_profile_segments(segments: Dict[str, List[Dict[str, Any]]]) -> None
- _save_monitor_state(state: Dict[str, Any]) -> None
- _save_node_config(payload: Dict[str, Any]) -> Dict[str, Any]
- _save_node_provisioning(state: Dict[str, Any]) -> None
- _write_image_history(entries: List[Dict[str, Any]]) -> None
- _reset_node_ops_settings() -> Dict[str, Any] (for config/provision reset files)

Atomic baseline helper:

- _atomic_write_text(path: Path, text: str) -> None
  - Contract: write to temp file in same directory, then replace target
  - Practical guarantee: avoids partial JSON writes during crashes/interruption

## 3) API Route Contracts (Flask)

Route handlers live in callback_site/routes/, one module per area, each exposing a register_*_routes(app) function that app.py calls:

- routes/pages.py, routes/openwrt.py, routes/settings.py: pages and fragments, OpenWrt helpers, settings/logging/health/Tailscale
- routes/callbacks.py: cloud-init callback, events, node reports
- routes/sd.py, routes/images.py, routes/ssh.py: SD prepare and devices, image library and catalog pulls, SSH keys
- routes/test_terminal.py, routes/monitor.py, routes/nodes.py: verifier terminal, hardware monitor, Node Ops

Routes call helpers in callback_site/services/ for state and business logic. Names below such as _apply_node_provisioning()
are in services/node_runs.py, _monitor_nodes_summary() in services/node_views.py, _ping_status() in services/node_inventory.py,
and _run_prepare_job() in services/sd_prepare.py.

### Monitor/API

- GET /api/monitor/summary
  - Returns: monitor node summary envelope
  - Depends on: _monitor_nodes_summary()

- POST /api/monitor/nodes/<node_name>/ping
  - Returns: reachable, latency_ms, error
  - Depends on: _ping_status(), monitor node lookup

- POST /api/nodes/provision
  - Starts provisioning/check operation and returns a job id
  - Depends on: _apply_node_provisioning() job worker wrapper

### SD/API

- POST /api/sd/prepare
  - Starts SD preparation background job
  - Depends on: _run_prepare_job() -> scripts/prepare_sd_cloud_init.sh

- GET /api/sd/catalog-pull/jobs/<job_id>
  - Returns async image-catalog pull job state
  - Depends on: in-memory job table + persisted image history where needed

## 4) UI Interdependencies

### callback_site/templates/index.html

- Home dashboard and SD preparation orchestration
- Pulls from:
  - /api/cloud-init/events
  - /api/sd/catalog-pull and job status endpoint
  - /api/sd/options-profiles
  - /api/sd/images CRUD endpoints

### callback_site/templates/nodes.html

- Node Ops workflow and provisioning launch
- Pulls from:
  - /api/nodes/summary
  - /api/nodes/profiles
  - /api/nodes/node-inputs
  - /api/nodes/provision and /api/nodes/provision/jobs/<job_id>
- Optional node profiles can add storage-oriented behavior such as Longhorn. The first-pass implementation assumes Pi 5 nodes still boot from SD card, but the Longhorn storage path remains configurable so future NVMe-backed data paths can be swapped in without changing the operator flow.

### callback_site/templates/monitor.html

- Monitoring and close-check controls
- Pulls from:
  - /api/state/source-of-truth
  - /api/monitor/nodes/<node>/ping
  - /api/nodes/provision and job status endpoint for check-closer actions

### callback_site/templates/kubernetes.html

- Kubernetes runtime and cluster snapshot page
- Pulls from:
  - /api/kubernetes/summary
- The summary now includes a Longhorn readiness signal based on the `longhorn-system` namespace and pods so operators can confirm whether storage automation is present and healthy.

## 5) Data and State Dependency Map

- callback_site/data/callbacks.jsonl
  - Source of truth for cloud-init callback timeline and profile backfill
  - Read by monitor and node summary builders

- callback_site/data/node_provisioning.json
  - Node configs, selected profiles, per-node inputs, latest run results
  - Read/write path touches nodes + monitor contexts

- callback_site/data/node_monitor_state.json
  - Managed monitor-node metadata and cloud-init option definitions

- callback_site/data/node_monitor_config.json
  - Global node operation auth/webhook settings

- callback_site/data/image_download_catalog.jsonl
  - SD image pull history used in Home/Images views

- callback_site/data/sd_option_profiles.json
  - Optional cloud-init profile templates referenced in SD and provisioning flows

- callback_site/data/connection_profiles.json
  - Every connection profile seen per node (callback, inventory, tailnet, dns, hostname) with first/last seen and reachability
  - Written by the monitor poll; read by GET /api/nodes/<node>/connection-profiles

- callback_site/data/boot_logs/<node>/
  - Last 10 boot logs per node, pulled over SSH or pushed by the node

## 5b) Connection Profiles (committed direction, in development)

How Node Ops reaches a node is a replaceable strategy, not a hard-coded rule:

- services/connection_profiles.py gathers a list of profiles per node, then a strategy picks one. `prefer_tailnet` (default) and `lan_first` exist; add one with `register_strategy(name, fn)` and select it with `CALLBACK_CONNECTION_STRATEGY`.
- `_preferred_node_connect_target` in services/network.py is now a thin wrapper, so existing callers keep working.
- Next steps, all planned: callers (Ansible runs, hardware check, boot logs, terminal) take the whole profile (user, port, key) instead of only an address; per-node pinned profile; try the next profile when one fails; a Node Ops view to inspect and edit profiles; more transports (Tailscale SSH, bastion).

## 6) Practical Functional Rules (Strict Mode)

- Read functions should not mutate persisted state.
- Write functions for JSON/JSONL state must use _atomic_write_text.
- API handlers should keep validation at the boundary and return explicit status fields.
- Background jobs must report terminal states with enough context for UI polling to show actionable errors.

## 7) High-Impact Overlooked Changes Addressed

Completed in this pass:

- Unified persistence writes for key state files to atomic replacement semantics.
- Removed partial-write risk from image history rewrites.

Remaining caution:

- Append-only writes (_append_image_history) are intentionally append-based; if stricter durability is required, switch to write-lock + read/merge/atomic-write pattern.
