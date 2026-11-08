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
- Connection profiles (in active development): each node keeps several ways to reach it (callback, inventory, tailnet IP, tailnet FQDN, DNS, hostname) in `data/connection_profiles.json`, and a replaceable strategy picks one (`prefer_tailnet` default, `prefer_fqdn`, `lan_first`; set `CALLBACK_CONNECTION_STRATEGY` or `register_strategy`). The FQDN profile is only chosen when this host can resolve the MagicDNS name, and every tailnet profile carries the node's `fqdn` for later HTTPS with Tailscale certificates. `GET /api/nodes/<node>/connection-profiles` shows the candidates and the pick. Pinned profiles, per-profile user/key/port, failover and HTTPS via `tailscale cert` are planned next.
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

- http://127.0.0.1:8088/
- http://127.0.0.1:8088/images
- http://127.0.0.1:8088/nodes
- http://127.0.0.1:8088/monitor

## Pages

- `/` — SD image prep, SSH keys, optional cloud-init settings, and saved templates.
  On mobile, SSH key controls stack and callback tables scroll within their panel so the dashboard stays within the viewport.
  Older callbacks per host use single-line compact rows, including certificate
  text and badges; expand a row or use Expand older to see full details.
- `/images` — cached and downloadable image catalog with bulk pull jobs.
- `/monitor` — callback event feed, hardware snapshots (including a dedicated Architecture column), and certificate pull helpers. Architecture comes from hardware-check callbacks (`uname -m` on boot or `ansible_architecture` from Node Ops); run Get Hardware Profile to populate it for older reports.
- `/nodes` — node inventory, saved inputs, per-module configuration with remembered/named configurations, and provisioning check/apply.
- `/openwrt` — redirects to the OpenWrt routers panel in `/nodes`.
- `/tailscale-acl` — rack tailnet policy builder (grants, K3s ports, SSH, routes, tests).

## Longhorn Notes

- Longhorn is intentionally wired as an optional Pi 5 profile for now.
- The first pass assumes the nodes still boot from SD card.
- The storage path is configurable so you can later move Longhorn data to an NVMe-backed mount without changing the operator flow.
- The Storage Disk input (default `auto`) runs the `rack_storage_disk` Ansible module (`library/`): it partitions, formats and mounts the one blank disk at the storage path by UUID, never touches a disk that holds data, and reports the path, UUID and by-id name in the run output. `none` keeps a plain directory.
- The chart is written only on a K3s server node, so on a worker the module prepares the node (iSCSI, storage path) and skips the chart.
- The K3s dashboard reports whether the `longhorn-system` namespace and pods are present and healthy.

## Architecture

- `callback_site/app.py` is the Flask entrypoint: logging, the app object and route registration.
- `callback_site/routes/` holds the HTTP endpoints, one module per area (pages, openwrt, settings, callbacks, sd, images, ssh, test_terminal, monitor, nodes). Each exposes a `register_*_routes(app)` function.
- `callback_site/services/` holds the domain logic, layered so imports only point downward:
  - `storage`, `settings`, `sd_profiles`, `ssh_keys`, `openwrt`, `network`: paths, settings, keys and addressing.
  - `events`, `images`, `image_catalog`, `reports`, `boot_logs`: callback events, the image library, catalog pulls and node boot logs.
  - `node_state`, `node_inventory`, `node_runs`, `node_views`: Node Ops state, inventory, Ansible runs and read models.
  - `live_config`: read-only SSH reads of a module's current settings from a node (same layer as `node_runs`).
  - `sd_prepare`, `k3s_config`, `sd_devices`, `kubernetes`, `terminal_checks`, `provision_jobs`: SD preparation, config checks, devices, cluster summary and background jobs.
- `callback_site/k3s_lightweight.py` (board and role registry) and `callback_site/tailscale_policy.py` (policy builder) are shared by the services, routes and templates.
- `callback_site/network_toolkit.py` defines the Pi 3B+ appliance class; it renders the same runtime assets that `roles/network_toolkit` installs with Ansible.
- Page CSS and JavaScript live in `callback_site/static/` (`home/`, `nodes/` and `monitor/` split the large scripts by area; templates only carry markup and a small data bootstrap).

