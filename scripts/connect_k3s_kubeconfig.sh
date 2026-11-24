#!/usr/bin/env bash
# Connect the containerised dashboard to the K3s server running on this Linux host.
#
#   scripts/connect_k3s_kubeconfig.sh
#
# Exports /etc/rancher/k3s/k3s.yaml (via sudo) to .local/kubeconfig with the API
# endpoint rewritten from 127.0.0.1 to a host address the container can reach,
# records the Kubernetes Compose overlay in .env so later `docker compose up`
# runs keep it, then recreates the dashboard and checks kubectl inside it.
# K3S_API_HOST overrides the address; it must be in the API certificate's SANs.
#
#   K3S_API_SERVERS=auto scripts/connect_k3s_kubeconfig.sh
#
# With K3S_API_SERVERS (auto, or host:port pairs, comma separated) the dashboard instead reaches the
# API through the k3s-api load balancer in compose.k3s-api.yaml, so no single server is required.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${K3S_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
TARGET="$REPO_DIR/.local/kubeconfig"
ENV_FILE="$REPO_DIR/.env"
OVERLAY="compose.yaml:compose.kubernetes.yaml"
servers="${K3S_API_SERVERS:-}"

cd "$REPO_DIR"

if ! sudo test -r "$SOURCE"; then
  echo "K3s kubeconfig not found at $SOURCE; install and start K3s first." >&2
  exit 1
fi

api_host="${K3S_API_HOST:-}"
if [[ -z "$api_host" ]]; then
  api_host="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
fi
if [[ -z "$api_host" ]]; then
  echo "Could not detect a host address; set K3S_API_HOST." >&2
  exit 1
fi

config="$(sudo cat "$SOURCE")"
port="$(sed -nE 's#^[[:space:]]*server:[[:space:]]*https://[^:/]+:([0-9]+).*#\1#p' <<<"$config" | head -n 1)"
if [[ -z "$port" ]]; then
  echo "Could not read the API server port from $SOURCE." >&2
  exit 1
fi

# Persist a value in .env so later `docker compose up` runs keep it.
set_env() {
  if [[ -f "$ENV_FILE" ]] && grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s#^$1=.*#$1=$2#" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
  echo "Set $1=$2 in .env"
}

if [[ "$servers" == auto ]]; then
  servers="$(sudo k3s kubectl get nodes -l node-role.kubernetes.io/control-plane=true \
    -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{":6443,"}{end}' | sed 's/,$//')"
  if [[ -z "$servers" ]]; then
    echo "K3S_API_SERVERS=auto found no control-plane nodes; list them as host:port pairs instead." >&2
    exit 1
  fi
fi
if [[ -n "$servers" ]]; then
  if [[ ! "$servers" =~ ^[A-Za-z0-9.:_-]+:[0-9]+(,[A-Za-z0-9.:_-]+:[0-9]+)*$ ]]; then
    echo "K3S_API_SERVERS must be auto or host:port pairs, comma separated (got '$servers')." >&2
    exit 1
  fi
  OVERLAY="$OVERLAY:compose.k3s-api.yaml"
fi

mkdir -p "$REPO_DIR/.local"
chmod 0700 "$REPO_DIR/.local"
# 0644 so the container's non-root user can read the bind mount; the 0700 directory keeps it private on the host.
tmp="$(mktemp "$REPO_DIR/.local/kubeconfig.XXXXXX")"
if [[ -n "$servers" ]]; then
  # Every K3s serving certificate names kubernetes.default, so TLS verifies whichever server answers.
  sed -E "s#^([[:space:]]*)server:[[:space:]]*https://[^[:space:]]+#\1server: https://k3s-api:6443\n\1tls-server-name: kubernetes.default#" <<<"$config" > "$tmp"
  api_url="https://k3s-api:6443 -> $servers"
else
  sed -E "s#^([[:space:]]*server:[[:space:]]*)https://[^[:space:]]+#\1https://${api_host}:${port}#" <<<"$config" > "$tmp"
  api_url="https://${api_host}:${port}"
fi
chmod 0644 "$tmp"
mv -f "$tmp" "$TARGET"
echo "Wrote $TARGET (API $api_url)"

if [[ -n "$servers" ]]; then
  set_env K3S_API_SERVERS "$servers"
fi
set_env COMPOSE_FILE "$OVERLAY"

docker compose up --build -d --force-recreate callback-site
docker compose exec -T callback-site kubectl get nodes --request-timeout=10s
