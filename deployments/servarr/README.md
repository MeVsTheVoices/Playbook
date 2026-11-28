# Servarr deployment

The [upstream overlay](upstream/) renders the public
[Kubito Servarr chart](https://github.com/kubitodev/helm/tree/main/charts/servarr)
at chart version 1.5.2. Its rendered resources were verified against the
previous 1.3.0 deployment. The chart is fetched
from `https://charts.kubito.dev` by Helm; its downloaded cache is not committed.
Local values preserve the existing applications, Tailscale ingresses, and PVC
bindings. Local patches add qBittorrent's Tailscale sidecar and VPN guard; Seerr
keeps its non-root security context. Secrets and PVCs remain separately managed.

The original Kustomize source was not found. If it is recovered, reconcile it
with this overlay before using it: another apply could overwrite these patches.
The project in this directory remains a qBittorrent-only fallback reconstructed
from the previous live Deployment.

The guard assumes Kubby's pod CIDR `10.42.0.0/16`, cluster DNS
`10.43.0.10`, and qBittorrent `PUID=1000`. Adjust these together with the
cluster configuration if they change. Tailscale's marked transport and its
root-owned HTTPS authentication may use the underlying network; qBittorrent's
unprivileged process may not. The guard is installed before either container
starts and remains in the pod network namespace if the sidecar restarts.

The sidecar waits up to five minutes for an online exit node before initial
startup completes, and readiness reflects exit-node availability. Initial
Mullvad registration can take several minutes. Application DNS queries use
cluster DNS, whose upstream may not use Mullvad.

The sidecar reads `client_secret` from the `tailscale-mullvad-oauth` Secret
in the `servarr` namespace. Its OAuth credential requires Auth Keys write
access and the `tag:needs-exit` tag. The Secret also stores `client_id`.
Keep both values out of Git and command arguments. To create or replace the
Secret using protected files without trailing newlines:

```sh
kubectl create secret generic tailscale-mullvad-oauth -n servarr \
  --from-file=client_id=PATH --from-file=client_secret=PATH \
  --dry-run=client -o yaml | kubectl apply -f -
```

Restart qBittorrent after rotating the credential so the sidecar loads it;
revoke the old credential in Tailscale. Restarting briefly interrupts service.

## Dashboard deployment

In Kubernetes > Helm, import **Servarr (media suite)** and select **Load
existing Kubby preset**. Save, preview, and use **Compare live (kubectl)** to
review the differences before confirming deployment. The preset uses `kubectl apply` without
pruning, existing PVCs, and the VPN. It does not create a Helm release or take
ownership of existing resources; the dashboard refuses to apply Helm-owned
resources, PVCs or Secrets. Never switch deployment methods without planning
ownership and backing up data. Deployment submission does not wait for
rollouts: check all workloads and VPN egress afterward.

For new installations, Helm install/upgrade is available and the VPN is off by
default. Enabling it requires a separately created Secret, an exit node and
correct network settings. Turning it off can expose qBittorrent's direct
traffic; the dashboard asks for a separate confirmation. The firewall blocks
the configured unprivileged LinuxServer PUID, not arbitrary privileged or root
processes, and cluster DNS upstream may bypass the VPN.

The [bundled wrapper](../../charts/servarr/) and the Kustomize overlay use the
same local VPN patch files. Saved dashboard configurations are immutable chart
snapshots; to use a future upstream chart, update the version in the wrapper's
`Chart.yaml` and the overlay's `kustomization.yaml`, regenerate the wrapper's
`Chart.lock` with `helm dependency update`, review upstream changes, then use
**Refresh chart source** on the saved dashboard configuration. Its values and
target stay unchanged. Verify storage, ingress, security, egress and the
complete diff before applying. Saved configurations remain pinned until
explicitly refreshed; the old snapshot is retained, but rollback is manual.

## Render and deploy the upstream chart

Install Helm and kubectl, and use the repository's Python environment with
PyYAML. Render and validate before inspecting the Kubernetes diff. The chart
version is pinned in [upstream/kustomization.yaml](upstream/kustomization.yaml);
to adopt a newer upstream release, change that version and repeat this
procedure. The validator rejects missing VPN protection, unexpected workloads,
namespace changes, PVC changes, missing ingresses, and loss of Seerr's
security context; it does not replace reviewing the entire diff.

```sh
set -eu
manifest=$(mktemp)
trap 'rm -f "$manifest"' EXIT
kubectl kustomize deployments/servarr/upstream --enable-helm > "$manifest"
.venv/bin/python deployments/servarr/upstream/verify_render.py < "$manifest"
if kubectl diff -n servarr -f "$manifest"; then
  echo "No changes"
else
  status=$?
  [ "$status" -eq 1 ] || exit "$status"
fi
# Review the diff, including image, ingress, security and storage changes.
read -r -p "Type apply to deploy the reviewed manifest: " answer
[ "$answer" = apply ] || exit 1
kubectl apply -n servarr -f "$manifest"
kubectl rollout status -n servarr deployment/servarr-qbittorrent --timeout=5m
```

Never use `--prune` here: PVCs and other existing resources are managed
separately. Check all suite rollouts after changes and verify qBittorrent's
actual egress:

```sh
kubectl get deployments -n servarr
kubectl exec -n servarr deployment/servarr-qbittorrent -c servarr -- \
  su -s /bin/sh abc -c 'curl -fsS --max-time 20 https://am.i.mullvad.net/json'
```

The qBittorrent-only fallback is applied with `kubectl apply -k deployments/servarr`.
