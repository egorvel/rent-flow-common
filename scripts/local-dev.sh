#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly COMPOSE_FILE="${PROJECT_ROOT}/compose.yaml"
readonly SERVICES=(rentflow-postgres rentflow-kafka rentflow-redis rentflow-keycloak rentflow-keycloak-postgres)

compose() {
    docker compose \
        --project-directory "$PROJECT_ROOT" \
        --file "$COMPOSE_FILE" \
        "$@"
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: ${0} <command>

Commands:
  start          Start PostgreSQL, Kafka, Redis and Keycloak; wait until healthy.
  stop           Stop local infrastructure while preserving database and Kafka volumes.
  status         Show local infrastructure container status.
  logs           Follow the last 100 log lines per infrastructure container.
  help           Show this help.

Before the first start, provision Keycloak explicitly:
  ${SCRIPT_DIR}/local-keycloak.sh init
  ${SCRIPT_DIR}/local-keycloak.sh bootstrap

Run Inventory, Pricing and Reservation separately in your IDE or shell.
EOF
}

readonly COMMAND="${1:-help}"
if (($# > 0)); then
    shift
fi
(($# == 0)) || fail "${COMMAND} does not accept arguments"

case "$COMMAND" in
    help | --help | -h)
        usage
        exit 0
        ;;
    start | stop | status | logs)
        ;;
    *)
        usage >&2
        fail "unknown command: ${COMMAND}"
        ;;
esac

command -v docker >/dev/null 2>&1 || fail "docker is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose is required"

case "$COMMAND" in
    start)
        command -v python3 >/dev/null 2>&1 || fail "python3 is required for Keycloak management"
        # Preserve Keycloak's explicit bootstrap and policy-drift checks. Check identity
        # first so incomplete setup does not start the other infrastructure services.
        "${SCRIPT_DIR}/local-keycloak.sh" start
        printf '%s\n' "Starting PostgreSQL, Kafka and Redis"
        compose up --detach --wait rentflow-postgres rentflow-kafka rentflow-redis
        printf '%s\n' "Local development infrastructure is ready"
        ;;
    stop)
        printf '%s\n' "Stopping local infrastructure and preserving database and Kafka volumes"
        compose stop "${SERVICES[@]}"
        ;;
    status)
        compose ps --all "${SERVICES[@]}"
        ;;
    logs)
        compose logs --follow --tail 100 "${SERVICES[@]}"
        ;;
esac
