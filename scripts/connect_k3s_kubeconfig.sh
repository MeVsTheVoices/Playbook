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
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="${K3S_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
TARGET="$REPO_DIR/.local/kubeconfig"
ENV_FILE="$REPO_DIR/.env"
OVERLAY="compose.yaml:compose.kubernetes.yaml"

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

mkdir -p "$REPO_DIR/.local"
chmod 0700 "$REPO_DIR/.local"
# 0644 so the container's non-root user can read the bind mount; the 0700 directory keeps it private on the host.
tmp="$(mktemp "$REPO_DIR/.local/kubeconfig.XXXXXX")"
sed -E "s#^([[:space:]]*server:[[:space:]]*)https://[^[:space:]]+#\1https://${api_host}:${port}#" <<<"$config" > "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$TARGET"
echo "Wrote $TARGET (API https://${api_host}:${port})"

if [[ -f "$ENV_FILE" ]] && grep -q '^COMPOSE_FILE=' "$ENV_FILE"; then
  sed -i "s#^COMPOSE_FILE=.*#COMPOSE_FILE=${OVERLAY}#" "$ENV_FILE"
else
  printf 'COMPOSE_FILE=%s\n' "$OVERLAY" >> "$ENV_FILE"
fi
echo "Set COMPOSE_FILE=${OVERLAY} in .env"

docker compose up --build -d --force-recreate callback-site
docker compose exec -T callback-site kubectl get nodes --request-timeout=10s
