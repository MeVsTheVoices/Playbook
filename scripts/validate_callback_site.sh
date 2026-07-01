#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${ROOT_DIR}/.venv/bin/python"
CALLBACK_BASE="${CALLBACK_BASE:-http://127.0.0.1:8088}"

require_endpoint() {
  local path="$1"
  local url="${CALLBACK_BASE}${path}"
  local code
  code="$(curl -sS -o /dev/null -w "%{http_code}" "${url}" || true)"

  if [[ "${code}" == "200" ]]; then
    echo "OK ${path}"
    return 0
  fi

  if [[ "${code}" == "404" && "${path}" == "/api/test-terminal/checks" ]]; then
    echo "ERROR ${path} returned 404. The callback service is likely running an older build."
    echo "Restart callback_site and re-run this script."
    return 1
  fi

  echo "ERROR ${path} returned HTTP ${code}."
  return 1
}

echo "[1/3] Running callback_site unit tests"
if [[ ! -x "${VENV_PY}" ]]; then
  echo "ERROR: ${VENV_PY} not found. Run ./scripts/bootstrap_project.sh first."
  exit 1
fi
"${VENV_PY}" -m unittest discover -s "${ROOT_DIR}/callback_site/tests" -p 'test_*.py'

echo "[2/3] Checking local callback endpoints"
require_endpoint "/health"
require_endpoint "/api/cloud-init/events"
require_endpoint "/api/test-terminal/checks"

echo "[3/3] Posting a synthetic callback event"
curl -fsS -X POST "${CALLBACK_BASE}/api/cloud-init/callback" \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"preflight-node","instance_id":"preflight-01","stage":"phone_home","profile":"pi5"}' >/dev/null

echo "Validation complete: unit tests and callback APIs are healthy."
