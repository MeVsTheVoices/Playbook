# Headlamp: optional Kubernetes web UI

This installs upstream Headlamp, not Playbook's Kubernetes page. Kubernetes does
not include a web UI by default. Headlamp is the maintained replacement
recommended by Kubernetes for the archived Kubernetes Dashboard.

1. Wait until your cluster and its worker nodes are ready. In Playbook's
   Kubernetes page, open Helm configurations, select Headlamp under Bundled
   charts, and choose Import defaults. Importing does not deploy anything.
2. Set Namespace to headlamp, keep the generated Release name, and save.
   Create the namespace with `kubectl create namespace headlamp` using the
   intended cluster context. Preview, review the resources, then explicitly
   deploy. The locked upstream chart is downloaded during preview/deployment;
   this requires access to the Headlamp Helm repository and GitHub.
3. Keep Tailscale workload exposure Off for local access. On a trusted machine
   with kubectl connected to that cluster, run:

   kubectl -n headlamp port-forward svc/RELEASE_NAME 8080:80

   Replace RELEASE_NAME with your saved release name (normally headlamp-xxxxxx).
   Open http://127.0.0.1:8080 on the same machine. Port-forward binds to loopback
   by default; do not bind it to public interfaces.
4. Sign in with a short-lived token for a dedicated ServiceAccount that your
   administrator has bound to the required roles. For read-only cluster
   inspection, an administrator can run:

   kubectl -n headlamp create serviceaccount headlamp-viewer
   kubectl create clusterrolebinding headlamp-viewer --clusterrole=view --serviceaccount=headlamp:headlamp-viewer
   kubectl -n headlamp create token headlamp-viewer --duration=1h

   These are explicit, optional access changes, not chart defaults. The view
   role cannot read Secrets or manage workloads and may not list all cluster
   resources. Use deliberately scoped roles for additional access, not an
   automatic cluster-admin binding. Tokens are credentials: never paste them
   into Playbook's values or configuration names. Renew expired tokens using
   kubectl. The chart does not create login tokens or privileged user bindings.
5. For private tailnet HTTPS access instead, install and configure the Tailscale
   operator first, then select HTTPS exposure in Playbook. Use the actual Service
   name shown in Preview (normally your release name), Service port 80, and your
   desired hostname. Save, preview, and explicitly deploy again. Keep Headlamp
   authentication enabled; tailnet access does not replace Kubernetes RBAC.

No persistent storage, public ingress, NodePort, LoadBalancer or login bypass
is enabled by default. The pod's ServiceAccount has no cluster-admin binding.
Use nodeSelector/tolerations if you need to constrain placement.

Upstream installation and authentication documentation:
https://headlamp.dev/docs/latest/installation/in-cluster/
https://headlamp.dev/docs/latest/installation/

To remove the optional read-only account later:

kubectl delete clusterrolebinding headlamp-viewer
kubectl -n headlamp delete serviceaccount headlamp-viewer

These access resources are created manually and are not removed by uninstalling
the Helm release.
