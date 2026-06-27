# Rack Cluster Runbook

## Current Hardware Snapshot
- Top of rack: GL.iNet router running OpenWrt.
- Network layer: PoE switch under router.
- Raspberry Pi 5: 2 currently online, 4 target total. Pi 5 nodes include NVMe bottoms and PoE hats.
- Raspberry Pi B+: 1 currently online, 3 target total. Pi B+ nodes have no NVMe storage.

## Repository Model
- Active hosts are in `rpi5_active` and `rpi_bplus_active` and are targeted by `site.yml`.
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
2. Prepare an SD card with cloud-init and callback baked in:
   - `cd ..`
   - `chmod +x scripts/prepare_sd_cloud_init.sh`
   - `sudo scripts/prepare_sd_cloud_init.sh --profile pi5 --hostname rpi5-03 --device /dev/sdX --ssh-key-file ~/.ssh/id_ed25519.pub --callback-url http://<callback-host>:8088/api/cloud-init/callback`
3. For Pi B+, provide a cloud-init compatible image explicitly:
   - `sudo scripts/prepare_sd_cloud_init.sh --profile pibplus --hostname rpi-bplus-02 --device /dev/sdX --ssh-key-file ~/.ssh/id_ed25519.pub --callback-url http://<callback-host>:8088/api/cloud-init/callback --image-url <your-cloud-init-image-url>`
4. Insert SD card, boot node, then watch callback events:
   - Open `http://<callback-host>:8088/`

## Callback Site Testing
- Run the local regression suite from `callback_site/` with `python -m unittest discover -s tests`.
- The tests cover image CRUD, callback event fragments, and the health endpoint.

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
