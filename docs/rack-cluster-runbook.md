# Rack Cluster Runbook

## Current Hardware Snapshot
- Top of rack: GL.iNet router running OpenWrt.
- Network layer: PoE switch under router.
- Raspberry Pi 5: 2 currently online, 4 target total. Pi 5 nodes include NVMe bottoms and PoE hats. **Their role is selectable per node: `worker` (default), `control-plane`, or `utility` (no Kubernetes).**
- Raspberry Pi 3B+: 2 slots reserved. Pi 3B+ nodes run 64-bit images and SD-card storage only. **Their role is selectable per node: `light-worker` (default, a tainted K3s agent), `utility` (no Kubernetes), or `control-plane` (not recommended, see the K3s section).**

## Power-Restore / IP-Change Note
- Every device provisioned by this workflow now tries to call home again on every boot or power restoration by enabling a persistent local systemd callback.
- The callback is intentionally simple and unencrypted for now, so when a PoE power cycle causes a different DORA lease or a new upstream IP, we still need at least CHAP-level reauthentication before accepting that new IP as the node's canonical address.
- In other words: DHCP can change the TCP/IP endpoint, but the identity update still needs an authenticated reauth step to avoid stale or wrong lease data.

## Repository Model
- Active hosts are in `rpi5_active` and `rpi_3bplus_active` and are targeted by `site.yml`.
- Planned hosts are tracked in `rpi5_planned` but not targeted by default.
- Router and switch are represented in inventory as infrastructure placeholders and marked unmanaged.

## K3s Topology And Join Preflight
- The NucBox is the K3s server. Pi 5 nodes default to workers and can be made control-plane nodes: NVMe storage and more RAM make them far better etcd voters than a Pi 3B+, and the NucBox plus two control-plane Pi 5 makes three servers, which is the odd count etcd wants. A Pi 3B+ is a light worker by default: it has about 900 MB of RAM and an SD card, below the 2 GB K3s documents for a server, and two of them as etcd voters took the cluster down twice. Control-plane remains selectable on a Pi 3B+ but is not recommended.
- Use the NucBox Tailscale address for the API endpoint and each node's own Tailscale IPv4 as `node-ip`/`advertise-address`. The LAN route may be NATed through `192.168.174.1`; etcd peer TLS identity is IP-sensitive, so do not advertise that NAT address.
- Permit TCP 6443 to the API server and TCP 2379/2380 between control-plane peers over the selected path. Confirm `nc -z <peer-ip> 2380` in both directions before joining another server.
- All control-plane servers must have matching critical settings, especially `secrets-encryption`. This cluster reports encryption disabled; remove an inherited `--secrets-encryption` systemd flag from joiners rather than enabling a different cluster mode.
- Pi 3B+ control-plane units using `protect-kernel-defaults=true` require `vm.overcommit_memory=1`, `kernel.panic=10`, and `kernel.panic_on_oops=1`. Do not pass `node-role.kubernetes.io/control-plane` through `node-label`; K3s rejects that reserved label. The role uses `role=control-plane` and the standard control-plane taint.
- SD bootstrap installs the K3s server or agent (decided by the chosen role, not guessed from the config text) before Node Ops applies the complete structured config. Worker settings are the server endpoint, token, node IP, role label and the optional taint and pod cap; do not set server-only pod/service CIDRs on agents.
- The advanced config override replaces the generated config for either family. If used, keep it complete, including the node name, join endpoint, token, node IP, role labels/taints, and any cluster-critical settings required by that node type.
- Before retrying a failed control-plane join, inspect `k3s etcd-snapshot ls` and `etcdctl member list`. With quorum, remove or update only the exact stale learner/member; do not delete a datastore or reset the cluster merely to clear a failed join.
- If quorum is lost and a member cannot be recovered, first stop K3s and create verified offline backups of the surviving server database, token, TLS material, and unit/config, plus an etcd snapshot. Only after explicit authorization, use `k3s server --cluster-reset` on the survivor, verify its single-member leadership and Tailscale peer URL, then rejoin replacement servers one at a time.
- Save the endpoint, current server token, role, and node Tailscale IP under each node's Kubernetes module before applying. The token is sensitive and should remain masked in site/API views.
- A Pi already installed as a k3s server needs an explicit migration/reimage (and removal from etcd) before it can become a worker, and an installed agent cannot silently become a server; the module refuses both.

### Known-Good Current Values
- API endpoint: `https://100.65.24.76:6443` (NucBox over Tailscale).
- Control-plane nodes: `jd-shorty-rack-pi3bplus-alpha` (`100.70.57.110`) and `jd-shorty-rack-pi3bplus-bravo` (`100.120.128.100`). Tailscale addresses are assigned per install, so read them from `tailscale status` instead of reusing old ones.
- Pi 5 nodes default to `worker`; a Pi 5 can also be `control-plane`. Secrets encryption: disabled on the NucBox; joining server units must not carry `--secrets-encryption`.
- Tailscale tags and ACL: the NucBox is tagged `tag:homelab` and the Pis carry `tag:rpi-3bplus` and `tag:homelab`. The ACL must allow those tags to reach each other and `192.168.174.0/24`; without that rule a tagged node sees no peers and K3s cannot join.
- Current state (2026-10-31, after the outage): the Pi 3B+ control nodes were too small to carry etcd reliably and quorum was lost twice. The cluster is being rebuilt from the NucBox alone with the lightweight profile below. The old `jd-shorty-rack-alpha-*`, `jd-shorty-rack-beta-*` and Pi 5 node records are stale and removed by the fresh-start script.

