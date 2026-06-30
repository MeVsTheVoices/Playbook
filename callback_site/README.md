# Cloud-Init Callback Site

This Flask service provides two operator workflows:
- Cloud-init callback ingest and event visibility.
- Browser-driven SD card imaging that executes locally on the host.

## Quick Start

1. Create environment and install dependencies.

```bash
cd callback_site
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Run as root for SD flashing workflow (recommended).

```bash
sudo PORT=9821 .venv/bin/python3 app.py
```

3. Open:
- http://127.0.0.1:9821/
- API events: http://127.0.0.1:9821/api/cloud-init/events

## SD Card Imager Workflow

The UI supports end-to-end preparation of cloud-init capable images:
- Profile-aware device targeting (`pi5`, `pibplus`).
- Distro catalog with filtering by family, release, and search.
- Optional SHA256 verification input.
- SSH key generation and direct key download links.
- Live job output with compacted progress lines.
- The dashboard is split into smaller template fragments, with htmx-backed row refreshes for the image catalog, retrieved image manager, and callback event table.
- Retrieved image records support create, update, and delete actions from the UI.
- Manual catalog pulls can download selected catalog images in the background (bulk mode) without flashing.
- Image management is hosted on the dedicated `/images` page; the callback dashboard keeps links/references.
- The `/images` page visually indicates whether catalog and managed images are locally available.
- The `/images` page treats catalog availability as file-backed state and also considers previously recorded successful downloads, so availability reflects the current cache plus persisted history.
- Ubuntu Raspberry Pi presets are pinned to `old-releases.ubuntu.com` because several `cdimage.ubuntu.com` release URLs now return HTTP `403` during automated pulls.

Cloud-init optional settings on Home:
- The Home page (`/`) is the source of truth for cloud-init optional setting templates used during SD prep.
- Operators can save, load, and delete reusable templates and include template data in `POST /api/sd/prepare` requests.
- This keeps first-boot cloud-init authoring close to the imaging workflow and avoids context switching.

### Cloud-Init Template Workflow (Home)

Recommended operator flow:
1. Fill optional settings segments needed for this image run.
2. Verify the configured field count badge reflects expected values.
3. Save as a reusable template with a stable name (for example `pi5-baseline-lab`).
4. For future runs, select and load a template, then make run-specific edits.
5. Use `Reset Optional Fields` before switching between materially different node roles.

Template naming best practices:
- Prefer `<profile>-<role>-<site>` naming for easy scanning.
- Keep template descriptions concise and operationally meaningful.
- Use one baseline template per node class and layer overrides at run time.

The underlying script is:
- `scripts/prepare_sd_cloud_init.sh`

### Hash Verification Behavior

- If expected SHA256 is provided (or discovered), the script verifies the image.
- If no expected hash is available, imaging continues and logs that hash validation was skipped.

### Download Progress Behavior

- Download progress is emitted in coarse increments to keep logs readable.
- The UI replaces prior progress lines so output appears as a smooth single metric stream.

## Security and Safety

- SD write operations are destructive and erase the selected target device.
- Run this service only on trusted networks.
- Prefer running the app as root instead of granting broad sudoers exceptions.

## UI Ownership Best Practices

Keep each page focused on one operational concern:
- Home (`/`): imaging + first-boot cloud-init authoring (including optional template selection/reuse).
- Monitor (`/monitor`): runtime node inventory, callback/certificate visibility, and operational monitoring.
- Nodes (`/nodes`): post-boot provisioning actions and checks.

Implementation guidance:
- Avoid duplicating major workflows across pages.
- If a feature appears on multiple pages, define one page as authoritative and link from others.
- Add render-level tests for page ownership to prevent accidental UI drift.

## API Endpoints

Core:
- `GET /health`
- `GET /settings`
- `GET /images`
- `GET /api/settings`
- `POST /api/settings`
- `POST /api/cloud-init/callback`
- `GET /api/cloud-init/events`

SD workflow:
- `GET /api/sd/devices`
- `POST /api/sd/prepare`
- `GET /api/sd/jobs/<job_id>`
- `GET /api/sd/catalog`
- `POST /api/sd/catalog-pull`
- `GET /api/sd/catalog-pull/jobs/<job_id>`
- `GET /api/sd/options-profiles`
- `POST /api/sd/options-profiles`
- `DELETE /api/sd/options-profiles/<profile_name>`

SSH key workflow:
- `POST /api/ssh/generate`
- `GET /api/ssh/keys`
- `GET /api/ssh/download/<key_name>/private`
- `GET /api/ssh/download/<key_name>/public`
- `POST /api/ssh/generate` supports `mode: generate|kload`.
- `generate` creates a new key and returns `409` if the key name already exists.
- `kload` loads an existing key by name (or creates one if missing).

Node Ops:
- `GET /api/nodes/summary`
- `POST /api/nodes/config`
- `POST /api/nodes/monitor`
- `GET /api/nodes/profiles`
- `POST /api/nodes/node-configs`
- `POST /api/nodes/node-inputs`
- `POST /api/nodes/provision`

Settings:
- `POST /api/settings/reset` (`scope: node_ops`) to clean-slate Node Ops credentials, node profile selections, saved per-profile inputs, and provisioning results.
- `POST /api/cloud-init/events/clear` to clear stored callback backlog events from the dashboard feed.

### Node Ops Workflow (Post-Callback)

The Node Ops tab is designed as the next stage after callback visibility:
- Pull active nodes and their latest callback + reachability state.
- Node addressing in Node Ops is inventory-backed but callback-enriched.
: If inventory contains placeholder values (for example `CHANGE_ME_*`) and a node has checked in, Node Ops prefers callback `source_ip` for live checks.
- Assign a per-node set of provisioning profiles (baseline, tailscale, telegraf, docker, kubernetes).
- Save profile-specific values (for example, Telegraf endpoint/bucket/org/access key/secret key and Tailscale auth key/tailnet).
- Run a closer check (`mode=check`) for one node or all active nodes.
- Optionally apply provisioning (`mode=apply`) to a selected node.

Required profile inputs are validated before check/apply runs:
- Missing required fields return `400` from `POST /api/nodes/provision` with a `missing_inputs` payload.
- Save/update node values first with `POST /api/nodes/node-inputs`.
- `GET /api/nodes/summary` and `GET /api/nodes/profiles` return `node_inputs` to hydrate the UI.

Provisioning execution modes:
- `run_ansible=true`: runs `ansible-playbook` with `--limit <node>` and profile tags.
- `run_ansible=false`: stores planned checks without execution (safe planning mode).

Node profile and check state is persisted in:
- `callback_site/data/node_provisioning.json`

Fragment endpoints:
- `GET /fragments/sd/image-catalog-rows`
- `GET /fragments/sd/images-rows`
- `GET /fragments/cloud-init/event-rows`
- `GET /fragments/tailscale-acl/overview`
- `GET /fragments/tailscale-acl/inputs`
- `GET /fragments/tailscale-acl/output`
- `GET /fragments/tailscale-acl/apply-guide`

Tailscale ACL planner loading model:
- `/tailscale-acl` now serves a lightweight shell first.
- Section panels are fetched progressively using htmx, so first paint is fast and deeper sections hydrate in short staged delays.
- ACL defaults are rack-aware and use the saved rack name from Settings as the default homelab tag basis.

Tests:
- `python -m unittest discover -s tests`

### Basic Testing

Automated:
- `cd callback_site`
- `.venv/bin/python -m unittest discover -s tests -v`

Manual smoke checks:
- Start app: `.venv/bin/python3 app.py`
- Dashboard: `curl -i http://127.0.0.1:8088/`
- Health: `curl -s http://127.0.0.1:8088/health`
- Events API: `curl -s http://127.0.0.1:8088/api/cloud-init/events`
- Fragment rows:
	- `curl -i http://127.0.0.1:8088/fragments/cloud-init/event-rows`
	- `curl -i http://127.0.0.1:8088/fragments/sd/image-catalog-rows`
	- `curl -i http://127.0.0.1:8088/fragments/sd/images-rows`

