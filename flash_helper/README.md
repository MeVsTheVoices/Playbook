# Isolated Linux flashing helper

The portal stays unprivileged. This optional helper discovers explicitly allowed
disks and performs the existing Linux image download, schema validation,
unmount, write, full-image readback, cloud-init injection, seed readback and
cleanup workflow. It accepts neither shell commands nor arbitrary local paths.
Windows-native raw disk access is not implemented.

The same helper now supports two deliberately different workflows:

- **Pi (`workflow=pi`, the default):** flash a raw `.img`/`.img.xz`, inject the
  validated cloud-init seed into its boot partition, then verify both.
- **UEFI (`workflow=uefi-installer`):** verify a pinned installer ISO, embed
  Ubuntu autoinstall or Rocky Kickstart, preserve its UEFI boot catalog, and
  write/read back the customized ISO. This USB installs onto a **different
  internal disk** when booted; it does not run the installed OS from the USB.

## Unattended UEFI installation

The catalog includes Ubuntu Server 22.04, 24.04 and 26.04 LTS and Rocky Linux
9/10 Minimal for x86_64 (`amd64`) and ARM64 (`aarch64`). URLs and SHA-256 values
are pinned to upstream release checksum files; architecture metadata does not
make these images compatible with stock Raspberry Pi firmware. ARM targets
must already support UEFI. Rocky 10 x86_64 requires an x86-64-v3 CPU.

UEFI requests use `profile=uefi-x86_64` or `profile=uefi-arm64`, the matching
`catalog_id`, `workflow=uefi-installer`, and **`install_authorized: true`**.
The existing USB `device`, `device_identity`, and `confirm_device` fields
still apply. Authorization must be an explicit, fresh warning/confirmation
at USB creation, not a saved template default:

The dashboard offers both UEFI profiles, disables creation with an explanation
when no Linux helper is configured, limits options to username/timezone/locale,
and locks the image source to the selected catalog ISO. It asks for installation
authorization separately from confirmation of the USB device on every submission.
Saved templates retain the architecture and catalog selection, never authorization.

> Booting this USB will erase the sole eligible internal disk without another
> installation prompt. Do not boot it on a machine whose data you need.

The installer preflight runs before storage configuration. It requires UEFI,
identifiable installer mounts, and exactly one unmounted, writable, non-USB,
non-removable whole disk of at least 16 GiB with a serial or WWN. Active
mapped/RAID disks are rejected. Zero or multiple eligible disks, unknown media,
or missing identity stops installation; it never chooses the largest/first disk.
Existing unmounted partitions **do not protect data from erasure**. USB-attached
destination disks and systems with multiple eligible internal disks require a
future explicit disk-selection workflow.

Baseline options are hostname, SSH public key, non-root `username` (default
`rackadmin`), `timezone`, `locale`, and callback URL. The account receives
passwordless administrative sudo, key-based SSH, no SSH password login, and
no root SSH login. DHCP and an accessible callback endpoint are required.
Other enabled Pi/cloud-init/K3s options are rejected, not silently discarded.
Installer callbacks describe completion before the machine powers off.
Additional first-boot, Tailscale, package and K3s workflows are not implemented
for UEFI installation.

Both installer families power off after installation rather than rebooting
into the destructive USB again. Remove the USB before starting the installed
system. Keep the USB physically controlled: creation-time authorization travels
with it. ISO remastering preserves upstream signed bootloader/kernel binaries
but is not a promise of Secure Boot compatibility on every firmware.

Automated tests cover single-disk refusal cases, generated unattended config,
GRUB arguments and EFI boot-catalog preservation. Real official x86_64 ISO
remastering can be verified without disk access, but complete machine boots,
ARM firmware behavior and physical installs still need hardware/VM acceptance
testing before operational use.

## Lifecycle and API

The helper defaults to a 180-second idle lease. Authenticated discovery and job
polls renew it; `/health` probes and unauthorized requests do not. Active jobs
prevent expiry, even without a browser. Completion starts a fresh idle grace
period. SIGTERM stops accepting work and waits for an active worker to finish.
Only one write is accepted at a time; submitting the same job ID is idempotent.
Job state is process-local, not resumable or durable.

Portal API (browser requests use the portal, never the privileged helper):

| Endpoint | Behavior |
| --- | --- |
| `GET /api/sd/backend` | `{ok, backend: "native" or "helper", discovery: "sse" or "poll"}`; no wake-up. |
| `GET /api/sd/devices` | Wakes a configured Kubernetes Job on connection failure and renews the running helper lease. Returns `{backend, devices, errors}`. Startup, authentication, and discovery failures return HTTP 503 with errors, not an empty success. |
| `POST /api/sd/prepare` | Existing payload plus `device_identity` and `confirm_device` (the confirmed path). UEFI adds `workflow`, `catalog_id`, `install_authorized` and a UEFI profile. Requires matching discovery identity, HTTPS image URL and expected SHA-256. No `image_file` or `workspace` override in helper mode. Existing job response shape is preserved. |
| `GET /api/sd/jobs`, `GET /api/sd/jobs/<id>` | Existing portal job API; a background proxy polls the helper while the job runs. |

