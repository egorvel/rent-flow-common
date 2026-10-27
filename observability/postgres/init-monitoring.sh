#!/usr/bin/env bash
set -Eeuo pipefail

# Also safe to run explicitly against an existing local database volume.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 --set=monitor_password="$POSTGRES_EXPORTER_PASSWORD" <<'SQL'
SELECT 'CREATE ROLE rentflow_monitor LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rentflow_monitor')\gexec
ALTER ROLE rentflow_monitor PASSWORD :'monitor_password';
GRANT pg_monitor TO rentflow_monitor;
SQL
