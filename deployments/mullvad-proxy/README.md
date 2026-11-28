# Mullvad proxy pool

This is an independent, cluster-internal HTTP CONNECT and SOCKS5 TCP proxy,
not part of Servarr. It uses the [3proxy container image](https://github.com/tarampampam/3proxy)
and one Tailscale sidecar per exit node. Check that project's license and the
licenses of its dependencies before redistributing it.

The Service `mullvad-proxy.mullvad-proxy.svc.cluster.local` listens on TCP
3128 (HTTP) and 1080 (SOCKS). Both require the credentials in the
`mullvad-proxy-auth` Secret. Kubernetes chooses a backend per TCP connection;
it does not guarantee round-robin distribution, and a long-lived connection
stays on the same backend. Clients must explicitly use the proxy; this is not
a transparent gateway. SOCKS UDP and IPv6 destinations are not supported.

Proxy connections to private, loopback, link-local, and Tailscale/CGNAT IPv4
addresses are blocked. Proxy DNS uses 1.1.1.1 and 1.0.0.1 over the selected
exit node, rather than cluster DNS. The Tailscale bootstrap itself uses
Kubby's cluster DNS (`10.43.0.10`) and can use the underlying network. The
guard routes replies to Kubby's pod CIDR (`10.42.0.0/16`) locally; adjust both
addresses if the cluster configuration changes. The proxy process can only
connect outward through `tailscale0`; its IPv6 traffic is blocked. The pod is
removed from the Service when its exit node is not online.

## Setup and credentials

Create the namespace and two namespace-local Secrets before applying the
project:

```sh
kubectl apply -f deployments/mullvad-proxy/namespace.yaml
# Create mullvad-proxy-auth with keys username and password.
# Create mullvad-proxy-tailscale with key authkey.
kubectl apply -k deployments/mullvad-proxy
kubectl rollout status -n mullvad-proxy deployment/mullvad-proxy-los-angeles
kubectl rollout status -n mullvad-proxy deployment/mullvad-proxy-seattle
```

Proxy credentials must be nonempty and contain only letters, digits, dot,
underscore, or hyphen, without trailing newlines. Create them from protected
local files with `kubectl create secret generic mullvad-proxy-auth -n mullvad-proxy
--from-file=username=PATH --from-file=password=PATH`; do not commit them.
Generate a reusable, ephemeral, preauthorized Tailscale auth key tagged
`tag:needs-exit`, and create the other Secret with `--from-file=authkey=PATH`.
The tailnet policy must grant that tag access to Mullvad exit nodes and
internet traffic. Each active proxy consumes a Mullvad device slot. Initial
registration may take several minutes.

Auth keys expire: replace the namespace Secret before its expiration, and
restart the Deployments one at a time to use the replacement. When rotating
proxy credentials, update the Secret and restart both Deployments; clients
must use the new credentials. Use protected files and avoid exposing secrets
in command history, process arguments, or logs. Kubernetes Secret access is
sensitive; restrict RBAC and enable encryption at rest as appropriate.

The proxy uses its own scoped auth key, separate from the qBittorrent OAuth
credential.

## Client example

From a pod with the proxy credentials available as `PROXY_USER` and
`PROXY_PASSWORD`, an HTTP client can verify its exit without placing the
password in curl's arguments:

```sh
printf 'proxy = "http://mullvad-proxy.mullvad-proxy.svc.cluster.local:3128"\nproxy-user = "%s:%s"\n' \
  "$PROXY_USER" "$PROXY_PASSWORD" |
  curl --config - --fail --silent --show-error https://am.i.mullvad.net/json
```

On a trusted administrator machine with access to the namespace, retrieve
these values into shell variables without printing them:

```sh
set +x
PROXY_USER=$(kubectl get secret -n mullvad-proxy mullvad-proxy-auth -o jsonpath='{.data.username}' | base64 -d)
PROXY_PASSWORD=$(kubectl get secret -n mullvad-proxy mullvad-proxy-auth -o jsonpath='{.data.password}' | base64 -d)
```

Provide those variables securely to the client pod; the Service DNS name is
only reachable from within the cluster.

For SOCKS5 with proxy-side DNS, use the same command with the proxy URL
`socks5h://mullvad-proxy.mullvad-proxy.svc.cluster.local:1080`. The response
should report `"mullvad_exit_ip": true`. The HTTP and SOCKS protocols provide
authentication, not transport encryption between client and proxy; use only
on a trusted cluster network, and use HTTPS for the destination.

Inspect ready endpoints and Tailscale status with:

```sh
kubectl get endpointslices -n mullvad-proxy -l kubernetes.io/service-name=mullvad-proxy
kubectl exec -n mullvad-proxy deployment/mullvad-proxy-los-angeles -c tailscale -- tailscale status
kubectl exec -n mullvad-proxy deployment/mullvad-proxy-seattle -c tailscale -- tailscale status
```
