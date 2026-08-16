#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_NAME="rentflow-common-smoke"
readonly INVENTORY_PORT="${INVENTORY_PORT:-8080}"
readonly PRICING_PORT="${PRICING_PORT:-8081}"
readonly INVENTORY_BASE_URL="http://localhost:${INVENTORY_PORT}"
readonly PRICING_BASE_URL="http://localhost:${PRICING_PORT}"
readonly SERIAL_NUMBER="SMOKE-001"
readonly EXPECTED_ITEM='{"serialNumber":"SMOKE-001","type":"Industrial drill","name":"Smoke drill","status":"AVAILABLE"}'
readonly CREATE_ITEM_REQUEST='{"serialNumber":"SMOKE-001","type":"Industrial drill","name":"Smoke drill","status":"AVAILABLE"}'
readonly EXPECTED_PRICING='{"serialNumber":"SMOKE-001","price":125.50,"weekendRate":1.2500,"longRentalCondition":7,"longRentalDiscount":0.1000,"deposit":300.00}'
readonly CREATE_PRICING_REQUEST='{"serialNumber":"SMOKE-001","price":125.50,"weekendRate":1.2500,"longRentalCondition":7,"longRentalDiscount":0.1000,"deposit":300.00}'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

cd "$PROJECT_ROOT"

compose() {
    docker compose --project-name "$PROJECT_NAME" "$@"
}

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

info() {
    printf '%s\n' "$*"
}

wait_for_service_health() {
    local service="$1"
    local max_attempts="$2"
    local attempt=1
    local container_id
    local health

    while ((attempt <= max_attempts)); do
        container_id="$(compose ps --quiet "$service")"
        if [[ -n "$container_id" ]]; then
            health="$(docker inspect \
                --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                "$container_id" 2>/dev/null || true)"
            if [[ "$health" == "healthy" ]]; then
                return
            fi
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    compose ps >&2 || true
    fail "$service did not become healthy within ${max_attempts}s"
}

wait_for_http_status() {
    local expected_status="$1"
    local url="$2"
    local max_attempts="$3"
    local attempt=1
    local actual_status

    while ((attempt <= max_attempts)); do
        actual_status="$(curl \
            --silent \
            --output /dev/null \
            --write-out '%{http_code}' \
            --max-time 2 \
            "$url" 2>/dev/null || true)"
        if [[ "$actual_status" == "$expected_status" ]]; then
            return
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    fail "$url did not return HTTP $expected_status within ${max_attempts}s"
}

assert_http_status() {
    local expected_status="$1"
    local url="$2"
    local max_time="${3:-2}"
    local actual_status

    actual_status="$(curl \
        --silent \
        --output /dev/null \
        --write-out '%{http_code}' \
        --max-time "$max_time" \
        "$url" 2>/dev/null || true)"
    [[ "$actual_status" == "$expected_status" ]] \
        || fail "$url returned HTTP $actual_status instead of $expected_status"
}

assert_bootstrap_state() {
    local postgres_user="${POSTGRES_USER:-rentflow_admin}"
    local postgres_db="${POSTGRES_DB:-rentflow}"
    local inventory_user="${INVENTORY_DB_USER:-inventory}"
    local pricing_user="${PRICING_DB_USER:-pricing}"
    local expected_role_state
    local role_state
    local table_state

    role_state="$(compose exec --no-TTY rentflow-postgres \
        psql \
        --username "$postgres_user" \
        --dbname "$postgres_db" \
        --tuples-only \
        --no-align \
        --command "
            SELECT namespace.nspname
                   || ':' || role.rolname
                   || ':' || role.rolcanlogin
                   || ':' || role.rolsuper
                   || ':' || role.rolcreatedb
                   || ':' || role.rolcreaterole
            FROM pg_catalog.pg_namespace AS namespace
            JOIN pg_catalog.pg_roles AS role ON role.oid = namespace.nspowner
            WHERE namespace.nspname IN ('inventory', 'pricing')
            ORDER BY namespace.nspname;
        ")"
    expected_role_state="$(printf \
        'inventory:%s:true:false:false:false\npricing:%s:true:false:false:false' \
        "$inventory_user" \
        "$pricing_user")"
    [[ "$role_state" == "$expected_role_state" ]] \
        || fail "Service role/schema bootstrap state is invalid"

    table_state="$(compose exec --no-TTY rentflow-postgres \
        psql \
        --username "$postgres_user" \
        --dbname "$postgres_db" \
        --tuples-only \
        --no-align \
        --command "
            SELECT service_schema.name || ':' || count(app_table.table_name)
            FROM (VALUES ('inventory'), ('pricing')) AS service_schema(name)
            LEFT JOIN information_schema.tables AS app_table
                ON app_table.table_schema = service_schema.name
            GROUP BY service_schema.name
            ORDER BY service_schema.name;
        ")"
    [[ "$table_state" == $'inventory:0\npricing:0' ]] \
        || fail "The database bootstrap created application or migration tables"
}