## SD and Cloud-Init Flow

- The imaging script is `scripts/prepare_sd_cloud_init.sh`.
- Required inputs: `profile`, `hostname`, `device`, `ssh_public_key`, `callback_url`.
- Optional metadata is passed via `cloud_init_options` and saved template profiles.
- Cloud-init sections follow a per-profile matrix in `static/home/cloud_init_options.js`.
  K3s settings appear only for the K3s distribution; Pi-specific boot controls
  appear only for Pi 5 templates, the only board with such controls.
  Profile-hidden fields retain their values when switching profiles but are disabled
  and excluded from submissions and saved templates.
- `reboot_after_bootstrap` uses cloud-init's `power_state` module rather than rebooting inside `runcmd`, so the power-restore callback service is enabled and the final cloud-init modules can finish before shutdown. This applies to newly generated SD user-data; already-flashed nodes need updated user-data before repeating bootstrap. Old cloud-init log errors remain in the log history.
- The optional settings panel includes an integrated security starter action (`Apply Security Starter Defaults`) that disables SSH password auth, disables root SSH login, enables fail2ban, and enables unattended upgrades.
- Additional SSH hardening options are supported through cloud-init options, including `ssh_port`, `ssh_allow_groups`, and extra authorized keys.
- `distribution` supports `standard` and `k3s`.
- When `distribution` is `k3s`, the UI opens a K3s config section and populates a default block using a pragma header:

```yaml
# pragma: k3s-config-v1
server: https://<local-machine-ip>:6443
```

That config is carried forward in the cloud-init payload and used by the post-boot K3s bootstrap path.

### Website-hosted SSH public keys

`GET /api/ssh/public/<key_name>` serves a saved public key as plain text without
serving its private key. Generate or load a website key in **SSH Access** to enable
automatic first-boot retrieval. The endpoint URL uses the callback URL's host and
port, which must be reachable from the new node (not `localhost` or loopback).
Uploaded or manually edited keys remain embedded-only.

Cloud-init fetches the key with bounded retries and verifies its SHA-256 digest
against the selected key before adding it to the administrative user's
`authorized_keys`, preserving other keys. The selected key is also embedded so
an unavailable endpoint does not lock out bootstrap SSH; fetching failures are
reported as cloud-init errors. Retain the matching private key for future login.
This is a first-boot install, not automatic key rotation or a fix for sudo
permissions. Use HTTPS where possible; a changed endpoint key is rejected until
you deliberately select it for a new image.

### Default Pi administrative access

All Pi imaging profiles (Pi 5, Pi 4, Pi 3B+ and Network Toolkit) grant the
configured administrative user passwordless sudo by default. Cloud-init sets
the user grant and, before the other first-boot commands, installs and validates
`/etc/sudoers.d/010_<username>-nopasswd`, matching the Raspberry Pi OS
raspi-config **Admin Password disabled** setting. A non-interactive sudo check
reports a boot error if the grant is ineffective. Other users' grants are left
alone; the toolkit's `network-console` user does not receive unrestricted sudo.
The example and legacy Pi cloud-init configs also default to passwordless sudo.
SSH password authentication and root SSH access remain separate settings.
Protect administrative private keys: anyone holding one has root-level access.

### Network Toolkit (Pi 3B+)

Select **Network Toolkit (Pi 3B+)** in the SD Profile selector. This is a separate
`network-toolkit` class, not a K3s role. It uses the pinned, checksum-verified
Raspberry Pi OS **Trixie Lite 64-bit** image (which supports cloud-init), forces
Standard distribution, and installs no desktop, Docker, K3s or Telegraf.
The ARM64 browser terminal (`ttyd` 1.7.7) is installed from its pinned upstream
release with SHA-256 verification because the Pi OS Trixie repositories do not
provide the package. Imaging and Node Ops use the same URL and checksum.
Do not substitute legacy Bookworm images, which lack this first-boot workflow.

