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

## Additional Operator Pages

The app now includes a lightweight OpenWrt workflow for collecting the connection details needed for Ansible-based router access:
- `/openwrt` renders a small operator form for host, port, username, password, connection mode, and module hint.
- `POST /api/openwrt/inspect` returns an Ansible-style inventory preview plus a live SSH probe result.
- The page is intentionally simple and is meant to help you inspect router login details before wiring up a full playbook.

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

Telegraf bootstrap options (Home -> Hardware Detection and Telemetry):
- `install_telegraf`: installs Telegraf and runs a dedicated bootstrap script on first boot.
- `telegraf_config_url`: if provided, node pulls `telegraf.conf` from this URL.
- `telegraf_influx_token`: optional bearer token used for authenticated config pull (`Authorization: Token ...`).
- `telegraf_influx_url`, `telegraf_influx_org`, `telegraf_influx_bucket`: fallback mode if `telegraf_config_url` is empty; node renders a local `telegraf.conf` using these credentials.

Telegraf bootstrap order:
1. Install package (if enabled).
2. Pull config from `telegraf_config_url` when present.
3. Otherwise generate local config from Influx URL/token/org/bucket when all are provided.
4. Enable and restart `telegraf` service.

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

OpenWrt probe:
- `GET /openwrt`
- `POST /api/openwrt/inspect`

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

### Node Ops operational notes
- The live provisioning path prefers the current node address over hostname-based inventory values when the node is reachable by IP.
- For environments without local DNS or an `/etc/hosts` entry, use the node IP in the Node Ops UI and let the temporary inventory generated for provisioning target the address directly.
- Provisioning is intentionally non-interactive: the app strips prompt-driven environment variables and uses the bootstrap SSH identity and private key path that match the node's initial cloud-init setup.
- Save node-specific input values before running `check` or `apply`; missing required fields return `400` with a `missing_inputs` payload.
- When `run_ansible=true`, the workflow executes `ansible-playbook` with the selected profile tags; when `false`, it records the intended run without executing it.

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
- Image download catalog source: `callback_site/catalogs/image_catalog.yml`
- Node monitor config: `callback_site/data/node_monitor_config.json`
- Generated SSH keys: `callback_site/data/ssh_keys/`
- Per-run archived SSH public keys: `callback_site/data/ssh_key_archive/`
- Per-run key archive index: `callback_site/data/ssh_key_archive.jsonl`
- SD optional settings templates: `callback_site/data/sd_option_profiles.json`

## Logging

Comprehensive logging is built in for multi-node provisioning visibility and troubleshooting.

### Log Output and Location

- **Log file**: `callback_site/data/logs/callback_site.log` (or `$CALLBACK_APPDATA_DIR/logs/callback_site.log` if custom appdata directory is set)
- **Console output**: All logs are also printed to stdout/stderr in real time
- **Log level**: INFO by default, DEBUG when verbose mode is enabled

### Log Rotation

Logs are automatically rotated to prevent unbounded disk growth:
- **Max file size**: 10 MB per log file
- **Retention**: Keeps 10 rotated backups (`.log.1`, `.log.2`, etc.) = ~100 MB total retention
- **Automatic rollover**: When current log reaches 10 MB, it's renamed and a new one is created

Example log file sequence after rotation:
```
callback_site.log      (current, < 10 MB)
callback_site.log.1    (10 MB)
callback_site.log.2    (10 MB)
...
callback_site.log.10   (10 MB, oldest)
```

### What Gets Logged

**Callback events**:
- Incoming callback hostname, stage (e.g., `phone_home`, `hardware-check`), instance_id, source IP
- Hardware detection data when reported (Pi model, RAM, storage, HAT, PoE presence)

**SD card preparation**:
- Job start: hostname, profile, device, enabled cloud-init options
- Job completion: success/failure, exit code
- Permission errors if app is not running as root

**Catalog operations**:
- Catalog pull job start: number of entries, skip_existing flag
- Catalog pull completion: OK count, failed count, skipped count

**System diagnostics**:
- App startup/shutdown
- Configuration loading
- Error conditions and exceptions

### Enabling Verbose Logging

**Via Web UI** (Settings page):
- Click "Enable Verbose Logging" button
- All future logs will include DEBUG-level messages
- Click "Disable Verbose Logging" to return to INFO level

**Via Environment** (at startup):
- (Currently INFO by default; future enhancement: support `LOG_LEVEL=DEBUG` env var)

**View Recent Logs**:
- Settings page: "View Recent Logs (100 lines)" button fetches and displays the last 100 log lines
- API endpoint: `GET /api/logging/tail?lines=50` (customize line count)
- Manual inspection: `tail -f callback_site/data/logs/callback_site.log`

### Log Format

```
2026-07-01 13:02:09,740 - core - INFO - Callback received: hostname=rpi5-01, stage=phone_home, instance_id=iid-clear-01, source_ip=127.0.0.1
2026-07-01 13:02:09,788 - core - INFO - Catalog pull job started: job_id=job-test, entries=5, skip_existing=False
2026-07-01 13:02:09,789 - core - INFO - Catalog pull job completed: job_id=job-test, ok=5, failed=0, skipped=0
```

### Troubleshooting

**Logs not appearing**:
- Verify app is running as root or has write permission to `CALLBACK_APPDATA_DIR/logs/`
- Check stderr for startup errors

**Logs fill disk quickly**:
- Verbose logging (DEBUG) produces ~10x more output
- Review production deployments for DEBUG mode accidentally left enabled
- Adjust rotation settings in `app.py` if needed (maxBytes and backupCount)

**Recovering logs after rotation**:
- All rotated files are retained with `.1`, `.2`, etc. suffixes
- Search across all logs: `grep "pattern" callback_site/data/logs/callback_site.log*`

## Local Persistence (AppData Style)

This app persists state to a local appdata directory.

- Default path: `callback_site/data/`
- Override path with environment variable: `CALLBACK_APPDATA_DIR`
- Everything that should survive restarts or container recreation lives under this directory, including logs, callback state, image history, SSH key material, node provisioning state, and SD option templates.

Example (local custom path):

```bash
CALLBACK_APPDATA_DIR=/home/jdizzle/appdata/callback_site sudo PORT=9821 .venv/bin/python3 app.py
```

Example Docker-style mount:

```bash
mkdir -p /srv/appdata/callback_site
docker run -d \
  --name callback-site \
  -e CALLBACK_APPDATA_DIR=/data \
  -v /srv/appdata/callback_site:/data \
  -p 9821:9821 \
  your-image:latest
```

This mirrors a simple Docker volume pattern (host path mounted as appdata) without requiring any other changes.

For containerized deployments, map your host Docker volume to `CALLBACK_APPDATA_DIR` so background catalog pulls persist downloaded images across restarts and operator-edited state remains available after upgrades or container rebuilds.