assert_inventory_runtime_image() {
    local container_id
    local configured_user
    local entrypoint
    local healthcheck

    container_id="$(compose ps --quiet inventory)"
    configured_user="$(docker inspect --format '{{.Config.User}}' "$container_id")"
    entrypoint="$(docker inspect --format '{{json .Config.Entrypoint}}' "$container_id")"
    healthcheck="$(docker inspect --format '{{json .Config.Healthcheck.Test}}' "$container_id")"

    [[ "$configured_user" == "10001:10001" ]] \
        || fail "Inventory is not configured to run as UID/GID 10001"
    [[ "$entrypoint" == '["java","-jar","/opt/inventory/inventory.jar"]' ]] \
        || fail "Inventory does not use the expected runtime artifact"
    [[ "$healthcheck" == *'"http://localhost:8080/readyz"'* ]] \
        || fail "Inventory health does not depend on readiness"
    [[ "$healthcheck" != *"livez"* ]] \
        || fail "Inventory container health must not use liveness"

    compose exec --no-TTY inventory sh -ec '
        test "$(id -u)" = "10001"
        test "$(id -g)" = "10001"
        test -r /opt/inventory/inventory.jar
        ! command -v javac >/dev/null 2>&1
        ! command -v mvn >/dev/null 2>&1
        ! test -d /workspace
        ! test -d /root/.m2
        ! test -d /home/inventory/.m2
    ' || fail "Inventory runtime image contains build tooling or runs with the wrong identity"
}

assert_pricing_runtime_image() {
    local container_id
    local configured_user
    local entrypoint
    local healthcheck

    container_id="$(compose ps --quiet pricing)"
    configured_user="$(docker inspect --format '{{.Config.User}}' "$container_id")"
    entrypoint="$(docker inspect --format '{{json .Config.Entrypoint}}' "$container_id")"
    healthcheck="$(docker inspect --format '{{json .Config.Healthcheck.Test}}' "$container_id")"

    [[ "$configured_user" == "10001:10001" ]] \
        || fail "Pricing is not configured to run as UID/GID 10001"
    [[ "$entrypoint" == '["java","-jar","/opt/pricing/pricing.jar"]' ]] \
        || fail "Pricing does not use the expected runtime artifact"
    [[ "$healthcheck" == *'"http://localhost:8080/readyz"'* ]] \
        || fail "Pricing health does not depend on readiness"
    [[ "$healthcheck" != *"livez"* ]] \
        || fail "Pricing container health must not use liveness"

    compose exec --no-TTY pricing sh -ec '
        test "$(id -u)" = "10001"
        test "$(id -g)" = "10001"
        test -r /opt/pricing/pricing.jar
        ! command -v javac >/dev/null 2>&1
        ! command -v mvn >/dev/null 2>&1
        ! test -d /workspace
        ! test -d /root/.m2
        ! test -d /home/pricing/.m2
    ' || fail "Pricing runtime image contains build tooling or runs with the wrong identity"
}

create_item() {
    local response

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        --request POST \
        --header 'Content-Type: application/json' \
        --data "$CREATE_ITEM_REQUEST" \
        "${INVENTORY_BASE_URL}/api/v1/inventory")"
    [[ "$response" == "$EXPECTED_ITEM" ]] \
        || fail "Create response did not match the inventory contract"
}

assert_item_readable() {
    local response

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        "${INVENTORY_BASE_URL}/api/v1/inventory/${SERIAL_NUMBER}")"
    [[ "$response" == "$EXPECTED_ITEM" ]] \
        || fail "Persisted inventory item could not be retrieved"
}

create_pricing() {
    local response

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        --request POST \
        --header 'Content-Type: application/json' \
        --data "$CREATE_PRICING_REQUEST" \
        "${PRICING_BASE_URL}/api/v1/pricing")"
    [[ "$response" == "$EXPECTED_PRICING" ]] \
        || fail "Create response did not match the pricing contract"
}

assert_pricing_readable() {
    local response

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        "${PRICING_BASE_URL}/api/v1/pricing/${SERIAL_NUMBER}")"
    [[ "$response" == "$EXPECTED_PRICING" ]] \
        || fail "Persisted pricing could not be retrieved"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || fail "docker is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"

cleanup

info "Validating Compose configuration"
compose config --quiet

info "Building the Inventory and Pricing runtime images"
compose build inventory pricing

info "Starting PostgreSQL and validating both first-run bootstrap owners"
compose up --detach rentflow-postgres
wait_for_service_health rentflow-postgres 60
assert_bootstrap_state

info "Starting Inventory and Pricing"
compose up --detach inventory pricing
wait_for_service_health inventory 120
wait_for_service_health pricing 120
assert_inventory_runtime_image
assert_pricing_runtime_image

info "Creating and retrieving an inventory item and its pricing"
create_item
assert_item_readable
create_pricing
assert_pricing_readable

info "Restarting both services and checking database-backed persistence"
compose restart inventory pricing
wait_for_service_health inventory 120
wait_for_service_health pricing 120
assert_item_readable
assert_pricing_readable

info "Stopping PostgreSQL and checking independent service liveness"
compose stop rentflow-postgres
assert_http_status 200 "${INVENTORY_BASE_URL}/livez"
assert_http_status 200 "${PRICING_BASE_URL}/livez"
assert_http_status 503 "${INVENTORY_BASE_URL}/readyz" 35
assert_http_status 503 "${PRICING_BASE_URL}/readyz" 35

info "Restarting PostgreSQL and checking readiness recovery"
compose start rentflow-postgres
wait_for_service_health rentflow-postgres 60
wait_for_http_status 200 "${INVENTORY_BASE_URL}/readyz" 60
wait_for_http_status 200 "${PRICING_BASE_URL}/readyz" 60
wait_for_service_health inventory 60
wait_for_service_health pricing 60
assert_item_readable
assert_pricing_readable

info "Recreating the combined stack without deleting its volume"
compose down --remove-orphans
compose up --detach
wait_for_service_health rentflow-postgres 60
wait_for_service_health inventory 120
wait_for_service_health pricing 120
assert_item_readable
assert_pricing_readable

info "Combined container smoke verification passed"
