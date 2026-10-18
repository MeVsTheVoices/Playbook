# Rack Cluster Runbook

## Current Hardware Snapshot
- Top of rack: GL.iNet router running OpenWrt.
- Network layer: PoE switch under router.
- Raspberry Pi 5: 2 currently online, 4 target total. Pi 5 nodes include NVMe bottoms and PoE hats.
- Raspberry Pi B+: 1 currently online, 3 target total. Pi B+ nodes have no NVMe storage.
- Raspberry Pi 3B+: 2 slots reserved for k3s control-plane testing. Pi 3B+ nodes run 64-bit images and SD-card storage only.

## Power-Restore / IP-Change Note
- Every device provisioned by this workflow now tries to call home again on every boot or power restoration by enabling a persistent local systemd callback.
- The callback is intentionally simple and unencrypted for now, so when a PoE power cycle causes a different DORA lease or a new upstream IP, we still need at least CHAP-level reauthentication before accepting that new IP as the node's canonical address.
- In other words: DHCP can change the TCP/IP endpoint, but the identity update still needs an authenticated reauth step to avoid stale or wrong lease data.

## Repository Model
- Active hosts are in `rpi5_active`, `rpi_bplus_active`, and `rpi_3bplus_active` and are targeted by `site.yml`.
- Planned hosts are tracked in `rpi5_planned` and `rpi_bplus_planned` but not targeted by default.
- Router and switch are represented in inventory as infrastructure placeholders and marked unmanaged.

## Provisioning Flow
1. Bootstrap the local workspace once:
   - `./scripts/bootstrap_project.sh`
1. Bootstrap each node with the matching cloud-init example in `cloud-init/`.
2. Set reachable hostnames or Tailscale hostnames in `inventory.yml`.
3. Add secrets to your vault-backed files (Tailscale OAuth and optional MAAS API key).
4. Run Ansible for active nodes:
   - `./.venv/bin/ansible-playbook site.yml --check`
   - `./.venv/bin/ansible-playbook site.yml`

## SD Bootstrap + Callback (Recommended)
1. Start the callback site:
   - `./scripts/bootstrap_project.sh --start-callback`
2. Open the modular dashboard fragments and refresh individual tables when you do not want a full page reload.
3. Prepare an SD card with cloud-init and callback baked in:
   - `cd ..`
   - `chmod +x scripts/prepare_sd_cloud_init.sh`
   - `sudo scripts/prepare_sd_cloud_init.sh --profile pi5 --hostname rpi5-03 --device /dev/sdX --ssh-key-file ~/.ssh/id_ed25519.pub --callback-url http://<callback-host>:8088/api/cloud-init/callback`
4. For Pi B+, provide a cloud-init compatible image explicitly:
   - `sudo scripts/prepare_sd_cloud_init.sh --profile pibplus --hostname rpi-bplus-02 --device /dev/sdX --ssh-key-file ~/.ssh/id_ed25519.pub --callback-url http://<callback-host>:8088/api/cloud-init/callback --image-url <your-cloud-init-image-url>`
5. For Pi 3B+, the default Ubuntu 24.04 ARM64 image applies automatically (same as Pi 5), or pass `--image-url` explicitly:
   - `sudo scripts/prepare_sd_cloud_init.sh --profile pi3bplus --hostname rpi-3bplus-01 --device /dev/sdX --ssh-key-file ~/.ssh/id_ed25519.pub --callback-url http://<callback-host>:8088/api/cloud-init/callback`
5. Insert SD card, boot node, then watch callback events:
   - Open `http://<callback-host>:8088/`
6. If the callback host is reached through the router WAN or another NAT hop, the dashboard will show that upstream address in `source_ip`.
   - For example, Pi LAN leases on `192.168.174.x` can still appear as `10.255.x` in callback logs when the router NATs the request.
   - Use the router client list for the Pi's LAN lease and the callback dashboard for the observed request path.

## Callback Site Testing
- Run the local regression suite from `callback_site/` with `python -m unittest discover -s tests`.
- The tests cover page/fragment rendering, image CRUD, callback event fragments, source IP precedence, and the health endpoint.
- The verifier terminal now accepts only named checks (no freeform remote command input). Use `GET /api/test-terminal/checks` to review allowed checks.
- Quick smoke checks:
   - `curl -i http://127.0.0.1:8088/`
   - `curl -s http://127.0.0.1:8088/health`
   - `curl -s http://127.0.0.1:8088/api/cloud-init/events`

## Shared State Model
- Hardware Monitor and Node Ops both read from `/api/state/source-of-truth` so callback data, monitor state, and node provisioning state stay aligned.
- Hardware callbacks populate the monitor view, and Node Ops reuses the same callback-derived fields for live inventory and provisioning context.
- Use the Hardware Monitor page when you want the freshest callback-driven hardware snapshot; use Node Ops when you want the same data plus profile and apply controls.
- The remote inventory / terminal checks are for one-off verification and should not replace the shared state views.

## Preflight Before Flashing
1. Run the full callback-site validation script from the repo root:
   - `./scripts/validate_callback_site.sh`
2. Confirm hot-plug update on the Home page target device selector:
   - remove SD card and verify the selector updates to no-device state
   - reinsert SD card and verify the expected `/dev/*` target reappears
3. Use the dashboard link to open verifier terminal in a separate window and run a named check against a known host.
4. Confirm callback dashboard cards render long "Latest pull" values without clipping or overflow.

## Node Ops Progression
1. After callback events appear for a node, open `/nodes`.
2. Assign per-node configuration profiles (baseline/tailscale/telegraf/docker/kubernetes).
3. Save profile-specific inputs (for example, Telegraf endpoint/bucket/org/access key/secret key, Tailscale auth key/tailnet).
4. Prefer the node's current IP in Node Ops when local DNS is not available; the provisioning workflow uses that address to build a temporary inventory and target the live node directly.
5. Run "Check Closer" to validate node profile sets (`mode=check`).
6. If required values are missing, fill them in and save inputs, then re-run check.
7. Run "Apply" for optional provisioning once checks look correct.
8. Use plan-only checks by disabling Ansible execution in the Node Ops UI.
9. Expect the provisioning path to be non-interactive: it should not hang on vault prompts, and it should use the bootstrap SSH identity/private key that matches the original cloud-init bootstrap.

## Kubernetes (K8s) Primer
- What it is: Kubernetes is a scheduler and control plane for running containerized workloads across multiple machines.
- What it does for you: keeps services running, restarts failed containers, handles rollouts, and can spread workloads across nodes.
- Why people use it: useful when you run many services across several nodes and want standardized deployment/operations.
- Why it might be overkill: if you mainly run a few homelab services, Docker Compose is often simpler and easier to debug.
- Practical recommendation for this rack: start with Docker/Ansible first; move to K8s only when you need multi-node scheduling, self-healing workloads, and declarative app lifecycle at scale.
- Safe next step if you want to explore: stand up a small single-purpose cluster slice (for example one non-critical service) before migrating core infrastructure.

## MAAS Notes
- MAAS defaults are in `group_vars/all/maas.yml` and disabled by default.
- Set `maas_manage: true`, the MAAS API URL, and a vault-backed key when MAAS automation is introduced.
- Keep architecture/pool/zone mappings in `maas_role_profiles` aligned with your MAAS resource pools.

## Bring-Up Checklist for New Nodes
1. Move host from planned group to active group in inventory.
2. Set the node `ansible_host` to a reachable DNS/IP/Tailscale name.
3. Validate SSH and Python availability on the node.
4. Run `./.venv/bin/ansible-playbook site.yml --limit <hostname>` first.
5. Run full `site.yml` after host-level validation.