Each helper device has `path` (stable `/dev/disk/by-id/...` alias), `name`,
`size` (bytes), `model`, `serial`, `transport`, `mountpoints`, and opaque
`identity`. Confirm the identity/size/model/serial and warn that all data will
be erased. Retain the selected path and identity across discovery refreshes;
if either changes, require another explicit confirmation. Poll discovery every
five seconds **only while the dashboard is visible**. Do not start the native
SSE endpoint in helper mode (it returns 409). Closing or hiding the dashboard
stops discovery heartbeats; a running job remains protected independently.

The helper itself exposes only:

- `GET /health`: unauthenticated readiness; no lease renewal.
- `GET /devices`: authenticated discovery and heartbeat.
- `POST /jobs`: authenticated, validated preparation with a 32-character hex
  `job_id`; returns 202, or 200 for a duplicate ID. A competing write returns 409.
- `GET /jobs/<id>`: authenticated, bounded job output/status. Unknown jobs return 404.

Use `Authorization: Bearer <dedicated token>` for all non-health requests.
Identity is checked at submission, worker startup, and again immediately before
overwriting, after lengthy downloads/rendering. The existing per-device `flock`
also excludes concurrent script writers. Unplug/replug changes Linux `diskseq`,
even if the path and serial are reused. Missing identity information, unknown
host mounts, system disks and swap disks fail closed.

## Trust and hardware boundaries

**The helper is host-administrator code, not a sandbox.** Its narrow API and
allowlist reduce accidental misuse; they cannot contain a compromised root
process. Do not expose it publicly or give its token to browsers. The token
authorizes one trusted portal, not an individual human login. The portal still
requires trusted-network access controls; this feature does not add user login.
Use TLS or a private encrypted network between different hosts. HTTP inside a
cluster is appropriate only with an enforced NetworkPolicy and trusted network.

Hotplug-capable container execution uses a privileged pod with the host's `/dev`
and a read-only host mount table. This is intentionally restricted to the
optional helper, **never** the dashboard. Use a dedicated Linux reader host,
explicit by-id allowlists, and a namespace permitting privileged workloads.
Kubernetes cannot constrain `jobs/create` to a particular Job name or manifest:
the portal's lifecycle permission can create other Jobs in that namespace.
Isolate that namespace and use admission controls if stronger separation is
needed. The chart restricts Job get/delete and template reads by name.

Potential next additions are Debian stable (broad server support and preseed),
Fedora Server (new hardware and SELinux), openSUSE Leap/MicroOS (transactional
server/appliance variants), and Alpine (small-footprint nodes). Each needs its
own unattended configuration and boot acceptance tests; merely adding an ISO
URL does not constitute supported provisioning.

The container refuses targets still mounted **on the host**; unmount those
partitions there first and disable desktop automount for the dedicated reader.
It only unmounts filesystems in its own mount namespace. It does not perform
USB/IP attachment, Windows administrative operations, host-namespace entry,
host formatting outside the selected target, or host daemon configuration.
Kernels must expose `/sys/class/block/<disk>/diskseq`.

Docker Desktop Kubernetes runs inside a VM: scheduling this Job there does not
make Windows USB devices appear. Use a Linux node that actually sees the
reader. Kubernetes host maintenance, forced deletion, OOM, loss of power and
node eviction can still interrupt a write. Never force-delete an active helper
or remove the card until verification and cleanup finish.

## Build and direct Linux use

From the repository root:

```bash
docker build -f flash_helper/Dockerfile -t playbook-flash-helper:0.1.0 .
```

Generate a dedicated token into a private file (do not put it in Git or command
arguments), for example with `umask 077; openssl rand -hex 32 > /secure/flash-token`.
The helper and portal must read identical token contents. Only operator-owned
configuration supplies these variables:

| Helper variable | Meaning |
| --- | --- |
| `FLASH_TOKEN_FILE` | Required token file, 32-256 URL-safe ASCII characters (hex recommended). |
| `FLASH_ALLOWED_DEVICES` | Required comma-separated whole-disk by-id aliases. |
| `FLASH_IDLE_SECONDS` | 30-3600 seconds; default 180. |
| `FLASH_WORK_DIR` | Existing directory for UEFI remastering; defaults to the system temporary directory natively and `/work` in the image. |
| `FLASH_BIND`, `FLASH_PORT` | Default loopback and 8090; image binds all container interfaces. |
| `FLASH_HOST_MOUNTINFO` | Host mount table; default `/proc/1/mountinfo` for native execution. Container must use `/host/mountinfo`. |

