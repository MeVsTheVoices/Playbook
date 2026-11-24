# Generic Linux x86_64 profile

`linux_x64_active` is an intentionally empty inventory group for generic
Debian/Ubuntu x86_64 hosts such as NUCs, mini-PCs, and OptiPlex-class systems.
It does not add or reassign a real host. The Node Ops `Generic Linux x86_64`
profile runs the idempotent `generic_linux_x64` Ansible role: refresh apt
metadata, install the configured package baseline, and optionally set timezone
and upgrade packages. Upgrades default off. No Kubernetes role, cluster
endpoint, Tailscale identity, or network settings are assumed.

## 🧩 Optional modules

The same `rack_node` role that provisions the Pis also runs on this group
(`rack_node_board: x64`), so an x86_64 host can opt into **Tailscale**,
**Kubernetes** (worker, control-plane or utility), **Docker** and **Telegraf**
from Node Ops. Nothing extra runs until a module is selected: a new x86_64 node
starts with only **Generic Linux x86_64**, and Tailscale is installed only when
its module is applied, so hosts that already run their own Tailscale are left
alone. The default tags are `tag:linux-x64` and `tag:homelab`
(`group_vars/linux_x64/all.yml`); the Tailscale policy builder lists the x86_64
tag beside the Pi tags. OAuth uses `vault_linux_x64_tailscale_oauth_client_*`,
falling back to the Pi 5 client. Longhorn stays Pi 5-only because its disk
preparation is NVMe-specific.

The role is limited to Debian-family x86_64. Group-level `common_packages` and
`timezone` can be reused or overridden; the Node Ops inputs override those
defaults when provided. Keep credentials and host-specific values in Ansible
Vault or protected host/group vars.

## USB bootstrap workflow

In the provisioning dashboard, select **Generic Linux x86_64 (UEFI USB
installer)**, choose the pinned Ubuntu Server ISO and a verified removable USB
stick discovered by the configured flash-helper service, then create the
installer. The USB is erased during creation. Booting
it on the target runs an unattended baseline OS installation and erases the
target's sole eligible internal disk. This is a separate, explicit
authorization from writing the USB; the installer stops if it cannot identify
exactly one eligible internal disk. Keep the created USB physically
controlled. The installed OS is standard Ubuntu; it does not install K3s or
join a cluster until the Kubernetes module is applied from Node Ops.

After installation, use the resulting callback entry in Node Ops to apply
**Generic Linux x86_64** settings. This repository change was validated without
writing an image, creating installer media, or contacting a physical node.

## Safe checks

```bash
ansible-playbook -i inventory.yml site.yml --syntax-check
PYTHONPATH=callback_site .venv/bin/python -m unittest discover \
  -s callback_site/tests -p 'test_generic_linux_x64.py'
```

For a future operator-approved host, first review the target inventory and
profile inputs. A Node Ops **Check Module** is a dry run; **Apply Module**
changes the selected host and should only be used after confirming its
identity, OS family, and recovery path.
