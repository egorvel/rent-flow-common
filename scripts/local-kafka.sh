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
  start          Start Kafka and wait until it is healthy.
  stop           Stop Kafka while preserving its data volume.
  status         Show the Kafka container status.
  logs           Follow the Kafka container logs.
  topics         List topics in the local Kafka cluster.
  reset [--force]
                 Delete the local Kafka volume and start a fresh broker.
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

    info "This will stop Kafka and permanently delete all local topics and messages."
    printf 'Continue? [y/N] '
    read -r response
    [[ "$response" == "y" || "$response" == "Y" || "$response" == "yes" || "$response" == "YES" ]] \
        || {
            info "Reset cancelled"
            exit 0
        }
}

kafka_data_volume() {
    local container_id
    local volume_name

    compose create rentflow-kafka >/dev/null
    container_id="$(compose ps --all --quiet rentflow-kafka)"
    [[ -n "$container_id" ]] || fail "could not find the Kafka container"

    volume_name="$(docker inspect \
        --format '{{range .Mounts}}{{if eq .Destination "/var/lib/kafka/data"}}{{.Name}}{{end}}{{end}}' \
        "$container_id")"
    [[ -n "$volume_name" ]] || fail "could not find the Kafka data volume"
    printf '%s\n' "$volume_name"
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
        info "Starting Kafka"
        compose up --detach --wait rentflow-kafka
        ;;
    stop)
        require_no_arguments "$COMMAND" "$@"
        info "Stopping Kafka and preserving its topics and messages"
        compose stop rentflow-kafka
        ;;
    status)
        require_no_arguments "$COMMAND" "$@"
        compose ps rentflow-kafka
        ;;
    logs)
        require_no_arguments "$COMMAND" "$@"
        compose logs --follow rentflow-kafka
        ;;
    topics)
        require_no_arguments "$COMMAND" "$@"
        compose exec --no-TTY rentflow-kafka \
            /opt/kafka/bin/kafka-topics.sh \
            --bootstrap-server localhost:19092 \
            --list
        ;;
    reset)
        volume_name=""
        (($# <= 1)) || fail "reset accepts only the optional --force flag"
        confirm_reset "${1:-}"
        volume_name="$(kafka_data_volume)"
        info "Deleting the Kafka container and data volume"
        compose rm --stop --force rentflow-kafka >/dev/null
        docker volume rm "$volume_name" >/dev/null
        info "Starting fresh Kafka"
        compose up --detach --wait rentflow-kafka
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
