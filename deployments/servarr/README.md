# qBittorrent VPN deployment

This local Kustomize project manages only the Kubby `servarr-qbittorrent`
Deployment. Its base was reconstructed from the Deployment's last-applied
configuration; `qbittorrent-vpn.patch.yaml` adds the VPN guard. It does not
manage the rest of the Helm-rendered suite, its Services, PVCs, or credentials.
The original Servarr Kustomize source was not found. If it is recovered, add
the patch there as well, or its next apply could overwrite this protection.

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

To apply this local qBittorrent overlay:

```sh
kubectl apply -k deployments/servarr
kubectl rollout status -n servarr deployment/servarr-qbittorrent
```