Its cloud-init panel shows only Toolkit, Extra Network Diagnostic Tools, Identity and Locale, administrative
Access, Tailscale, and Boot Reporting and Updates. Generic Wi-Fi, K3s, package
bootstrap and hardening sections are omitted. Redundant SSH/firewall, routing,
MOTD and Telegraf controls are hidden; the appliance enforces its own defaults.
The Tailscale join key is explicitly required, even with Funnel turned off.

**Extra Network Diagnostic Tools** accepts optional comma-separated Debian package
names, such as `iperf3, iputils-tracepath, whois, strace`. These are added to the
standard toolkit packages on first boot and can be saved in SD templates.
Leave blank to preserve the base package set. Only use packages available in the
image's configured repositories; unavailable packages fail cloud-init installation.
This does not grant the browser terminal any additional sudo permissions.
For command-line imaging, set `CLOUD_INIT_TOOLKIT_EXTRA_PACKAGES` to a
space- or comma-separated package list.

The profile-specific form requires:

- Wi-Fi SSID, password and country code for the `wlan0` uplink.
- A browser password **hash**, generated locally with `openssl passwd -6 -stdin`.
  Use a long, unique, randomly generated password; the browser login is `operator`.
- A preauthorized Tailscale auth key in the Tailscale section, authorized for
  **`tag:network-toolkit`**. This class uses only that tag, not the rack tags.
- A non-root administrative user and SSH public key for private management.

Before booting a Pi 3B+, use a reliable 5 V / 2.5 A supply (more USB equipment may
need a powered hub). The Wi-Fi form supports personal WPA/WPA2 PSK networks,
not enterprise EAP credentials or captive-portal login. Verify that the callback
URL is reachable from the Pi's Wi-Fi or tailnet, not a loopback address on the
imaging machine. Ethernet is for equipment, not the internet uplink; keep its
device subnet separate from Wi-Fi. Use a USB serial adapter with the electrical
standard your equipment requires.

Check power with `sudo vcgencmd get_throttled`: bit 0 indicates active
undervoltage and bit 16 records earlier undervoltage. Correct the supply/cable
and peripheral load before provisioning; software cannot remedy voltage drops.
Pi cloud-init waits for NTP during `bootcmd`, before package updates, as well as
before HTTPS installers. A synchronization timeout is reported explicitly.
Toolkit setup corrects the image's Netplan file permissions and removes the
`netplan_nm_patch` cloud-init entry only when that module is absent. The next boot
then avoids the missing-module warning. Persistent journals are capped at 16 MiB
so failures survive a restart without unbounded SD-card logging.

The **Publish authenticated terminal** checkbox is off by default. Before enabling
it, enable MagicDNS and HTTPS in the tailnet, use the Tailscale ACL planner's
**Network Toolkit Funnel permission** checkbox, and apply its generated policy.
Funnel exposes HTTPS 443 to the authenticated loopback nginx proxy on 7681;
ttyd itself listens only on loopback 7682. The browser terminal runs as
`network-console`, with serial access but no general-purpose sudo. Only
the dedicated Ethernet configuration and bounded packet-capture helpers are allowed.
SSH password and root logins are disabled; the firewall allows inbound SSH only
over Tailscale, and blocks forwarding. No router port-forward is needed.
Funnel is public, not tailnet-authenticated: anyone on the internet can reach
the login prompt. Follow your organization's policy before publishing it.

Node Ops also accepts an explicit `tag:shared` override in the Tailscale module
when the auth key and existing tailnet policy use that tag. Imaging still defaults
to `tag:network-toolkit`; the ACL planner's toolkit Funnel rules target that default
tag. A shared-tag node needs its existing policy to permit SSH and, if enabled,
Funnel. Routing and exit nodes remain disabled.

`eth0` is dedicated to equipment, uses link-local addressing initially, and never
provides a default route. USB serial adapters appear under `/dev/serial/by-id/`
(or `/dev/ttyUSB*` and `/dev/ttyACM*`). Examples from the browser terminal:

```bash
ls -l /dev/serial/by-id/
picocom --baud 9600 --flow n /dev/ttyUSB0
sudo rack-device-network show
sudo rack-device-network static 192.168.1.2/24
ping 192.168.1.1
ssh admin@192.168.1.1
sudo rack-device-capture
```

Choose a device subnet that does **not** overlap your Wi-Fi uplink or Tailscale;
the helper rejects Tailscale-range overlap and prefixes wider than /8.
Serial baud/flow settings depend on the equipment. Use the appropriate
USB serial adapter (RS-232 versus TTL); GPIO UART is not enabled by this class.
Telnet and TFTP clients are included for legacy equipment, but transmit data
without encryption: use them only on an authorized, isolated device connection.
Packet capture is restricted to `eth0`, 60 seconds or 1000 packets.

Callbacks retain `profile: network-toolkit` and discover nodes in
`network_toolkit_active`. Node Ops defaults to Baseline, Tailscale and Network
Toolkit only. The Network Toolkit module reuses the same credential hash and
Funnel toggle; Wi-Fi is boot-only and can be changed privately with `nmcli`.
Saved SD templates retain the class and non-secret settings, but omit the Wi-Fi
password, browser hash and join key. Re-enter them for every new card.
Treat the flashed card's boot files as sensitive; Wi-Fi and join credentials
are necessarily present there during first boot.

To manage an existing appliance, add it under `network_toolkit_active` in inventory
with its Tailscale address and non-root administrative user. Check then apply the
Network Toolkit module after entering its hash. The role deliberately changes
Ethernet and enforces private SSH; do not use it on a general-purpose Pi or an
existing cluster member. Reimage a former cluster node only after safely removing
it from the cluster.

Inspect `systemctl status rack-toolkit-terminal rack-toolkit-funnel` and
`tailscale funnel status` over private SSH. The public URL is the node's
`https://<hostname>.<tailnet>.ts.net/`. Disable publication with
`sudo systemctl disable --now rack-toolkit-funnel.service` (or clear the toggle
and apply the module). The proxy/terminal remain local. Failed Funnel policy
authorization or terminal setup is reported by systemd/cloud-init; publication
is not replaced with an unauthenticated fallback.

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
- `GET /api/nodes/live-config?node=<name>&module=<id>` — reads that module's current settings off the node over SSH (never secrets); a failed read is `ok: false` with the reason, not an HTTP error?module=<id>&node=<name>` — saved configurations plus settings drawn from other nodes and SD profiles
- `POST /api/nodes/module-presets` — name a saved configuration (`id`, `name`) or save a node's current inputs under a name (`node`, `module`, `name`)
- `POST /api/nodes/module-presets/load` — fill a node's module inputs from a source (`preset:<id>`, `node:<name>`, `sd:<profile>`)
- `DELETE /api/nodes/module-presets/<id>`
- `POST /api/ssh/generate`
- `GET /api/ssh/keys`
- `GET /api/ssh/public/<key_name>` — plain-text public key for verified first-boot SSH setup

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

### Docker Compose

From the repository root, build and start the callback site:

```powershell
docker compose up --build -d
docker compose ps
```

The container uses Gunicorn with one worker because background job status is
process-local; threads handle concurrent requests within that worker.
Open http://127.0.0.1:8088/. View the app log with
`docker compose logs -f callback-site`; stop it with `docker compose down`.
To use a different host port, set `CALLBACK_PORT` before starting Compose
(for example, `$env:CALLBACK_PORT=9821` in PowerShell).
The `callback-site-data` Docker volume keeps catalog pulls, callbacks, node
reports, SSH keys, and operator edits across image rebuilds and container
recreation. `docker compose down` leaves that volume intact; do not add `-v`
if you want to keep its data.

For nodes to call back to this site, configure their callback URL with the
Windows host's LAN IP and port, not `127.0.0.1`. Docker Desktop publishes the
web port; Windows Firewall may also need to allow inbound connections on that
port. This Windows deployment does not pass physical SD-card devices or the
host's Tailscale daemon into the container. SD-card flashing and host-tailnet
discovery therefore require a Linux host with those devices and services
available. Keep the site on a trusted network; it is not intended for direct
public-internet exposure.

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