### Node Roles (both boards)
Chosen on the Home page (K3s section, once a Pi profile is selected; switching boards always returns to that board's safe default role) or per node in Node Ops under the Kubernetes module.

| Role | Board | What it does |
| --- | --- | --- |
| `worker` (default) | Pi 5 | K3s agent. Labelled `role=worker`. Optional taint `rack/dedicated=true:NoSchedule` and pod cap, both off by default. |
| `light-worker` (default) | Pi 3B+ | K3s agent: no etcd, no API server. Labelled `role=light-worker`, tainted `rack/light=true:NoSchedule` so only pods that tolerate it land there, and capped at 20 pods (K3s defaults to 110). |
| `utility` | both | No Kubernetes. Use it for Tailscale subnet routing or exit node, monitoring, DNS and similar. On the SD form this is simply the Standard distribution. In Node Ops the Kubernetes module applies nothing and needs no token. If K3s is already installed it is left as-is. |
| `control-plane` | both | K3s server and etcd voter, labelled `role=control-plane`. Taint, pod cap and the server options below are selectable. Pi 5 starts with all of them off, so it matches the NucBox with no extra flags. Pi 3B+ starts with the lightweight set on and the control-plane taint plus a 20-pod cap, and is not recommended. |

Every K3s role also takes the same two options: a taint (defaults: Pi 3B+ on, Pi 5 off) and a pod cap (blank means the board default). To schedule something on a tainted node, give the pod a toleration for that role's taint and, if wanted, a `nodeSelector` such as `role: light-worker`. A blank role is rejected, and a role the board does not offer (for example `light-worker` on a Pi 5) is rejected with a clear message at apply time.

Keep the number of K3s servers odd. With the NucBox plus two control-plane Pi 5 there are three, which tolerates one failure; with two there is no fault tolerance until the third is in.

### K3s Server Options (control-plane role)
These trim or tune what a K3s server runs. On a Pi 3B+ they start on (the profile does not make a 900 MB board comfortable, and embedded etcd can be slow on SD cards); on a Pi 5 they start off.

| Option | Pi 3B+ | Pi 5 | Notes |
| --- | --- | --- | --- |
| Disable ServiceLB | on | off | No svclb pods. LoadBalancer Services stay pending. |
| Disable metrics-server | on | off | No `kubectl top` or HPA. |
| Disable local-path storage | on | off | No default StorageClass. |
| Disable network policy controller | on | off | NetworkPolicy objects are not enforced. |
| Disable cloud controller | off | off | Optional. |
| Disable Helm controller | off | off | Only valid with ingress `none`, because Traefik is installed through Helm. |
| Ingress | `traefik` | `traefik` | `none` also disables Traefik. ingress-nginx is not offered: the Kubernetes project retired it in March 2026 (no releases or security fixes). |

Rules that bite:
- `--disable` actively uninstalls the component from the running cluster, not just on that node.
- `servicelb`, `disable-network-policy`, `disable-helm-controller` and `disable-cloud-controller` must be identical on every server or joins fail with `critical configuration value mismatch`. Keep the other disables identical too, so servers do not fight over redeploying manifests. The NucBox runs everything, so a control-plane Pi must match it: leave the options off (the Pi 5 default), or, if you turn any on, apply the same set to the NucBox, which `scripts/k3s_fresh_start.sh --lightweight` writes to `/etc/rancher/k3s/config.yaml.d/10-lightweight.yaml` using the Pi 3B+ set. Without `--lightweight` the script leaves the NucBox's components alone.
- Options only appear for the roles they apply to; options that do not apply are ignored by the backend and the roles.

### Fresh Start (rebuild the control plane)
Run on the NucBox before reflashing any Pi 3B+ node:
1. `scripts/k3s_fresh_start.sh --dry-run` to review the plan.
2. `sudo scripts/k3s_fresh_start.sh` (typed password and `YES` confirmation). It backs up the datastore, token and TLS, resets etcd to a single member, restarts K3s and deletes the stale node objects. Add `--lightweight` only if a Pi 3B+ will be a control-plane server. The token is preserved, so tokens saved in Node Ops stay valid.
3. Reflash the Pis. Light workers join as agents and can be brought up in any order. If you do run Pi 3B+ servers, join them one at a time and wait for each to be `Ready`: with two etcd members there is no fault tolerance until the third joins.

Never reflash a control node while it is still an etcd member: removing it first is what keeps quorum.

### Joining A Control-Plane Node (order that worked)
1. Image the card from the Home page with Tailscale enabled and an auth key for the node's tag. First boot installs Tailscale, joins the tailnet, and writes the Tailscale address into the K3s config as `node-ip` before K3s starts. This is fail-safe: if the join fails, cloud-init continues and Node Ops can finish it. The auth key is never stored in job records or auto-saved templates.
2. Select the Kubernetes module on the node first, then save its inputs. Saving inputs for an unselected module is ignored (the response lists it under `ignored_unselected_modules`).
3. Save the NucBox Tailscale endpoint and the current server token. Leave `node_ip` blank to use the node's Tailscale IPv4.
4. Apply the Kubernetes module, confirm `etcdctl member list` shows the new peer on its Tailscale address, then join the next node. A dry run cannot show file content, so confirm the written `/etc/rancher/k3s/config.yaml` is multi-line YAML before relying on it.
5. A Node Ops run that matches no hosts is reported as failed (Ansible itself exits 0 for it), and validation messages are no longer hidden by `no_log`.

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
4. For Pi 3B+, the default Ubuntu 24.04 ARM64 image applies automatically (same as Pi 5), or pass `--image-url` explicitly:
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
- Long-form blocks such as the K3s config are syntax-checked when the field loses focus (`POST /api/config/check`, result shown under the field) and re-checked server-side on SD prepare and Node Ops save, so a bad block is rejected even if the page check is skipped. To cover another block, add a checker to `LONGFORM_CHECKS` in `callback_site/core.py` and set `"check": "<kind>"` on its input spec.
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
