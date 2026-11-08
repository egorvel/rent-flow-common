#!/usr/bin/env bash
set -Eeuo pipefail
psql --username "$POSTGRES_USER" --dbname postgres --set=ON_ERROR_STOP=1 \
    --set=runtime_password="$KEYCLOAK_DB_PASSWORD" <<'SQL'
CREATE ROLE rentflow_keycloak LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
SELECT format('ALTER ROLE rentflow_keycloak PASSWORD %L', :'runtime_password') \gexec
CREATE DATABASE rentflow_keycloak OWNER rentflow_keycloak;
REVOKE ALL ON DATABASE rentflow_keycloak FROM PUBLIC;
GRANT CONNECT ON DATABASE rentflow_keycloak TO rentflow_keycloak;
SQL
