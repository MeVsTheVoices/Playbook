# Callback Site Operator Portal

Callback Site is the control plane for the rack workflow: cloud-init bootstrap, SD-card flashing, callback tracking, image catalog management, post-boot node provisioning, and the shared monitoring views that tie those steps together.

## At A Glance

- SD-card imaging for `pi5`, `pi4` and `pi3bplus` profiles.
- Cloud-init authoring with reusable optional-setting templates.
- Distribution-aware imaging for `standard` and `k3s` flows.
- Catalog management with cache checks and bulk pulls (select several presets, or pull everything missing).
- Callback, hardware-monitor, and Node Ops views backed by one shared source-of-truth payload.
- Boot log exchange: nodes flashed from this site send their own boot logs (journal, cloud-init, dmesg) to `/api/cloud-init/boot-log` after the final callback and on every power restore, which works behind NAT. For other nodes the site pulls them over SSH shortly after a callback (skipped when a push just arrived; `CALLBACK_AUTO_BOOT_LOGS=0` turns the automatic pull off). The last 10 per node are kept.
- Node Ops connects to a node at its tailnet address when `tailscale status` on the site host lists it online, ahead of the LAN/NAT address a callback reports; the monitor pings the same target.
- OpenWrt routers as Node Ops hosts (group `openwrt`) with SSH probe and key push, SSH key management, and Tailscale ACL planning.
- Optional Pi 5 Longhorn provisioning for k3s nodes, with a configurable storage path so a later SD-boot to NVMe-boot/storage move can be handled by changing the path value instead of rewriting the workflow.

## Runtime Contract

Everything that should survive restarts or container recreation lives under `CALLBACK_APPDATA_DIR`.

- Default path: `callback_site/data/`
- Override path with `CALLBACK_APPDATA_DIR`
- The app can re-exec through `sudo` only when `CALLBACK_SITE_REQUIRE_ROOT=1` is set.
- The app stays normal-user friendly by default; elevated startup is opt-in.

## Quick Start

From the repo root:

```bash
cd /home/jdizzle/Playbook
.venv/bin/python3 callback_site/app.py
```

For SD flashing or other root-required paths:

```bash
sudo -E /home/jdizzle/Playbook/.venv/bin/python3 callback_site/app.py
```

Open:

- http://127.0.0.1:9821/
- http://127.0.0.1:9821/images
- http://127.0.0.1:9821/nodes
- http://127.0.0.1:9821/monitor

## Pages

- `/` — SD image prep, SSH keys, optional cloud-init settings, and saved templates.
- `/images` — cached and downloadable image catalog with bulk pull jobs.
- `/monitor` — callback event feed, hardware snapshots, and certificate pull helpers.
- `/nodes` — node inventory, saved inputs, per-module configuration with remembered/named configurations, and provisioning check/apply.
- `/openwrt` — redirects to the OpenWrt routers panel in `/nodes`.
- `/tailscale-acl` — rack tailnet policy builder (grants, K3s ports, SSH, routes, tests).

## Longhorn Notes

- Longhorn is intentionally wired as an optional Pi 5 profile for now.
- The first pass assumes the nodes still boot from SD card.
- The storage path is configurable so you can later move Longhorn data to an NVMe-backed mount without changing the operator flow.
- The K3s dashboard reports whether the `longhorn-system` namespace and pods are present and healthy.

## Architecture

- `callback_site/app.py` is the Flask entrypoint: logging, the app object and route registration.
- `callback_site/routes/` holds the HTTP endpoints, one module per area (pages, openwrt, settings, callbacks, sd, images, ssh, test_terminal, monitor, nodes). Each exposes a `register_*_routes(app)` function.
- `callback_site/services/` holds the domain logic, layered so imports only point downward:
  - `storage`, `settings`, `sd_profiles`, `ssh_keys`, `openwrt`, `network`: paths, settings, keys and addressing.
  - `events`, `images`, `image_catalog`, `reports`, `boot_logs`: callback events, the image library, catalog pulls and node boot logs.
  - `node_state`, `node_inventory`, `node_runs`, `node_views`: Node Ops state, inventory, Ansible runs and read models.
  - `sd_prepare`, `k3s_config`, `sd_devices`, `kubernetes`, `terminal_checks`, `provision_jobs`: SD preparation, config checks, devices, cluster summary and background jobs.
- `callback_site/k3s_lightweight.py` (board and role registry) and `callback_site/tailscale_policy.py` (policy builder) are shared by the services, routes and templates.
- Page CSS and JavaScript live in `callback_site/static/` (`home/`, `nodes/` and `monitor/` split the large scripts by area; templates only carry markup and a small data bootstrap).

