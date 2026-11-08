#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCANNER_IMAGE="zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
MODE="${1:-history}"

case "${MODE}" in
  history)
    SCANNER_ARGS=(git /repo --config /repo/.gitleaks.toml --redact --log-opts=--all)
    ;;
  staged)
    SCANNER_ARGS=(git /repo --config /repo/.gitleaks.toml --pre-commit --staged --redact)
    ;;
  *)
    echo "Usage: bash scripts/check_secrets.sh [history|staged]" >&2
    exit 2
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker is required for the pinned secret scanner." >&2
  exit 1
fi

docker run --rm \
  --network none \
  --volume "${ROOT_DIR}:/repo:ro" \
  "${SCANNER_IMAGE}" "${SCANNER_ARGS[@]}"