Callback source IP sanity check:
- `curl -s -X POST http://127.0.0.1:8088/api/cloud-init/callback -H 'Content-Type: application/json' -H 'X-Forwarded-For: 203.0.113.10, 10.0.0.2' -d '{"hostname":"test-host","instance_id":"iid-test","profile":"pi5","stage":"phone_home"}'`
- Confirm `source_ip` is `203.0.113.10` in `GET /api/cloud-init/events`.

Environment note:
- Run the callback app with `callback_site/.venv` binaries (for example `.venv/bin/python3 app.py` from `callback_site/`).
- The repository-root venv (`/home/jdizzle/Playbook/.venv`) may not include callback-site Flask dependencies.

### Regression Testing Focus

When changing Home/Monitor boundaries or cloud-init template behavior, verify:
- Home page renders cloud-init template controls (save/load/reset/delete).
- Monitor page does not render template-builder controls.
- `POST /api/sd/prepare` queues `sd_options_profile` and `cloud_init_options` metadata.
- Profile CRUD APIs continue to pass (`GET/POST/DELETE /api/sd/options-profiles`).
- Node Ops summary and monitor status prefer callback IP for inventory placeholder addresses.

Quick run:
- `cd /home/jdizzle/Playbook`
- `/home/jdizzle/Playbook/.venv/bin/python -m unittest callback_site/tests/test_app.py`

## Data Files

- Callback event log: `callback_site/data/callbacks.jsonl`
- Catalog pull image cache: `callback_site/data/image_cache/`
- Node monitor config: `callback_site/data/node_monitor_config.json`
- Generated SSH keys: `callback_site/data/ssh_keys/`
- Per-run archived SSH public keys: `callback_site/data/ssh_key_archive/`
- Per-run key archive index: `callback_site/data/ssh_key_archive.jsonl`
- SD optional settings templates: `callback_site/data/sd_option_profiles.json`

## Local Persistence (AppData Style)

This app persists state to a local appdata directory.

- Default path: `callback_site/data/`
- Override path with environment variable: `CALLBACK_APPDATA_DIR`

Example (local custom path):

```bash
CALLBACK_APPDATA_DIR=/home/jdizzle/appdata/callback_site sudo PORT=9821 .venv/bin/python3 app.py
```

This mirrors a simple Docker volume pattern (host path mounted as appdata) without requiring any other changes.

For containerized deployments, map your host Docker volume to `CALLBACK_APPDATA_DIR` so background catalog pulls persist downloaded images across restarts.
