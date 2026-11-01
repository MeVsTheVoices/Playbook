#!/usr/bin/env bash
# Fresh start for the K3s control plane, run ON THE NUCBOX before reflashing the Pi 3B+ nodes.
#
# What it does, in order:
#   1. Backs up the K3s datastore, certs and token (K3s is stopped, so the copy is consistent).
#   2. With --lightweight only: writes /etc/rancher/k3s/config.yaml.d/10-lightweight.yaml.
#      Needed only when a Pi 3B+ runs as a control-plane SERVER: K3s refuses to let servers join
#      when these settings differ ("critical configuration value mismatch"), and --disable actively
#      uninstalls components. Pi 3B+ light workers and utility nodes need none of this.
#   3. Resets etcd to a single member (this node), so a dead or reflashed Pi can never cost quorum.
#   4. Starts K3s, removes the stale node objects of the Pis you are about to reflash, and verifies.
#
# Usage:
#   sudo scripts/k3s_fresh_start.sh [--dry-run] [--yes] [--keep-nodes] [--lightweight [--option KEY=VALUE ...]]
#
#   --dry-run      Print the plan; change nothing (no root needed).
#   --yes          Do not ask for the YES confirmation.
#   --keep-nodes   Do not delete the other node objects.
#   --lightweight  Also write the lightweight server drop-in (see step 2).
#   --option       With --lightweight, same keys the UI uses, e.g. --option k3s_ingress=none
#                  --option k3s_disable_metrics_server=false
#
# The token is preserved by a cluster reset, so tokens already saved in Node Ops stay valid.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/../callback_site"
DROPIN_DIR="${K3S_DROPIN_DIR:-/etc/rancher/k3s/config.yaml.d}"
DROPIN_FILE="${DROPIN_DIR}/10-lightweight.yaml"
DATA_DIR="${K3S_DATA_DIR:-/var/lib/rancher/k3s}"
K3S_BIN="${K3S_BIN:-/usr/local/bin/k3s}"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${K3S_BACKUP_ROOT:-/var/backups}/k3s-fresh-start-${TS}"

DRY_RUN="false"
ASSUME_YES="false"
KEEP_NODES="false"
LIGHTWEIGHT="false"
OPTIONS=()

