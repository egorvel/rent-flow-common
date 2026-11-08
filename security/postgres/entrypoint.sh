#!/usr/bin/env bash
set -Eeuo pipefail
export POSTGRES_PASSWORD="$(cat /run/secrets/keycloak_postgres_admin_password)"
export KEYCLOAK_DB_PASSWORD="$(cat /run/secrets/keycloak_db_password)"
exec /usr/local/bin/docker-entrypoint.sh "$@"
