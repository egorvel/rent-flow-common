#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly COMPOSE_FILE="${PROJECT_ROOT}/compose.yaml"

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

info() {
    printf '%s\n' "$*"
}

usage() {
    cat <<EOF
Usage: ${0} <command>

Commands:
  start          Start PostgreSQL and wait until it is healthy.
  stop           Stop PostgreSQL while preserving its data volume.
  status         Show the PostgreSQL container status.
  logs           Follow the PostgreSQL container logs.
  reset [--force]
                 Delete the local database volume and start a fresh PostgreSQL.
  help           Show this help.
EOF
}

require_no_arguments() {
    local command="$1"
    shift
    (($# == 0)) || fail "${command} does not accept arguments"
}

confirm_reset() {
    local option="${1:-}"
    local response

    if [[ "$option" == "--force" ]]; then
        return
    fi
    [[ -z "$option" ]] || fail "reset accepts only the optional --force flag"
    [[ -t 0 ]] || fail "reset requires an interactive terminal; use reset --force to confirm"

    info "This will stop the Compose stack and permanently delete its PostgreSQL data volume."
    printf 'Continue? [y/N] '
    read -r response
    [[ "$response" == "y" || "$response" == "Y" || "$response" == "yes" || "$response" == "YES" ]] \
        || {
            info "Reset cancelled"
            exit 0
        }
}

command -v docker >/dev/null 2>&1 || fail "docker is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose is required"

readonly COMMAND="${1:-help}"
if (($# > 0)); then
    shift
fi

case "$COMMAND" in
    start)
        require_no_arguments "$COMMAND" "$@"
        info "Starting PostgreSQL"
        compose up --detach --wait rentflow-postgres
        ;;
    stop)
        require_no_arguments "$COMMAND" "$@"
        info "Stopping PostgreSQL and preserving its data volume"
        compose stop rentflow-postgres
        ;;
    status)
        require_no_arguments "$COMMAND" "$@"
        compose ps rentflow-postgres
        ;;
    logs)
        require_no_arguments "$COMMAND" "$@"
        compose logs --follow rentflow-postgres
        ;;
    reset)
        (($# <= 1)) || fail "reset accepts only the optional --force flag"
        confirm_reset "${1:-}"
        info "Deleting the Compose stack and PostgreSQL data volume"
        compose down --volumes --remove-orphans
        info "Starting fresh PostgreSQL"
        compose up --detach --wait rentflow-postgres
        ;;
    help | --help | -h)
        require_no_arguments "$COMMAND" "$@"
        usage
        ;;
    *)
        usage >&2
        fail "unknown command: ${COMMAND}"
        ;;
esac
