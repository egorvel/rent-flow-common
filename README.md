# Rent Flow Common

Local PostgreSQL, Kafka, Redis, Keycloak and optional observability for RentFlow.
Requires Docker Engine, Docker Compose and Python 3, with the Inventory, Pricing and
Reservation repositories cloned alongside Common. Run commands from this directory.

Merge settings from [.env.security.example](.env.security.example) and
[.env.observability.example](.env.observability.example) into `.env` as needed.
Shell variables override `.env`; IDE processes need their own environment settings.

## Local Keycloak

Add this hosts-file entry:

```text
127.0.0.1 auth.rentflow.test
```

Provision once before starting applications:

```bash
./scripts/local-keycloak.sh init
./scripts/local-keycloak.sh bootstrap
```

Optional development users (`customer-a`, `customer-b`, `staff`) and browser verification:

```bash
./scripts/local-keycloak.sh fixtures apply
./scripts/local-keycloak.sh verify-browser
```

The issuer is `http://auth.rentflow.test:8180/realms/rentflow-local`.
Use `rentflow-operator` in the
[admin console](http://auth.rentflow.test:8180/admin/rentflow-local/console/);
its password is in `.secrets/security/rentflow-operator-password`.
Fixture passwords and caller credentials are in the same ignored directory.

For an existing installation after a policy update:

```bash
./scripts/local-keycloak.sh policy plan
./scripts/local-keycloak.sh policy apply
./scripts/local-keycloak.sh recipients apply
```

`policy plan` exits with `2` when changes exist. Startup reports drift without applying it.
Bootstrap prepares private caller-secret copies automatically; `recipients apply` refreshes them.
Recreate Pricing and Reservation after refreshing their mounted files.

See the [security walkthrough](SECURITY_WALKTHROUGH.md) for credentials, rotation, recovery
and verification, and the [security specification](SECURITY_SPEC.md) for the policy contract.
Before upgrading an existing Reservation database, follow its
[migration and cutover contract](../rent-flow-reservation/.specs/security/design.md#52-consolidated-migration-and-scoped-maintenance):
V6 clears development reservations, replay records and outbox rows.

## Local development infrastructure

After Keycloak provisioning, start dependencies for IDE or shell runs:

```bash
./scripts/local-dev.sh start
```

Use `./scripts/local-dev.sh status`, `./scripts/local-dev.sh logs`, or
`./scripts/local-dev.sh stop` for routine management. Startup waits for health checks;
stopping preserves data. Applications start separately.

| Dependency | Host address | Compose address |
| --- | --- | --- |
| PostgreSQL | `localhost:5432/rentflow` | `rentflow-postgres:5432/rentflow` |
| Kafka | `localhost:9092` | `rentflow-kafka:19092` |
| Redis | `localhost:6379` | `rentflow-redis:6379` |
| Keycloak | `auth.rentflow.test:8180` | `auth.rentflow.test:8180` |

### Run applications in your IDE

Use the `local` profile and `SPRING_DATASOURCE_URL=jdbc:postgresql://localhost:5432/rentflow`.
Set each application's datasource username/password and server port:

| Service / run instructions | Port | Database username / password |
| --- | --- | --- |
| [Inventory](../rent-flow-inventory/README.md#run-locally) | `8080` | `inventory` / `inventory-local` |
| [Pricing](../rent-flow-pricing/README.md#run-locally) | `8081` | `pricing` / `pricing-local` |
| [Reservation](../rent-flow-reservation/README.md#run-java-locally) | `8082` | `reservation` / `reservation-local` |

Pricing and Reservation use `INVENTORY_BASE_URL=http://localhost:8080`.
Their local profiles import `.secrets/ide/` relative to the service repository. Create that
directory with mode `0700` and symlink `spring.security.oauth2.client.registration.inventory.client-secret`
to Common's `.secrets/security/pricing-service-client-secret` or
`.secrets/security/reservation-service-client-secret`, respectively.

### Run applications in Compose

With infrastructure running:

```bash
docker compose up --build --detach --wait inventory pricing reservation
```

Swagger is at `/swagger-ui.html` on each application's port. Business APIs require bearer tokens;
use Swagger's Authorize button to log in.

### Manage individual dependencies

| Script | Operations |
| --- | --- |
| `./scripts/local-postgres.sh` | `start`, `stop`, `status`, `logs`, `reset`, `help` |
| `./scripts/local-kafka.sh` | `start`, `stop`, `status`, `logs`, `topics`, `reset`, `help` |
| `./scripts/local-keycloak.sh` | See `help` for identity lifecycle and policy commands |

PostgreSQL and Kafka `reset` commands delete their respective volumes and ask for confirmation.
Keycloak keeps its own database volume. `docker compose --profile observability down` preserves
all volumes; adding `--volumes` also deletes identity and telemetry data.

## Shared observability

Start applications and collectors after Keycloak provisioning:

```bash
COMPOSE_PROFILES=observability TRACING_EXPORT_ENABLED=true docker compose up --detach --build
```

Open [Grafana](http://localhost:3000), default login `admin` / `rentflow-local`, and the
**RentFlow** dashboard folder. Dashboards, datasources and alerts are provisioned from
[observability/grafana](observability/grafana/). `GRAFANA_PORT` and `OTLP_HTTP_PORT` override
the host ports `3000` and `4318`.

For an existing application PostgreSQL volume, initialize the monitoring login once:

```bash
docker compose exec --no-TTY rentflow-postgres bash /docker-entrypoint-initdb.d/40-init-monitoring.sh
```

For IDE trace export, set `TRACING_EXPORT_ENABLED=true` and
`TRACING_OTLP_ENDPOINT=http://localhost:4318/v1/traces`. Host-process metrics and logs need
separate collection configuration. Keep trace export disabled when collectors are stopped.

The [optional demo](OBSERVABILITY_DEMO.md) does not yet acquire bearer tokens.
Use the combined smoke test for authenticated workload verification.

## Verification

Smoke tests require Linux host networking, Docker Compose, Python 3, `curl` and `jq`.

| Command | Scope |
| --- | --- |
| `./scripts/container-smoke-test.sh` | Full secured stack, cancellation/expiry, cache, observability, outages and persistence |
| `./scripts/security-smoke-test.sh` | Keycloak policy, browser login and machine tokens |
| `./scripts/security-smoke-test.sh --inventory` | Identity checks plus Inventory interoperability |
| `./scripts/security-smoke-test.sh --pricing` | Inventory checks plus Pricing interoperability |
| `python3 scripts/observability-smoke-test.py --ready-only` | Readiness of a running observability stack |

Smoke suites use disposable projects and clean up their resources. Keep their ports free:
combined application defaults are `8080`–`8082`, scoped Inventory/Pricing defaults are `28080`/`28081`,
and Keycloak/callback defaults are `18180`/`18765`. Override combined API ports with
`INVENTORY_PORT`, `PRICING_PORT`, `RESERVATION_PORT`; identity ports use
`KEYCLOAK_SMOKE_PORT` and `KEYCLOAK_SMOKE_PKCE_PORT`. See
[the security guide](SECURITY_WALKTHROUGH.md#acceptance-checks) for detailed checks.