## SD and Cloud-Init Flow

- The imaging script is `scripts/prepare_sd_cloud_init.sh`.
- Required inputs: `profile`, `hostname`, `device`, `ssh_public_key`, `callback_url`.
- Optional metadata is passed via `cloud_init_options` and saved template profiles.
- The optional settings panel includes an integrated security starter action (`Apply Security Starter Defaults`) that disables SSH password auth, disables root SSH login, enables fail2ban, and enables unattended upgrades.
- Additional SSH hardening options are supported through cloud-init options, including `ssh_port`, `ssh_allow_groups`, and extra authorized keys.
- `distribution` supports `standard` and `k3s`.
- When `distribution` is `k3s`, the UI opens a K3s config section and populates a default block using a pragma header:

```yaml
# pragma: k3s-config-v1
server: https://<local-machine-ip>:6443
```

That config is carried forward in the cloud-init payload and used by the post-boot K3s bootstrap path.

## Source of Truth

- Hardware Monitor reads the shared source-of-truth payload and surfaces callback hardware, certificate, and monitor state.
- Node Ops reads the same source-of-truth payload and renders callback/hardware/cert context alongside provisioning state.
- The shared payload is exposed at `GET /api/state/source-of-truth`.

## Key APIs

- `GET /health`
- `POST /api/sd/prepare`
- `GET /api/sd/jobs/<job_id>`
- `GET /api/cloud-init/events`
- `POST /api/cloud-init/callback`
- `GET /api/state/source-of-truth`
- `GET /api/sd/options-profiles`
- `POST /api/sd/options-profiles`
- `DELETE /api/sd/options-profiles/<name>`
- `GET /api/nodes/summary`
- `POST /api/nodes/stale/prune`
- `POST /api/nodes/provision`
- `GET /api/nodes/module-presets?module=<id>&node=<name>` — saved configurations plus settings drawn from other nodes and SD profiles
- `POST /api/nodes/module-presets` — name a saved configuration (`id`, `name`) or save a node's current inputs under a name (`node`, `module`, `name`)
- `POST /api/nodes/module-presets/load` — fill a node's module inputs from a source (`preset:<id>`, `node:<name>`, `sd:<profile>`)
- `DELETE /api/nodes/module-presets/<id>`
- `POST /api/ssh/generate`
- `GET /api/ssh/keys`

## Data Files

- `callback_site/data/callbacks.jsonl` — callback event log
- `callback_site/data/image_cache/` — local image cache
- `callback_site/catalogs/image_catalog.yml` — source image catalog
- `callback_site/data/node_provisioning.json` — node profiles and saved inputs
- `callback_site/data/module_presets.json` — remembered and named module configurations (contains secrets; git-ignored)
- `callback_site/data/sd_option_profiles.json` — reusable cloud-init option templates
- `callback_site/data/ssh_keys/` — generated SSH key material
- `callback_site/data/node_reports/` — stored hardware-report payloads
- `callback_site/data/boot_logs/` — boot logs pulled from nodes, one folder per node

## Testing

Run the project test suite from the repo root:

```bash
cd /home/jdizzle/Playbook
.venv/bin/python -m unittest discover -s callback_site/tests -t callback_site/tests
```

For a broader validation pass, use:

```bash
./scripts/validate_callback_site.sh
```

## Deployment Notes

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

That keeps the runtime state outside the image so catalog pulls, callbacks, node reports, and operator edits survive rebuilds.

## Release / Recreate

The repo is intended to recreate from source plus an empty appdata directory. Runtime state and generated artifacts stay out of version control, including callback events, node provisioning state, SD option profile state, node reports, SSH archives, and SSH known-host caches.

Minimal recreate steps:

```bash
cd /home/jdizzle/Playbook
python -m venv .venv
.venv/bin/pip install -r callback_site/requirements.txt
CALLBACK_APPDATA_DIR=/home/jdizzle/Playbook/callback_site/data .venv/bin/python callback_site/app.py
```

If a deployment needs elevated startup only in specific contexts, set `CALLBACK_SITE_REQUIRE_ROOT=1`. Otherwise run normally as the unprivileged user.

## Notes

- SD writes are destructive; only target the intended device.
- The app expects a working local environment and is meant to be run on a trusted machine.
- The repository-root venv and the app-local venv are not interchangeable; prefer the repo-root venv for tests and the callback-site app run if that’s the environment you configured.
