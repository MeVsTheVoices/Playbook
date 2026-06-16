# Cloud-Init Callback Site

This service receives callback events from cloud-init and shows them in a simple dashboard.
It also provides a browser-based SD imaging flow that calls the local flashing script.

## Run locally

```bash
cd callback_site
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

If port 8088 is already in use:

```bash
PORT=8090 python3 app.py
```

To identify the process using 8088:

```bash
sudo lsof -iTCP:8088 -sTCP:LISTEN -n -P
```

Open:
- http://127.0.0.1:8088/
- API: http://127.0.0.1:8088/api/cloud-init/events

Callback endpoint used by SD prep flow:
- POST http://<host>:8088/api/cloud-init/callback

## Browser SD imaging

The browser cannot directly format raw block devices by itself. This app solves that by running on the same host as the SD reader and invoking the local script:
- `scripts/prepare_sd_cloud_init.sh`

Requirements:
- Run the site on the machine that has the SD card attached.
- The process user must be root, or have passwordless sudo for the script and disk tools.
- Use local/trusted network only; this endpoint is destructive.

API endpoints:
- `GET /api/sd/devices`
- `POST /api/sd/prepare`
- `GET /api/sd/jobs/<job_id>`

Node Ops endpoints:
- `GET /api/nodes/summary`
- `POST /api/nodes/config`
- `POST /api/nodes/monitor`

## First-time SSH key generation

The SD Imager page includes a button to generate or load an SSH keypair for bootstrap use.

- Endpoint: `POST /api/ssh/generate`
- Default key name: `rack-bootstrap-ed25519`
- Storage path: `callback_site/data/ssh_keys/`

When generated/loaded, the public key is auto-filled into the SD form.

## Node Ops tab

The UI now includes a sidebar toggle and a `Node Ops` tab:
- Reads active node groups from `inventory.yml` via `ansible-inventory`.
- Stores local credentials and webhook URL for monitoring actions.
- Runs basic host reachability checks (ping) and displays last callback timestamp.
- Optional webhook dispatch of monitoring summary payload.

## Data storage

Events are appended to:
- `callback_site/data/callbacks.jsonl`
