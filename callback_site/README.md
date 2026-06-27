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

## API Endpoints

Core:
- `GET /health`
- `POST /api/cloud-init/callback`
- `GET /api/cloud-init/events`

SD workflow:
- `GET /api/sd/devices`
- `POST /api/sd/prepare`
- `GET /api/sd/jobs/<job_id>`

SSH key workflow:
- `POST /api/ssh/generate`
- `GET /api/ssh/download/<key_name>/private`
- `GET /api/ssh/download/<key_name>/public`

Node Ops:
- `GET /api/nodes/summary`
- `POST /api/nodes/config`
- `POST /api/nodes/monitor`

Fragment endpoints:
- `GET /fragments/sd/image-catalog-rows`
- `GET /fragments/sd/images-rows`
- `GET /fragments/cloud-init/event-rows`

Tests:
- `python -m unittest discover -s tests`

## Data Files

- Callback event log: `callback_site/data/callbacks.jsonl`
- Node monitor config: `callback_site/data/node_monitor_config.json`
- Generated SSH keys: `callback_site/data/ssh_keys/`
- Per-run archived SSH public keys: `callback_site/data/ssh_key_archive/`
- Per-run key archive index: `callback_site/data/ssh_key_archive.jsonl`

## Local Persistence (AppData Style)

This app persists state to a local appdata directory.

- Default path: `callback_site/data/`
- Override path with environment variable: `CALLBACK_APPDATA_DIR`

Example (local custom path):

```bash
CALLBACK_APPDATA_DIR=/home/jdizzle/appdata/callback_site sudo PORT=9821 .venv/bin/python3 app.py
```

This mirrors a simple Docker volume pattern (host path mounted as appdata) without requiring any other changes.
