# Callback Site Operator Portal

A small Flask app for Raspberry Pi and homelab provisioning: cloud-init bootstrap, SD-card flashing, callback tracking, image catalog management, and post-boot node provisioning.

## What it does

- SD-card imaging workflow for `pi5` and `pibplus` profiles.
- Cloud-init authoring with reusable optional-setting templates.
- Distribution-aware imaging: standard and `k3s` flows, including a defaulted K3s config block that opens automatically when `distribution=k3s` is selected.
- Catalog image management with local cache checks and bulk pulls.
- Callback dashboard for first-boot cloud-init events.
- Post-boot Node Ops workflow with saved node inputs and Ansible provisioning.
- OpenWrt inspection helpers and SSH key management.
- Tailscale ACL planning support.

## Quick start

From the repo root:

```bash
cd /home/jdizzle/Playbook
.venv/bin/python3 callback_site/app.py
```

For SD flashing, prefer running as root:

```bash
sudo -E /home/jdizzle/Playbook/.venv/bin/python3 callback_site/app.py
```

Then open:

- http://127.0.0.1:9821/
- http://127.0.0.1:9821/images
- http://127.0.0.1:9821/nodes
- http://127.0.0.1:9821/monitor

## Main pages

- `/` — SD image prep, SSH keys, optional cloud-init settings, and saved templates.
- `/images` — cached and downloadable image catalog with bulk pull jobs.
- `/monitor` — callback event feed and operational status.
- `/nodes` — node inventory, saved inputs, and provisioning check/apply.
- `/openwrt` — router inspection helper for Ansible access.
- `/tailscale-acl` — ACL planning and generation.

## Backend layout

- `callback_site/app.py` remains the Flask entrypoint and shared runtime/job state owner.
- Route registration is split into focused modules:
  - `callback_site/routes_pages.py` for page and fragment rendering routes.
  - `callback_site/routes_openwrt.py` for OpenWrt helper API routes.
  - `callback_site/routes_settings.py` for settings, logging, and health routes.
- Core business logic and persistence helpers remain in `callback_site/core.py`.

## Current SD and cloud-init flow

- The imaging script is `scripts/prepare_sd_cloud_init.sh`.
- Required inputs: `profile`, `hostname`, `device`, `ssh_public_key`, `callback_url`.
- Optional metadata is passed via `cloud_init_options` and saved template profiles.
- The optional settings panel includes an integrated security starter action (`Apply Security Starter Defaults`) that sets conservative defaults in-place (disable SSH password auth, disable root SSH login, enable fail2ban, enable unattended upgrades).
- Additional SSH hardening options are supported through cloud-init options, including `ssh_port`, `ssh_allow_groups`, and extra authorized keys.
- `distribution` is supported with `standard` and `k3s` values.
- When `distribution` is `k3s`, the UI opens a K3s config section and populates a default config block using a pragma header:

```yaml
# pragma: k3s-config-v1
server: https://control-plane.example:6443
```

This config is carried forward in the cloud-init payload and later used by the post-boot K3s bootstrap path.

## Key APIs

- `GET /health`
- `POST /api/sd/prepare`
- `GET /api/sd/jobs/<job_id>`
- `GET /api/cloud-init/events`
- `POST /api/cloud-init/callback`
- `GET /api/sd/options-profiles`
- `POST /api/sd/options-profiles`
- `DELETE /api/sd/options-profiles/<name>`
- `GET /api/nodes/summary`
- `POST /api/nodes/provision`
- `POST /api/ssh/generate`
- `GET /api/ssh/keys`

## Data files

- `callback_site/data/callbacks.jsonl` — callback event log
- `callback_site/data/image_cache/` — local image cache
- `callback_site/catalogs/image_catalog.yml` — source image catalog
- `callback_site/data/node_provisioning.json` — node profiles and saved inputs
- `callback_site/data/sd_option_profiles.json` — reusable cloud-init option templates
- `callback_site/data/ssh_keys/` — generated SSH key material

## Testing

Run the project test suite from the repo root:

```bash
cd /home/jdizzle/Playbook
.venv/bin/python -m unittest callback_site/tests/test_app.py
```

## Notes

- SD writes are destructive; only target the intended device.
- The app expects a working local environment and is meant to be run on a trusted machine.
- The repository-root venv and the app-local venv are not interchangeable; prefer the repo-root venv for the tests and the callback-site app run if that’s the environment you configured.
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
