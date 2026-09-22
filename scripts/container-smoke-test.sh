#!/usr/bin/env bash

set -Eeuo pipefail

readonly PROJECT_NAME="rentflow-common-smoke"
readonly INVENTORY_PORT="${INVENTORY_PORT:-8080}"
readonly PRICING_PORT="${PRICING_PORT:-8081}"
readonly RESERVATION_PORT="${RESERVATION_PORT:-8082}"
readonly INVENTORY_BASE_URL="http://localhost:${INVENTORY_PORT}"
readonly PRICING_BASE_URL="http://localhost:${PRICING_PORT}"
readonly RESERVATION_BASE_URL="http://localhost:${RESERVATION_PORT}"
readonly SERIAL_NUMBER="SMOKE-001"
readonly KAFKA_SMOKE_TOPIC="rentflow.smoke.events.v1"
readonly KAFKA_SMOKE_MESSAGE='{"eventId":"SMOKE-001","eventType":"smoke-test"}'
readonly CANCELLATION_SERIAL_NUMBER="SMOKE-CANCEL-001"
readonly CANCELLATION_SOURCE_TOPIC="rentflow.reservation.cancelled.v1"
readonly CANCELLATION_DLT_TOPIC="rentflow.inventory.reservation-cancellation.dlt.v1"
readonly CANCELLATION_GROUP="rentflow.inventory.reservation-cancellation.v1"
readonly CANCELLATION_CREATION_KEY="d880f919-2b5c-4f7e-a56d-e047e7d932a6"
readonly CANCELLATION_CREATION_REQUEST='{"customerId":"SMOKE-CUSTOMER","orderId":"SMOKE-ORDER","items":[{"serialNumber":"SMOKE-CANCEL-001","startDate":"9999-12-30","endDate":"9999-12-31"}]}'
readonly CANCELLATION_ITEM_RESERVED='{"serialNumber":"SMOKE-CANCEL-001","type":"Industrial drill","name":"Cancellation smoke drill","status":"RESERVED"}'
readonly CANCELLATION_ITEM_AVAILABLE='{"serialNumber":"SMOKE-CANCEL-001","type":"Industrial drill","name":"Cancellation smoke drill","status":"AVAILABLE"}'
readonly EXPECTED_ITEM='{"serialNumber":"SMOKE-001","type":"Industrial drill","name":"Smoke drill","status":"AVAILABLE"}'
readonly CREATE_ITEM_REQUEST='{"serialNumber":"SMOKE-001","type":"Industrial drill","name":"Smoke drill","status":"AVAILABLE"}'
readonly EXPECTED_PRICING='{"serialNumber":"SMOKE-001","price":125.50,"weekendRate":1.2500,"longRentalCondition":7,"longRentalDiscount":0.1000,"deposit":300.00}'
readonly CREATE_PRICING_REQUEST='{"serialNumber":"SMOKE-001","price":125.50,"weekendRate":1.2500,"longRentalCondition":7,"longRentalDiscount":0.1000,"deposit":300.00}'
CANCELLATION_RESERVATION_ID=""

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
    local reservation_user="${RESERVATION_DB_USER:-reservation}"
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
            WHERE namespace.nspname IN ('inventory', 'pricing', 'reservation')
            ORDER BY namespace.nspname;
        ")"
    expected_role_state="$(printf \
        'inventory:%s:true:false:false:false\npricing:%s:true:false:false:false\nreservation:%s:true:false:false:false' \
        "$inventory_user" \
        "$pricing_user" \
        "$reservation_user")"
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
            FROM (VALUES ('inventory'), ('pricing'), ('reservation')) AS service_schema(name)
            LEFT JOIN information_schema.tables AS app_table
                ON app_table.table_schema = service_schema.name
            GROUP BY service_schema.name
            ORDER BY service_schema.name;
        ")"
    [[ "$table_state" == $'inventory:0\npricing:0\nreservation:0' ]] \
        || fail "The database bootstrap created application or migration tables"
}

create_kafka_smoke_event() {
    compose exec --no-TTY rentflow-kafka \
        /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server localhost:19092 \
        --create \
        --if-not-exists \
        --topic "$KAFKA_SMOKE_TOPIC" \
        --partitions 1 \
        --replication-factor 1 >/dev/null

    printf '%s\n' "$KAFKA_SMOKE_MESSAGE" | compose exec --no-TTY rentflow-kafka \
        /opt/kafka/bin/kafka-console-producer.sh \
        --bootstrap-server localhost:19092 \
        --topic "$KAFKA_SMOKE_TOPIC"
}

