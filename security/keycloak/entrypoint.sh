#!/usr/bin/env bash
set -Eeuo pipefail
export KC_DB_PASSWORD="$(cat /run/secrets/keycloak_db_password)"
if [[ -f /run/secrets/keycloak_bootstrap_secret ]]; then
    export KC_BOOTSTRAP_ADMIN_CLIENT_ID=rentflow-bootstrap
    export KC_BOOTSTRAP_ADMIN_CLIENT_SECRET="$(cat /run/secrets/keycloak_bootstrap_secret)"
fi
exec /opt/keycloak/bin/kc.sh "$@"