A native root process can use the existing Python dependencies and Linux
packages listed in the [Dockerfile](Dockerfile), then run
`PYTHONPATH=.:callback_site python -m flash_helper.server` from the repository
root. Do not set `CALLBACK_FLASH_HELPER_URL` in the helper environment: it would
recursively forward work. Native mode exits on idle and must be started by the
operator or an independently controlled supervisor when needed.

For an explicitly started Linux container:

```bash
docker run --rm --name playbook-flasher --privileged \
  --mount type=bind,src=/dev,dst=/dev \
  --mount type=bind,src=/proc/1/mountinfo,dst=/host/mountinfo,readonly \
  --mount type=bind,src=/secure/flash-token,dst=/token/token,readonly \
  -e FLASH_TOKEN_FILE=/token/token \
  -e FLASH_ALLOWED_DEVICES=/dev/disk/by-id/usb-YOUR_READER \
  -e FLASH_HOST_MOUNTINFO=/host/mountinfo \
  -p 127.0.0.1:8090:8090 playbook-flash-helper:0.1.0
```

Do not use `restart: always`: idle expiry is intentional. The image includes
`cloud-init` for mandatory schema validation. Work/cache are ephemeral and may
need several times the expanded image size; provide sufficient disk space.

Portal configuration:

```text
CALLBACK_FLASH_HELPER_URL=https://linux-reader.example:8090
CALLBACK_FLASH_HELPER_TOKEN_FILE=<portal-readable token file>
```

For Compose, set `FLASH_HELPER_TOKEN_FILE` to the host file path and use
`docker compose -f compose.yaml -f compose.flash-helper.yaml up --build -d`.
This mounts only the token into the non-root portal, not disks or Docker's
socket. Ensure UID 10001 can read the mounted token without making it public.
Leave the optional override unused to preserve native behavior.

## On-demand Helm lifecycle

The [chart](../charts/flash-helper/) is disabled by default and creates nothing.
With `enabled=true` it installs a Service, Job-template ConfigMap, Role,
RoleBinding and NetworkPolicy, **not a running Job or Deployment**.

1. Push the helper image to a registry reachable by the pinned Linux node, or
   preload it on that node. Set `image.repository` and `image.tag` accordingly.
2. Create a dedicated namespace and a Secret there from the private token file,
   for example `kubectl -n flash-helper create secret generic flash-token
   --from-file=token=/secure/flash-token`. Mount the same token into the portal.
3. Install (replace the node, reader and existing portal ServiceAccount):

```bash
helm upgrade --install rack charts/flash-helper --namespace flash-helper \
  --set enabled=true --set nodeName=linux-reader \
  --set existingSecret=flash-token \
  --set portal.namespace=default --set portal.serviceAccount=portal \
  --set 'allowedDevices[0]=/dev/disk/by-id/usb-YOUR_READER'
```

4. Configure the portal:

```text
CALLBACK_FLASH_HELPER_URL=http://rack-flasher.flash-helper.svc:8090
CALLBACK_FLASH_HELPER_TOKEN_FILE=<mounted token path>
CALLBACK_FLASH_HELPER_NAMESPACE=flash-helper
CALLBACK_FLASH_HELPER_TEMPLATE=rack-flasher-template
```

The portal needs `kubectl` (already in its image), the chart's lifecycle RBAC
via its ServiceAccount or a dedicated kubeconfig, cluster API access, and
reachability to the Service. Set `portal.podLabels` to the portal's actual
labels; the CNI must enforce NetworkPolicy. This contract does not install a
dashboard chart or silently grant cluster-admin permissions.

The first device poll starts the Job; discovery returns 503 while it schedules.
Further visible polls discover hotplugged readers without launching extra
writers. After idle expiry, Kubernetes records the completed Job; the next
discovery removes only that terminal Job, and a later poll recreates it.
No automatic retry of failed writes or reconnect-triggered write resubmission
occurs. A disconnected writer may still be active: inspect its logs before
resubmitting. A portal outside the cluster (including Windows Compose) needs
a privately reachable helper Service address and a dedicated kubeconfig;
cluster-internal DNS does not normally resolve on Windows.

Uninstall only after active writes finish. Helm does not own the dynamically
created Job, so remove that named terminal Job explicitly after uninstalling.
Jobs, container work/cache and logs are temporary; preserve needed diagnostics
before the next wake-up deletes a completed Job. Treat work files and logs as
sensitive because cloud-init contains bootstrap credentials.
