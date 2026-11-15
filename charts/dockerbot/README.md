# Dockerbot Helm chart

Runs the same [Red Discord Bot container](https://github.com/rHomelab/Red-DiscordBot-Docker)
as the recovered [Tower-2 Compose project](../../tower-2-compose/dockerbot/).
The default `latest-noaudio` image omits Audio cog support and does not deploy
Lavalink. It preserves the instance name, prefix and extra Python packages from
Compose. The image tag is mutable; set `image.tag` to a tested version for
repeatable deployments. No Service or ingress is required: the bot connects
outbound to Discord. If migrating an instance that used the Audio cog, that cog
will not work with the no-audio image.

For the Discord application, bot token, intent and invite steps, see the
[quick setup guide](./PLAYBOOK_GUIDE.md). Playbook's Kubernetes page includes
this chart and guide and can write the token to the configured Secret.

## Install

1. Make sure a default StorageClass exists, or install Longhorn and use the
   Longhorn example below. Longhorn is optional in this repository; see the
   [Longhorn notes](../../callback_site/README.md#longhorn-notes).
2. Create the namespace and an existing Secret in it. The token file must
   contain only the Discord bot token, with no trailing newline, and should be
   owner-readable only and outside the repository. Avoid putting tokens in
   Helm values, command arguments, or terminal history.

   ```bash
   kubectl create namespace discord
   kubectl -n discord create secret generic dockerbot-token \
     --from-file=token=/secure/path/discord-token
   ```

3. Stop the old Compose bot before starting this one. Do not run both instances
   with the same token. To install using Longhorn's `longhorn` StorageClass:

   ```bash
   helm upgrade --install dockerbot ./charts/dockerbot \
     --namespace discord --set persistence.storageClassName=longhorn
   kubectl -n discord rollout status deployment/dockerbot-dockerbot --timeout=10m
   kubectl -n discord logs deployment/dockerbot-dockerbot
   ```

   Omit `--set persistence.storageClassName=longhorn` to use the cluster's
   default StorageClass. A missing class or insufficient capacity leaves the
   claim pending. The default may be node-local rather than replicated.

The PVC is `dockerbot-dockerbot` for the release above, mounted at
`/redbot/data`. It requests 10Gi and `ReadWriteOnce`. The single-replica
Deployment uses a Recreate strategy to avoid overlapping bot instances and
conflicting volume mounts during upgrades. The image entrypoint runs as root
initially to set data ownership, then starts Red as its unprivileged user.

Helm normally deletes its PVC on uninstall or when an upgrade stops rendering
it, potentially deleting its data according to the StorageClass reclaim
policy. Back up data before uninstalling or changing persistence settings.
To manage its lifecycle separately, create a PVC first and set
`persistence.existingClaim` to its name; Helm then neither creates nor deletes
that PVC. `persistence.enabled=false` uses ephemeral storage and **loses data
when the pod is replaced**; use it only for disposable testing.

The Secret is not managed by the chart. Set `bot.existingSecret` and
`bot.tokenKey` if using different names. After rotating the Secret, restart the
Deployment so the process receives the new environment variable:

```bash
kubectl -n discord rollout restart deployment/dockerbot-dockerbot
```

## Migrate existing Compose data

Back up `/mnt/user/appdata/redbot` first, then stop the Compose bot. Install the
chart as above, scale its Deployment to zero, and wait for its pod and volume
to detach. From a machine with access to the old data and the cluster, create a
temporary pod mounting the claim, copy the data, delete the pod, and scale the
Deployment back up. The example uses the default release, namespace and claim:

```bash
kubectl -n discord scale deployment/dockerbot-dockerbot --replicas=0
kubectl -n discord delete pod --ignore-not-found --wait=true \
  -l app.kubernetes.io/name=dockerbot,app.kubernetes.io/instance=dockerbot \
  --timeout=120s
kubectl -n discord apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: dockerbot-data-migration
spec:
  restartPolicy: Never
  containers:
    - name: copy
      image: alpine:3.21
      command: ["sleep", "3600"]
      volumeMounts:
        - name: data
          mountPath: /redbot/data
  volumes:
    - name: data
      persistentVolumeClaim:
        claimName: dockerbot-dockerbot
EOF
kubectl -n discord wait --for=condition=Ready pod/dockerbot-data-migration --timeout=300s
set -o pipefail
if tar -C /mnt/user/appdata/redbot -cf - . | \
  kubectl -n discord exec -i dockerbot-data-migration -- tar -C /redbot/data -xf -; then
  kubectl -n discord delete pod dockerbot-data-migration --wait=true &&
    kubectl -n discord scale deployment/dockerbot-dockerbot --replicas=1 &&
    kubectl -n discord rollout status deployment/dockerbot-dockerbot --timeout=10m
else
  echo "Copy failed; bot remains scaled down" >&2
fi
```

Check the copied bot state and logs before discarding the backup. The copy
requires `tar` on the source machine and in the temporary container, and
`pipefail` support in the local shell. If the copy fails, leave the bot scaled
down, investigate and repeat before restarting.
Adjust names if you changed the release, namespace or claim. Switching an
existing PVC's StorageClass does not migrate its data; provision a new claim and
copy the data instead.