usage() { sed -n '2,/^set -euo pipefail/{/^set -euo/!p}' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN="true"; shift ;;
    --yes) ASSUME_YES="true"; shift ;;
    --keep-nodes) KEEP_NODES="true"; shift ;;
    --lightweight) LIGHTWEIGHT="true"; shift ;;
    --option)
      [[ $# -ge 2 && "$2" == *=* ]] || { echo "--option needs KEY=VALUE" >&2; exit 2; }
      OPTIONS+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

render_dropin() {
  python3 - "$LIB_DIR" "${OPTIONS[@]+"${OPTIONS[@]}"}" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import k3s_lightweight as lw

options = {}
for item in sys.argv[2:]:
    key, _, value = item.partition("=")
    if key not in lw.LIGHTWEIGHT_OPTION_IDS:
        sys.exit(f"Unknown option '{key}'. Known: {', '.join(lw.LIGHTWEIGHT_OPTION_IDS)}")
    options[key] = value
text, errors = lw.apply_to_k3s_config("", options)
if errors:
    sys.exit("; ".join(errors))
print("# Managed by scripts/k3s_fresh_start.sh. Must match on every K3s server in the cluster.")
print(text.strip())
PY
}

DROPIN_CONTENT=""
if [[ "$LIGHTWEIGHT" == "true" ]]; then
  DROPIN_CONTENT="$(render_dropin)"
  echo "== Lightweight settings that will be applied to this server =="
  printf '%s\n' "$DROPIN_CONTENT"
elif (( ${#OPTIONS[@]} > 0 )); then
  echo "--option only applies together with --lightweight" >&2
  exit 2
else
  echo "== No lightweight drop-in (Pi 3B+ nodes will be light workers or utility nodes, not servers) =="
fi
echo

if [[ "$DRY_RUN" == "true" ]]; then
  echo "== Dry run: nothing changed =="
  echo "Would back up ${DATA_DIR}/server/{db,tls,cred,token,node-token} to ${BACKUP_DIR}"
  [[ "$LIGHTWEIGHT" == "true" ]] && echo "Would write ${DROPIN_FILE}"
  echo "Would stop K3s, run '--cluster-reset' with this server's current flags, start K3s again"
  if [[ "$KEEP_NODES" == "true" ]]; then
    echo "Would keep all other node objects"
  else
    echo "Would delete every node object except this machine's"
  fi
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo $0 $*" >&2
  exit 1
fi
[[ -x "$K3S_BIN" ]] || { echo "K3s binary not found at ${K3S_BIN}" >&2; exit 1; }
systemctl cat k3s >/dev/null 2>&1 || { echo "No k3s systemd unit on this machine" >&2; exit 1; }
[[ -d "${DATA_DIR}/server/db" ]] || { echo "No K3s datastore at ${DATA_DIR}/server/db; is this the server?" >&2; exit 1; }

# Reuse the exact flags the unit starts with, so the reset targets the same address/identity.
EXEC_LINE="$(systemctl show k3s -p ExecStart --value)"
CMDLINE="$(sed -n 's/.*argv\[\]=\([^;]*\) ;.*/\1/p' <<<"$EXEC_LINE" | head -n1)"
if [[ "$CMDLINE" != *" server "* ]]; then
  echo "Could not read this server's start flags from systemd; refusing to guess." >&2
  exit 1
fi
SERVER_ARGS="${CMDLINE#*server }"
[[ "$SERVER_ARGS" == *--cluster-init* ]] || SERVER_ARGS="--cluster-init ${SERVER_ARGS}"

echo "== Plan =="
echo "  Start flags kept:   ${SERVER_ARGS}"
echo "  Backup:             ${BACKUP_DIR}"
echo "  Drop-in:            $([[ "$LIGHTWEIGHT" == "true" ]] && echo "$DROPIN_FILE" || echo none)"
echo "  etcd:               reset to a single member (this node)"
echo "  Other node objects: $([[ "$KEEP_NODES" == "true" ]] && echo kept || echo deleted)"
echo
if [[ "$ASSUME_YES" != "true" ]]; then
  read -r -p "Type YES to continue: " CONFIRM
  [[ "$CONFIRM" == "YES" ]] || { echo "Aborted."; exit 1; }
fi

LOG="${K3S_LOG_DIR:-/var/log}/k3s-fresh-start-${TS}.log"
exec > >(tee -a "$LOG") 2>&1
echo "Logging to ${LOG}"

STOPPED="false"
FINISHED="false"
on_exit() {
  local code=$?
  if [[ "$STOPPED" == "true" && "$FINISHED" != "true" && $code -ne 0 ]]; then
    echo >&2
    echo "FAILED (exit ${code}). K3s may be stopped. The backup is intact at: ${BACKUP_DIR}" >&2
    echo "To roll back: systemctl stop k3s; rm -rf ${DATA_DIR}/server/db; cp -a ${BACKUP_DIR}/db ${DATA_DIR}/server/db;" >&2
    echo "              rm -f ${DROPIN_FILE}; systemctl start k3s" >&2
  fi
}
trap on_exit EXIT

echo "== 1/5 Stopping K3s and backing up =="
systemctl stop k3s
STOPPED="true"
install -d -m 0700 "$BACKUP_DIR"
for item in db tls cred token node-token; do
  if [[ -e "${DATA_DIR}/server/${item}" ]]; then
    cp -a "${DATA_DIR}/server/${item}" "${BACKUP_DIR}/${item}"
  fi
done
[[ -d "${BACKUP_DIR}/db" ]] || { echo "Backup of the datastore failed" >&2; exit 1; }
echo "Backed up: $(ls "$BACKUP_DIR" | tr '\n' ' ')"

if [[ "$LIGHTWEIGHT" == "true" ]]; then
  echo "== 2/5 Writing the lightweight drop-in =="
  install -d -m 0755 "$DROPIN_DIR"
  if [[ -f "$DROPIN_FILE" ]]; then
    cp -a "$DROPIN_FILE" "${BACKUP_DIR}/10-lightweight.yaml.previous"
  fi
  printf '%s\n' "$DROPIN_CONTENT" > "$DROPIN_FILE"
  chmod 0644 "$DROPIN_FILE"
else
  echo "== 2/5 No lightweight drop-in requested; skipping =="
fi

echo "== 3/5 Resetting etcd to a single member =="
RESET_OUT="$(mktemp)"
# shellcheck disable=SC2086  # SERVER_ARGS is a flag list and must word-split
if ! timeout 300 "$K3S_BIN" server $SERVER_ARGS --cluster-reset >"$RESET_OUT" 2>&1; then
  tail -n 25 "$RESET_OUT" >&2
  rm -f "$RESET_OUT"
  exit 1
fi
if ! grep -qi 'cluster membership has been reset\|restart without --cluster-reset' "$RESET_OUT"; then
  echo "The reset command ended without its success message; last lines:" >&2
  tail -n 25 "$RESET_OUT" >&2
  rm -f "$RESET_OUT"
  exit 1
fi
rm -f "$RESET_OUT"
echo "etcd membership reset."

echo "== 4/5 Starting K3s =="
systemctl start k3s
for _ in $(seq 1 90); do
  if "$K3S_BIN" kubectl get --raw=/readyz >/dev/null 2>&1; then
    READY="yes"
    break
  fi
  sleep 2
done
[[ "${READY:-no}" == "yes" ]] || { echo "K3s did not become ready within 180s; check: journalctl -u k3s" >&2; exit 1; }
echo "API is ready."

if [[ "$KEEP_NODES" != "true" ]]; then
  SELF="$(hostname | tr '[:upper:]' '[:lower:]')"
  mapfile -t ALL_NODES < <("$K3S_BIN" kubectl get nodes -o name 2>/dev/null | sed 's#^node/##')
  mapfile -t STALE < <(printf '%s\n' "${ALL_NODES[@]+"${ALL_NODES[@]}"}" | grep -vx "$SELF" | grep -v '^$' || true)
  if ! printf '%s\n' "${ALL_NODES[@]+"${ALL_NODES[@]}"}" | grep -qx "$SELF"; then
    echo "WARNING: this machine's node '${SELF}' is not in the node list; skipping node deletion to be safe."
    STALE=()
  fi
  if (( ${#STALE[@]} > 0 )); then
    echo "Deleting stale node objects: ${STALE[*]}"
    "$K3S_BIN" kubectl delete node "${STALE[@]}" --wait=false || true
  else
    echo "No other node objects to delete."
  fi
fi

echo "== 5/5 Verifying =="
sleep 20
"$K3S_BIN" kubectl get nodes -o wide
echo
echo "kube-system workloads (disabled components should be gone or terminating):"
"$K3S_BIN" kubectl -n kube-system get deploy,daemonset,pods 2>/dev/null | head -n 30
echo
FINISHED="true"
echo "Done. Backup kept at ${BACKUP_DIR}; log at ${LOG}."
echo
echo "Next: reflash the Pi 3B+ nodes. Join them ONE AT A TIME and wait for each to show Ready before the next."
echo "With two etcd members there is no fault tolerance until the third is in, so a stalled joiner can block the control plane."
