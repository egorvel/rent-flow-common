#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/.."
if [[ "${1:-}" == --combined && $# == 1 ]]; then
    [[ "${COMPOSE_PROJECT_NAME:-}" == rentflow-common-smoke && -n "${SECURITY_STATE_DIR:-}" ]] \
        || { printf 'ERROR: combined smoke requires its isolated project/state\n' >&2; exit 1; }
    exec python3 scripts/security/smoke.py
fi
[[ $# == 0 ]] || { printf 'Usage: %s [--combined]\n' "$0" >&2; exit 1; }
export COMPOSE_PROJECT_NAME=rentflow-security-smoke
export KEYCLOAK_REALM=rentflow-smoke
export KEYCLOAK_HTTP_PORT="${KEYCLOAK_SMOKE_PORT:-18180}"
export KEYCLOAK_PKCE_PORT="${KEYCLOAK_SMOKE_PKCE_PORT:-18765}"
export SECURITY_STATE_DIR
SECURITY_STATE_DIR="$(mktemp -d /tmp/rentflow-security-smoke.XXXXXXXX)"
cleanup() {
    docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    python3 -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$SECURITY_STATE_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 scripts/security/smoke.py