assert_cancellation_topics() {
    local source_description
    local source_config
    local dlt_description
    local dlt_config

    source_description="$(compose exec --no-TTY rentflow-kafka \
        /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server localhost:19092 \
        --describe \
        --topic "$CANCELLATION_SOURCE_TOPIC")"
    source_config="$(compose exec --no-TTY rentflow-kafka \
        /opt/kafka/bin/kafka-configs.sh \
        --bootstrap-server localhost:19092 \
        --entity-type topics \
        --entity-name "$CANCELLATION_SOURCE_TOPIC" \
        --describe)"
    dlt_description="$(compose exec --no-TTY rentflow-kafka \
        /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server localhost:19092 \
        --describe \
        --topic "$CANCELLATION_DLT_TOPIC")"
    dlt_config="$(compose exec --no-TTY rentflow-kafka \
        /opt/kafka/bin/kafka-configs.sh \
        --bootstrap-server localhost:19092 \
        --entity-type topics \
        --entity-name "$CANCELLATION_DLT_TOPIC" \
        --describe)"

    [[ "$source_description" == *"PartitionCount: 3"* ]] \
        || fail "Cancellation source topic does not have three partitions"
    [[ "$source_config" == *"cleanup.policy=delete"* && "$source_config" == *"retention.ms=604800000"* ]] \
        || fail "Cancellation source topic policy is invalid"
    [[ "$dlt_description" == *"PartitionCount: 3"* ]] \
        || fail "Cancellation DLT does not have three partitions"
    [[ "$dlt_config" == *"cleanup.policy=delete"* && "$dlt_config" == *"retention.ms=2592000000"* ]] \
        || fail "Cancellation DLT policy is invalid"
}

create_cancellation_item() {
    local response

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        --request POST \
        --header 'Content-Type: application/json' \
        --data "$CANCELLATION_ITEM_AVAILABLE" \
        "${INVENTORY_BASE_URL}/api/v1/inventory")"
    [[ "$response" == "$CANCELLATION_ITEM_AVAILABLE" ]] \
        || fail "Cancellation smoke item was not created as AVAILABLE"
}

