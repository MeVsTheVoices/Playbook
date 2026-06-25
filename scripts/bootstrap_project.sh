#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_VENV="$ROOT_DIR/.venv"
CALLBACK_DIR="$ROOT_DIR/callback_site"
CALLBACK_VENV="$CALLBACK_DIR/.venv"
ANSIBLE_REQUIREMENTS="$ROOT_DIR/requirements-ansible.txt"
COLLECTIONS_REQUIREMENTS="$ROOT_DIR/collections/requirements.yml"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VALIDATE_ONLY="false"
SKIP_ANSIBLE="false"
SKIP_CALLBACK="false"
START_CALLBACK="false"
CALLBACK_PORT="8088"

usage() {
  cat <<'EOF'
Bootstrap the Playbook repository for local use.

Usage:
  scripts/bootstrap_project.sh [options]

Options:
  --validate-only   Verify existing bootstrap artifacts without installing.
  --skip-ansible    Skip root Ansible environment setup.
  --skip-callback   Skip callback_site environment setup.
  --start-callback  Start callback_site/app.py after bootstrapping.
  --callback-port   Port to use with --start-callback. Default: 8088.
  -h, --help        Show this help text.

What it does:
  - Creates a root .venv for Ansible tooling.
  - Installs ansible-core and repo-local collections.
  - Creates callback_site/.venv and installs Flask dependencies.
  - Validates inventory syntax and Docker Compose stack configs.
EOF
}

log() {
  printf '[bootstrap] %s\n' "$1"
}

fail() {
  printf '[bootstrap] ERROR: %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "Missing required command: $1"
  fi
}

find_virtualenv_cmd() {
  if command -v virtualenv >/dev/null 2>&1; then
    command -v virtualenv
    return 0
  fi

  if [[ -x "$HOME/.local/bin/virtualenv" ]]; then
    printf '%s\n' "$HOME/.local/bin/virtualenv"
    return 0
  fi

  return 1
}

install_virtualenv_tool() {
  local virtualenv_cmd

  if virtualenv_cmd="$(find_virtualenv_cmd)"; then
    printf '%s\n' "$virtualenv_cmd"
    return 0
  fi

  require_cmd "$PYTHON_BIN"
  "$PYTHON_BIN" -m pip --version >/dev/null 2>&1 || fail "python3 pip support is required"

  log "Installing virtualenv under ~/.local/bin"
  "$PYTHON_BIN" -m pip install --user --break-system-packages virtualenv >/dev/null

  if virtualenv_cmd="$(find_virtualenv_cmd)"; then
    printf '%s\n' "$virtualenv_cmd"
    return 0
  fi

  fail "virtualenv installation completed but the executable could not be found"
}

create_venv() {
  local venv_path="$1"
  local virtualenv_cmd

  if [[ -x "$venv_path/bin/python" ]]; then
    return 0
  fi

  if [[ "$VALIDATE_ONLY" == "true" ]]; then
    fail "Missing virtual environment: $venv_path"
  fi

  require_cmd "$PYTHON_BIN"

  if "$PYTHON_BIN" -m venv "$venv_path" >/dev/null 2>&1; then
    return 0
  fi

  virtualenv_cmd="$(install_virtualenv_tool)"
  "$virtualenv_cmd" "$venv_path" >/dev/null
}

install_ansible_env() {
  create_venv "$PROJECT_VENV"

  if [[ "$VALIDATE_ONLY" != "true" ]]; then
    log "Installing Ansible dependencies into $PROJECT_VENV"
    "$PROJECT_VENV/bin/pip" install -r "$ANSIBLE_REQUIREMENTS"
    "$PROJECT_VENV/bin/ansible-galaxy" collection install -r "$COLLECTIONS_REQUIREMENTS" -p "$ROOT_DIR/collections"
  fi

  "$PROJECT_VENV/bin/ansible-playbook" --version >/dev/null
  ANSIBLE_CONFIG="$ROOT_DIR/ansible.cfg" "$PROJECT_VENV/bin/ansible-inventory" --list >/dev/null
}

install_callback_env() {
  create_venv "$CALLBACK_VENV"

  if [[ "$VALIDATE_ONLY" != "true" ]]; then
    log "Installing callback site dependencies into $CALLBACK_VENV"
    "$CALLBACK_VENV/bin/pip" install -r "$CALLBACK_DIR/requirements.txt"
  fi

  (
    cd "$CALLBACK_DIR"
    "$CALLBACK_VENV/bin/python" -m py_compile app.py
  )
}

validate_compose_stacks() {
  local stack_dir
  local compose_args

  if ! command -v docker >/dev/null 2>&1; then
    log "Skipping Docker Compose validation because docker is not installed"
    return 0
  fi

  if ! docker compose version >/dev/null 2>&1; then
    log "Skipping Docker Compose validation because docker compose is unavailable"
    return 0
  fi

  shopt -s nullglob
  for stack_dir in "$ROOT_DIR"/tower-2-compose/*; do
    [[ -d "$stack_dir" ]] || continue
    compose_args=( -f "$stack_dir/docker-compose.yml" )
    if [[ -f "$stack_dir/docker-compose.override.yml" ]]; then
      compose_args+=( -f "$stack_dir/docker-compose.override.yml" )
    fi
    log "Validating Compose stack $(basename "$stack_dir")"
    docker compose "${compose_args[@]}" config >/dev/null
  done
  shopt -u nullglob
}

start_callback_site() {
  log "Starting callback site on port $CALLBACK_PORT"
  (
    cd "$CALLBACK_DIR"
    PORT="$CALLBACK_PORT" "$CALLBACK_VENV/bin/python" app.py
  )
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --validate-only)
      VALIDATE_ONLY="true"
      shift
      ;;
    --skip-ansible)
      SKIP_ANSIBLE="true"
      shift
      ;;
    --skip-callback)
      SKIP_CALLBACK="true"
      shift
      ;;
    --start-callback)
      START_CALLBACK="true"
      shift
      ;;
    --callback-port)
      CALLBACK_PORT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1"
      ;;
  esac
done

require_cmd "$PYTHON_BIN"

if [[ "$SKIP_ANSIBLE" == "false" ]]; then
  install_ansible_env
fi

if [[ "$SKIP_CALLBACK" == "false" ]]; then
  install_callback_env
fi

validate_compose_stacks

log "Bootstrap complete"
if [[ "$SKIP_ANSIBLE" == "false" ]]; then
  log "Ansible venv: $PROJECT_VENV"
fi
if [[ "$SKIP_CALLBACK" == "false" ]]; then
  log "Callback venv: $CALLBACK_VENV"
fi

if [[ "$START_CALLBACK" == "true" ]]; then
  start_callback_site
else
  log "Run scripts/bootstrap_project.sh --start-callback to launch the callback site"
fi