create_cancellation_reservation() {
    local response
    local remainder

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 10 \
        --request POST \
        --header 'Content-Type: application/json' \
        --header "Idempotency-Key: $CANCELLATION_CREATION_KEY" \
        --data "$CANCELLATION_CREATION_REQUEST" \
        "${RESERVATION_BASE_URL}/api/v1/reservations")"
    [[ "$response" == *'"serialNumber":"SMOKE-CANCEL-001"'* && "$response" == *'"status":"HELD"'* ]] \
        || fail "Reservation cancellation smoke setup did not create a HELD reservation"
    remainder="${response#*\"id\":\"}"
    [[ "$remainder" != "$response" ]] || fail "Reservation creation response has no ID"
    CANCELLATION_RESERVATION_ID="${remainder%%\"*}"
    [[ "$CANCELLATION_RESERVATION_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
        || fail "Reservation creation response has an invalid ID"
}

cancel_reservation() {
    local status

    status="$(curl \
        --silent \
        --output /dev/null \
        --write-out '%{http_code}' \
        --max-time 10 \
        --request POST \
        "${RESERVATION_BASE_URL}/api/v1/reservations/${CANCELLATION_RESERVATION_ID}/cancel")"
    [[ "$status" == "204" ]] || fail "Reservation cancellation endpoint returned HTTP $status"
}

assert_cancellation_outbox_count() {
    local postgres_user="${POSTGRES_USER:-rentflow_admin}"
    local postgres_db="${POSTGRES_DB:-rentflow}"
    local count

    count="$(compose exec --no-TTY rentflow-postgres \
        psql \
        --username "$postgres_user" \
        --dbname "$postgres_db" \
        --tuples-only \
        --no-align \
        --command "SELECT count(*) FROM reservation.reservation_cancellation_outbox WHERE record_key = '$CANCELLATION_SERIAL_NUMBER';")"
    [[ "$count" == "1" ]] || fail "Cancellation endpoint did not preserve exactly one logical outbox event"
}

assert_cancellation_reservation_readable() {
    local response

    response="$(curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        "${RESERVATION_BASE_URL}/api/v1/reservations/${CANCELLATION_RESERVATION_ID}")"
    [[ "$response" == *'"serialNumber":"SMOKE-CANCEL-001"'* && "$response" == *'"status":"CANCELLED"'* ]] \
        || fail "Cancelled reservation was not readable after restart"
}

wait_for_cancellation_item() {
    local expected="$1"
    local attempt=1
    local response

    while ((attempt <= 60)); do
        response="$(curl \
            --silent \
            --show-error \
            --max-time 2 \
            "${INVENTORY_BASE_URL}/api/v1/inventory/${CANCELLATION_SERIAL_NUMBER}" 2>/dev/null || true)"
        if [[ "$response" == "$expected" ]]; then
            return
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    fail "Cancellation smoke item did not reach the expected state"
}

wait_for_cancellation_group() {
    local attempt=1

    while ((attempt <= 60)); do
        if compose exec --no-TTY rentflow-kafka sh -ec \
            "/opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:19092 --describe --group '$CANCELLATION_GROUP' 2>/dev/null | awk '\$1 == \"$CANCELLATION_GROUP\" && \$2 == \"$CANCELLATION_SOURCE_TOPIC\" { found=1; lag += \$6 } END { exit !(found && lag == 0) }'"; then
            return
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    fail "Inventory cancellation consumer did not catch up"
}

cancellation_history() {
    curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        "${INVENTORY_BASE_URL}/api/v1/inventory-history?page=0&size=100&serialNumber=${CANCELLATION_SERIAL_NUMBER}&sort=timestamp&direction=asc"
}

reserve_cancellation_item_again() {
    local status

    status="$(curl \
        --silent \
        --output /dev/null \
        --write-out '%{http_code}' \
        --max-time 5 \
        --request PATCH \
        --header 'Idempotency-Key: 2c7afe55-d5aa-4d3c-ac6e-8a684a322001' \
        --header 'Content-Type: application/json' \
        --data '[{"serialNumber":"SMOKE-CANCEL-001","status":"RESERVED"}]' \
        "${INVENTORY_BASE_URL}/api/v1/inventory/status")"
    [[ "$status" == "204" ]] || fail "Cancellation smoke item could not be reserved again"
}

assert_cancellation_flow() {
    local history_after_release
    local history_before_duplicate
    local history_after_duplicate

    create_cancellation_item
    create_cancellation_reservation
    wait_for_cancellation_item "$CANCELLATION_ITEM_RESERVED"
    cancel_reservation
    wait_for_cancellation_item "$CANCELLATION_ITEM_AVAILABLE"
    wait_for_cancellation_group
    assert_cancellation_outbox_count
    history_after_release="$(cancellation_history)"
    [[ "$history_after_release" == *'"serialNumber":"SMOKE-CANCEL-001","statusFrom":"RESERVED","statusTo":"AVAILABLE"'* ]] \
        || fail "Cancellation release history is not visible through the public API"

    reserve_cancellation_item_again
    history_before_duplicate="$(cancellation_history)"
    cancel_reservation
    wait_for_cancellation_group
    assert_cancellation_outbox_count
    wait_for_cancellation_item "$CANCELLATION_ITEM_RESERVED"
    history_after_duplicate="$(cancellation_history)"
    [[ "$history_after_duplicate" == "$history_before_duplicate" ]] \
        || fail "Duplicate cancellation created additional release history"
}

assert_kafka_smoke_event_readable() {
    local response

    response="$(compose exec --no-TTY rentflow-kafka \
        /opt/kafka/bin/kafka-console-consumer.sh \
        --bootstrap-server localhost:19092 \
        --topic "$KAFKA_SMOKE_TOPIC" \
        --from-beginning \
        --max-messages 1 \
        --timeout-ms 10000 2>/dev/null)"
    [[ "$response" == "$KAFKA_SMOKE_MESSAGE" ]] \
        || fail "Kafka did not return the persisted smoke-test event"
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

assert_reservation_runtime_image() {
    local container_id
    local configured_user
    local entrypoint
    local healthcheck

    container_id="$(compose ps --quiet reservation)"
    configured_user="$(docker inspect --format '{{.Config.User}}' "$container_id")"
    entrypoint="$(docker inspect --format '{{json .Config.Entrypoint}}' "$container_id")"
    healthcheck="$(docker inspect --format '{{json .Config.Healthcheck.Test}}' "$container_id")"

    [[ "$configured_user" == "10001:10001" ]] \
        || fail "Reservation is not configured to run as UID/GID 10001"
    [[ "$entrypoint" == '["java","-jar","/opt/reservation/reservation.jar"]' ]] \
        || fail "Reservation does not use the expected runtime artifact"
    [[ "$healthcheck" == *"/readyz"* ]] \
        || fail "Reservation health does not depend on readiness"
    [[ "$healthcheck" != *"livez"* ]] \
        || fail "Reservation container health must not use liveness"

    compose exec --no-TTY reservation sh -ec '
        test "$(id -u)" = "10001"
        test "$(id -g)" = "10001"
        test -r /opt/reservation/reservation.jar
        ! command -v javac >/dev/null 2>&1
        ! command -v mvn >/dev/null 2>&1
        ! test -d /workspace
        ! test -d /root/.m2
        ! test -d /home/reservation/.m2
    ' || fail "Reservation runtime image contains build tooling or runs with the wrong identity"
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

info "Building the Inventory, Pricing, and Reservation runtime images"
compose build inventory pricing reservation

info "Starting PostgreSQL and validating all first-run bootstrap owners"
compose up --detach rentflow-postgres
wait_for_service_health rentflow-postgres 60
assert_bootstrap_state

info "Starting Kafka and verifying an explicit topic round trip"
compose up --detach rentflow-kafka
wait_for_service_health rentflow-kafka 90
create_kafka_smoke_event
assert_kafka_smoke_event_readable

info "Restarting Kafka and checking event persistence"
compose restart rentflow-kafka
wait_for_service_health rentflow-kafka 90
assert_kafka_smoke_event_readable

info "Starting Inventory, Pricing, and Reservation"
compose up --detach inventory pricing reservation
wait_for_service_health inventory 120
wait_for_service_health pricing 120
wait_for_service_health reservation 120
assert_inventory_runtime_image
assert_pricing_runtime_image
assert_reservation_runtime_image
assert_cancellation_topics

info "Verifying asynchronous reservation cancellation and durable duplicate suppression"
assert_cancellation_flow

info "Creating and retrieving an inventory item and its pricing"
create_item
assert_item_readable
create_pricing
assert_pricing_readable

info "Restarting all services and checking database-backed persistence"
compose restart inventory pricing reservation
wait_for_service_health inventory 120
wait_for_service_health pricing 120
wait_for_service_health reservation 120
assert_item_readable
assert_pricing_readable
assert_cancellation_reservation_readable

info "Stopping PostgreSQL and checking independent service liveness"
compose stop rentflow-postgres
assert_http_status 200 "${INVENTORY_BASE_URL}/livez"
assert_http_status 200 "${PRICING_BASE_URL}/livez"
assert_http_status 200 "${RESERVATION_BASE_URL}/livez"
assert_http_status 503 "${INVENTORY_BASE_URL}/readyz" 35
assert_http_status 503 "${PRICING_BASE_URL}/readyz" 35
assert_http_status 503 "${RESERVATION_BASE_URL}/readyz" 35

info "Restarting PostgreSQL and checking readiness recovery"
compose start rentflow-postgres
wait_for_service_health rentflow-postgres 60
wait_for_http_status 200 "${INVENTORY_BASE_URL}/readyz" 60
wait_for_http_status 200 "${PRICING_BASE_URL}/readyz" 60
wait_for_http_status 200 "${RESERVATION_BASE_URL}/readyz" 60
wait_for_service_health inventory 60
wait_for_service_health pricing 60
wait_for_service_health reservation 60
assert_item_readable
assert_pricing_readable
assert_cancellation_reservation_readable

info "Recreating the combined stack without deleting its volume"
compose down --remove-orphans
compose up --detach
wait_for_service_health rentflow-postgres 60
wait_for_service_health rentflow-kafka 90
wait_for_service_health inventory 120
wait_for_service_health pricing 120
wait_for_service_health reservation 120
assert_item_readable
assert_pricing_readable
assert_cancellation_reservation_readable
assert_kafka_smoke_event_readable

info "Combined container smoke verification passed"
