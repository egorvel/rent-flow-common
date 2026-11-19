# Rent Flow Common

This repository owns the local infrastructure topology shared by the Rent Flow services. Application-level
Kafka behavior remains in the service that publishes or consumes an event.

## Local development infrastructure

Start the shared infrastructure for applications running in your IDE or shell:

```bash
./scripts/local-dev.sh start
```

This starts PostgreSQL, Kafka, Redis (Pricing's cache), and Keycloak with its separate PostgreSQL,
and waits for health checks. Inventory, Pricing, Reservation and the optional observability stack
are started separately. Docker Engine, Docker Compose and Python 3 are required; keep the three
service repositories alongside Common so PostgreSQL can mount their schema initialization scripts.

Before the first start, run `./scripts/local-keycloak.sh init` and
`./scripts/local-keycloak.sh bootstrap`. For browser login, add `127.0.0.1 auth.rentflow.test`
to your hosts file. Optional test users can be created with `./scripts/local-keycloak.sh fixtures apply`.
See [Local Keycloak](#local-keycloak) for credentials and setup details. Routine startup uses Keycloak's
existing policy checks; it reports drift so you can review it with `local-keycloak.sh policy plan`
and reconcile it explicitly with `local-keycloak.sh policy apply`.

```bash
./scripts/local-dev.sh status
./scripts/local-dev.sh logs    # Ctrl+C stops following logs
./scripts/local-dev.sh stop    # Keeps database and Kafka volumes
```

The script works from any working directory and reads Common's Compose `.env` settings and shell
overrides. Default host endpoints are PostgreSQL `localhost:5432`, Kafka `localhost:9092`, Redis
`localhost:6379`, and Keycloak `http://auth.rentflow.test:8180`. Match any overridden ports in your
IDE run configurations; Compose's `.env` is not automatically exported to Java processes.

When launching the applications locally:

- Use `SPRING_DATASOURCE_URL=jdbc:postgresql://localhost:5432/rentflow` and each application's own
  database login: `inventory` / `inventory-local`, `pricing` / `pricing-local`, or
  `reservation` / `reservation-local` (local defaults).
- Set `SERVER_PORT` to `8080` for Inventory, `8081` for Pricing, and `8082` for Reservation.
- Set `INVENTORY_BASE_URL=http://localhost:8080` for Pricing and Reservation. Inventory must also
  be running for their cross-service operations.
- Use `SPRING_PROFILES_ACTIVE=local` for all three applications. Inventory and Reservation include
  local Kafka topic provisioning. Their default `KAFKA_BOOTSTRAP_SERVERS` is `localhost:9092`; Pricing's Redis defaults
  are `localhost:6379`.
- Configure Inventory's `SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_ISSUER_URI` as
  `http://auth.rentflow.test:8180/realms/rentflow-local` and
  `SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_JWK_SET_URI` as that issuer plus
  `/protocol/openid-connect/certs`. The hostname must resolve on the host for these requests too.

Inventory now requires bearer tokens for business requests. CUSTOMER can read/list; STAFF can
also manage items/statuses and inspect history. Apply the updated realm policy explicitly with
`./scripts/local-keycloak.sh policy plan` and `./scripts/local-keycloak.sh policy apply`; these
preserve users, subjects and credentials. Pricing now authenticates its Inventory lookups in
Phase 3. Reservation's Inventory-dependent workflows remain blocked until Phase 4.

Pricing's local profile supplies the canonical development issuer/JWKS/token URLs. Override the
standard Spring properties if you use a different issuer. Its outbound `pricing-service` secret
must be delivered as a config-tree property, never an environment value. For an IDE run, create
a private `.secrets/ide/` directory in the Pricing repository and a symlink named
`spring.security.oauth2.client.registration.inventory.client-secret` to Common's protected
`.secrets/security/pricing-service-client-secret`. Use Pricing's repository as the working
directory; its local profile imports that directory automatically. The source file remains
host-owned mode 0600. For a custom config-tree directory use `SPRING_CONFIG_IMPORT` as documented
in [Pricing's setup guide](../rent-flow-pricing/README.md#security).

Grafana, Prometheus, Loki, Tempo and Alloy are optional for ordinary development and debugger use.
See [Shared observability](#shared-observability) when you want collected metrics, logs or traces.

## Local Keycloak

Keycloak `26.8.1` is pinned by digest and uses its own PostgreSQL container, database/login and volume.
Application PostgreSQL resets preserve identity data. HTTP/development mode is for local use only.

For browser access, add this line to your host's hosts file:

```text
127.0.0.1 auth.rentflow.test
```

From Common's root, initialize the private files and perform explicit first-time provisioning:

```bash
./scripts/local-keycloak.sh init
./scripts/local-keycloak.sh bootstrap
./scripts/local-keycloak.sh fixtures apply
./scripts/local-keycloak.sh verify-browser
```

Fixtures are optional for normal use; `verify-browser` needs the two customers and staff fixture.
Open the [RentFlow administration console](http://auth.rentflow.test:8180/admin/rentflow-local/console/)
as `rentflow-operator`. Its generated password is in `.secrets/security/rentflow-operator-password`;
open this private file locally without copying credentials into logs or Git. Fixture passwords and
separate caller credentials are in the same ignored directory, with owner-only permissions.

The issuer is `http://auth.rentflow.test:8180/realms/rentflow-local` for host and container clients.
Optional non-secret port/realm settings are in `.env.security.example`; merge them into an existing
`.env` rather than replacing it. Run `init`/`bootstrap` before the first unrestricted `docker compose up`.
The wrapper reads Compose's `.env` settings. `SECURITY_STATE_DIR` selects a separate private state
directory; initialized settings are locked to that project/realm/ports to avoid accidental retargeting.

For routine use:

```bash
./scripts/local-keycloak.sh start
./scripts/local-keycloak.sh status
./scripts/local-keycloak.sh policy plan
./scripts/local-keycloak.sh policy apply
./scripts/local-keycloak.sh stop
```

`start` detects drift without applying it; `policy plan` returns exit code `2` when changes exist.
`policy apply` updates owned settings to match the checked-in policy and reads them back.
Role assignments, scope mappings, composites and defaults are declared in
`security/keycloak/rentflow-realm.template.json`. Its `_rentflow` block declares the user-profile
setting, local operator/optional fixtures and credential-file references; the renderer excludes
that tooling block from Keycloak's startup import. Python applies these declarations generically.
There are no migration files or policy versions. Run `verify` or `verify-browser` explicitly for token/login checks.
Neither restart nor policy apply resets users, passwords, keys or client secrets. There is no reset command.
For rotation, recovery and a claims/PKCE walkthrough, see [the security guide](SECURITY_WALKTHROUGH.md).

Run the isolated identity checks with `./scripts/security-smoke-test.sh`. To include the secured
Inventory application and real-provider permission/replay checks, run:

```bash
./scripts/security-smoke-test.sh --inventory
./scripts/security-smoke-test.sh --pricing
```

`--inventory` builds Inventory, injects its issuer/JWKS through a temporary test override,
and tests browser CUSTOMER/STAFF tokens and both machine clients against the actual API. Inventory
uses host port `28080`, configurable with `INVENTORY_SECURITY_SMOKE_PORT`.
Application PostgreSQL/Kafka use isolated host ports `25432`/`29092`, configurable with
`INVENTORY_SECURITY_POSTGRES_PORT`/`INVENTORY_SECURITY_KAFKA_PORT`. All checks provision
their own `rentflow-smoke` realm, keys, temporary secret files and volumes, and remove only their
own test resources. Browser/JWT dependencies
are pinned inside the test image. Smoke defaults to identity port `18180` and callback port `18765`;
set `KEYCLOAK_SMOKE_PORT` and `KEYCLOAK_SMOKE_PKCE_PORT` to change them. Automated checks use their own
hostname mapping and do not edit `/etc/hosts`. The browser runner currently supports Linux host networking.

`--pricing` additionally builds the actual Pricing image and starts Redis. It tests both approved
Pricing browser clients, customer reads/write denial, staff CRUD, cache authorization and real
Pricing-to-Inventory machine tokens, reuse/renewal and safe failure/outage behavior. Pricing uses
port `28081` (`PRICING_SECURITY_SMOKE_PORT`) and Redis `26379` (`PRICING_SECURITY_REDIS_PORT`).
Test-only helpers bind loopback at Pricing's port + 1 and the PKCE callback port + 1. A transparent
observer forwards actual Inventory requests and records only token digests and contract booleans
in memory; no bearer values are recorded. Only the private test runner can request scoped restarts,
provider stops/starts and database/cache comparisons through its temporary control capability.

The full `./scripts/container-smoke-test.sh` remains a separate acceptance gate. Reservation
workflows remain pending Phase 4; neither scoped suite claims
combined acceptance. Base Compose issuer/caller wiring is deferred until that full gate can pass.
For a local Inventory container before then, first start infrastructure and apply the realm policy,
then supply its required settings through the existing Compose run environment mechanism:

```bash
docker compose run --rm --service-ports --use-aliases \
  -e SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_ISSUER_URI=http://auth.rentflow.test:8180/realms/rentflow-local \
  -e SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_JWK_SET_URI=http://auth.rentflow.test:8180/realms/rentflow-local/protocol/openid-connect/certs \
  inventory
```

Use your configured issuer/ports if overridden. Inventory needs no client secret or additional
public-origin setting. Its local Swagger is available at `/swagger-ui.html`; API resource metadata
GETs under `/.well-known/oauth-protected-resource` are public and derive URLs from the request.

For a local Pricing container, start its infrastructure and Inventory first. Pricing's runtime
UID is `10001`; a host-owned mode-0600 source secret alone is unreadable to that UID. Prepare a
private recipient copy and grant read access on that copy (requires `setfacl`):

```bash
install -m 600 .secrets/security/pricing-service-client-secret .secrets/security/runtime/pricing-recipient-secret
setfacl --modify user:10001:r .secrets/security/runtime/pricing-recipient-secret
docker compose run --rm --no-deps --service-ports \
  -e SPRING_PROFILES_ACTIVE=local \
  -e SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_ISSUER_URI=http://auth.rentflow.test:8180/realms/rentflow-local \
  -e SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_JWK_SET_URI=http://auth.rentflow.test:8180/realms/rentflow-local/protocol/openid-connect/certs \
  -e SPRING_SECURITY_OAUTH2_CLIENT_PROVIDER_KEYCLOAK_TOKEN_URI=http://auth.rentflow.test:8180/realms/rentflow-local/protocol/openid-connect/token \
  --volume "$PWD/.secrets/security/runtime/pricing-recipient-secret:/run/secrets/spring.security.oauth2.client.registration.inventory.client-secret:ro" \
  pricing
```

Keep the recipient directory private. Do not add an ACL to Common's original secret: its lifecycle
tool checks owner-only file modes. Repeat delivery and restart only Pricing after explicit client
rotation; the service does not hot-reload credentials. The automated suite uses an isolated
UID-owned mode-0400 recipient copy instead, so it adds no `setfacl` host prerequisite.

## Local PostgreSQL startup

### Start PostgreSQL and wait until healthy

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh start
```

### Show status

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh status
```

### Follow logs

Press <kbd>Ctrl</kbd>+<kbd>C</kbd> to stop following the logs.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh logs
```

### Stop PostgreSQL while preserving data

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh stop
```

### Reset the database

Delete the database volume and start fresh. This command asks for confirmation.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh reset
```

For a non-interactive destructive reset:

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh reset --force
```

### Show help

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-postgres.sh help
```

## Local Redis

The Compose stack runs `redis:8.10.2-alpine` as Pricing's disposable cache. Redis is an optional
accelerator rather than a source of truth: Pricing remains live and ready when Redis is unavailable
and cached reads fall back to PostgreSQL.

Start Redis and wait until it is healthy:

```bash
docker compose up --detach --wait rentflow-redis
docker compose ps rentflow-redis
```

Follow its logs or stop it:

```bash
docker compose logs --follow rentflow-redis
docker compose stop rentflow-redis
```

Redis is available to host processes at `localhost:${REDIS_PORT:-6379}` and to containers in this
Compose project at `rentflow-redis:6379`. Set `REDIS_PORT` to change only the loopback-bound host
port. The local instance is unauthenticated and has no persistence volume, so it is appropriate for
development but not as a production topology.

## Local Kafka

The Compose stack runs the official `apache/kafka:4.3.1` image as one combined KRaft broker/controller.
This topology is intentionally small and uses plaintext connections, so it is
appropriate for local development but not a production Kafka deployment.

### Start Kafka and wait until healthy

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh start
```

### Show status or list topics

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh status
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh topics
```

### Follow logs

Press <kbd>Ctrl</kbd>+<kbd>C</kbd> to stop following the logs.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh logs
```

### Stop Kafka while preserving topics and messages

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh stop
```

### Reset Kafka

Delete every local topic and message and start a fresh broker. This command asks for confirmation and
removes only the Kafka volume.

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh reset
```

For a non-interactive destructive reset:

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh reset --force
```

### Show help

```bash
/home/yegor/Projects/RentFlow/rent-flow-common/scripts/local-kafka.sh help
```

### Bootstrap addresses

Kafka advertises two client listeners because `localhost` means different things on the host and inside a
container.

| Client location | Bootstrap servers |
| --- | --- |
| Service started from an IDE or shell on the host | `localhost:9092` |
| Service container in this Compose project | `rentflow-kafka:19092` |

Set `KAFKA_PORT` before running the script to change the host port. For example,
`KAFKA_PORT=19092 ./scripts/local-kafka.sh start` makes the host bootstrap address `localhost:19092`.
Port `29093` is private controller traffic and must not be used by applications.

Automatic topic creation is disabled deliberately. A misspelled topic must fail visibly instead of silently
creating a topic with accidental defaults.

## Combined cancellation flow

The combined Compose stack supplies Reservation with `rentflow-kafka:19092` and the `local`
profile. Reservation owns local creation of `rentflow.reservation.cancelled.v1`; Inventory owns
its consumer group and dead-letter topic. `POST /api/v1/reservations/{id}/cancel` commits the local
status/outbox transaction before returning `204`, and Inventory releases the item asynchronously.
New `HELD` reservations also expire automatically after their immutable deadline and use the same
outbox/Kafka/Inventory path; `CONFIRMED` reservations are excluded. Compose defaults the hold
duration to the service's production-like `10m`, while the combined smoke script overrides it to
`15s` so it can verify the timed flow without waiting ten minutes.

Run `./scripts/container-smoke-test.sh` to build all three services and verify the complete public
API → Reservation outbox → Kafka → Inventory path, including duplicate suppression and persistence
across restarts. It also verifies timed cancellation releases Inventory and that an elapsed
`CONFIRMED` reservation remains reserved. The smoke test also exercises Pricing's Redis-backed
cached retrieval, verifies that a cache entry is populated, and confirms cached reads fail open to
PostgreSQL while Redis is unavailable.

## Shared observability

Common owns Prometheus (metrics), Loki (logs), Tempo (traces), Alloy (collection), and Grafana
(investigation and alerting). All images are pinned in `compose.yaml`. The stack is optional:
application readiness and Compose dependencies do not depend on any telemetry component.

Start all services and observability from this repository:

```bash
COMPOSE_PROFILES=observability TRACING_EXPORT_ENABLED=true docker compose up --detach --build
```

Alternatively copy `.env.observability.example` to `.env`, adjust the local settings, and run
`docker compose up --detach --build`. Preserve any existing `.env` settings when doing so.
Open [Grafana](http://localhost:3000); the local credentials default to `admin` / `rentflow-local`.
The **RentFlow** folder contains overview, Inventory, Pricing, Reservation, and cancellation dashboards.
Explore has provisioned Prometheus, Loki, and Tempo datasources. Grafana's Alerting page shows 19 provisioned
rules; external notification delivery is not configured. Dashboards and rules come from
`observability/grafana/`; UI dashboard edits are disabled so changes stay in version control.

For a ten-minute guided workload with cache reads, reservation replays, cancellations, expiration,
and optional Redis/Inventory outages, see [the observability demo guide](OBSERVABILITY_DEMO.md).
Run `python3 scripts/observability-demo.py --start --with-faults` to start it; omit `--with-faults`
to generate traffic without stopping services.

The metrics pipeline scrapes every 15 seconds with a five-second timeout. Inventory, Pricing and
Reservation expose health and Prometheus on internal management ports `8085`, `8086` and `8087`,
respectively. Common binds those listeners inside Docker without publishing them. Application probes
remain on `8080`. Metrics use Prometheus pull; additional OTLP metric/log export is disabled.
All three services receive the shared environment, trace export and sampling settings in Compose.
Alloy receives OTLP HTTP at `rentflow-alloy:4318/v1/traces`
and forwards to Tempo. Prometheus retains exemplars; click a latency exemplar to open its trace.
Loki's `traceId` derived field opens Tempo, and Tempo offers a reverse link to the same trace's logs.

| Current coverage | Status |
| --- | --- |
| Inventory HTTP/JVM/Hikari/JDBC, idempotency, cancellation and cleanup | Metrics, ECS JSON logs and traces |
| Pricing HTTP/JVM/Hikari/JDBC, cache/fallback and Inventory dependency | Metrics, ECS JSON logs and traces, including HTTP continuity into Inventory |
| Reservation creation/claims, outbox, hold expiration, workers and cleanup | Metrics, ECS JSON logs and traces; durable cancellation origin context survives a service restart |
| PostgreSQL | Standard exporter with a dedicated `rentflow_monitor` login granted `pg_monitor` |
| Redis | Standard exporter, including connection health and memory |
| Kafka | Standard exporter for metadata-visible broker count, topic offsets and consumer-group lag; broker JVM/JMX metrics are not included |
| Cancellation workflow | Fresh Reservation publication/hold backlogs, Inventory completion, Kafka lag and DLT; publication and release remain separate outcomes |

For an **existing PostgreSQL volume**, initialize the exporter login once without resetting data:

```bash
docker compose exec --no-TTY rentflow-postgres \
  bash /docker-entrypoint-initdb.d/40-init-monitoring.sh
```

Fresh volumes run that script automatically after the three service schemas are created. The script
is idempotent and also updates the monitoring password after changing `POSTGRES_EXPORTER_PASSWORD`.
The exporter uses this login rather than an application or administrator login. `pg_monitor` can
read operational database statistics; exporter metrics do not collect query text or bind values.

### Collection, retention and access

Only containers labeled `rentflow.observability.collect=true` **in the active Compose project** are
collected. This avoids collecting unrelated projects on the same Docker daemon. Alloy parses ECS
JSON and preserves the original record. Alloy supplies only `service` and `environment` labels;
Loki also adds its bounded `service_name` alias for Grafana log navigation. Instance,
trace/span/event identifiers, serial numbers, offsets and exception
diagnostics remain searchable JSON fields. Plain text from services without ECS support is kept.
Common does not add automatic request body/header/query capture or SQL parameter capture.

Metrics, logs and traces retain seven days. Prometheus additionally caps stored blocks at 2 GB;
Loki and Tempo rely on retention and available disk capacity. Named volumes retain Grafana state,
telemetry, and Alloy read positions across container recreation. Local Tempo uses filesystem storage;
production needs supported object storage, capacity planning and a different availability topology.
Local trace sampling is 100%; adjust `TRACING_SAMPLING_PROBABILITY` when traffic grows.

Only Grafana (`127.0.0.1:3000`) and Alloy OTLP HTTP (`127.0.0.1:4318`) are published. Set
`GRAFANA_PORT` or `OTLP_HTTP_PORT` to change those host ports. Backend APIs, exporter endpoints,
and Alloy's diagnostic UI remain inside Docker. Grafana requires login; Loki/Tempo/OTLP are
unauthenticated inside this local network. Use private networking, authentication and TLS for
deployment outside local development. Diagnostic data can include SQL text, paths and exception
messages, so access and retention must match its sensitivity.

Alloy mounts the Docker socket to discover containers and read stdout. A read-only bind mount
does not restrict Docker API privileges; this is a trusted local collector. Use an API-restricted
socket proxy or a different log source in a production deployment.

For a service running on the host, retain its loopback management binding and use
`TRACING_EXPORT_ENABLED=true TRACING_OTLP_ENDPOINT=http://localhost:4318/v1/traces`. Container
scrapes and Docker log collection target the Compose applications; host-process metrics/logs need
separate targets and a log source. Set `TRACING_EXPORT_ENABLED=false` when running Compose without
collectors. Export queues and timeouts in all three services keep collector failure isolated from requests.

### Investigation and alerts

For a slow request, open **Inventory service**, inspect HTTP p95 and route request rate, and follow
an exemplar into Tempo. Compare HTTP duration with Hikari connection acquisition and JDBC query
spans; query spans retain SQL text. In Loki Explore, query:

```logql
{service="rent-flow-inventory",environment="local"} | json | traceId="YOUR_TRACE_ID"
```

For a delayed cancellation, open **Cancellation workflow**. HTTP `204` means Reservation committed
its local status/outbox transaction; inspect Inventory's eventual completion separately. Compare
completion-delay p95 with consumer-group lag. Search a serial number or event ID and follow its trace:

```logql
{service="rent-flow-inventory",environment="local"} | json | serialNumber="YOUR_SERIAL"
```

Retry and DLT failure logs retain exception causes. Reservation's publication attempt has its own trace,
linked to the original cancellation request and relay worker. Inventory continues the publication
trace. In a Reservation publication log, follow **originTraceId → View cancellation origin**, or follow
the publication span's native trace link in Tempo. The origin is persisted with the outbox row and
survives a Reservation restart. Search the same `eventId` across Reservation and Inventory when
investigating retries; do not assume the origin and publication have the same trace ID.

For Pricing cache degradation, open **Pricing service** and compare database entries by `reason`
with cache provider errors and Redis health. `read_error`/`decode_error` represent degraded cache
reads; ordinary `miss` entries are separate. Compare logical Inventory lookup duration with physical
HTTP attempts; one logical lookup includes retry waits, and an open circuit produces no HTTP attempt.
Generic cache and batch error meters overlap and must not be added as unique failure counts.

For Reservation, compare creation execution with committed outcomes; method execution does not prove
commit. Backlog count/age gauges describe the shared database. Panels take the **maximum across fresh
replicas**, using 45s freshness for outbox/expiration and 180s for cleanup. No fresh replica means
unknown, shown as missing data and covered by freshness alerts. Worker run/success timestamps and
monitoring refresh failures distinguish stalled work from stale measurements. These freshness and age
checks remain active when business workers are disabled. `RESERVATION_CANCELLATION_RELAY_ENABLED`
defaults to `true`; temporarily disabling publication does not disable backlog monitoring.

For cleanup, check **Backlog measurement age** before interpreting the backlog. The count is
database-wide, so the dashboard uses `max` rather than summing across instances. Compare worker
start/success timestamps and deletion failures separately from measurement refresh failures.

Provisioned Grafana alerts cover:

- Missing/failed scrapes for each application for two minutes, including an absent target.
- Any five-minute increase in cancellation DLT delivery or DLT publication failure.
- Backlog measurements older than 180 seconds, including zero after a three-minute startup grace.
- Last successful cleanup older than 26 hours; an initial zero timestamp gets 26 hours of uptime grace.
- PostgreSQL/Redis/Kafka backend or exporter unavailability for two minutes.
- Pricing business HTTP 5xx above 5% or p95 above 2s, with at least 20 matching business requests
  over five minutes, sustained for five minutes. These are provisional operational thresholds.
- Pricing degraded-cache database work, unavailable/short-circuited Inventory lookups, or an open
  Inventory circuit for two minutes; any eviction-error increase over five minutes.
- Fresh Reservation pending-publication age above 120s for one minute, or oldest overdue HELD
  delay above 30s for 30 seconds. Disabled workers do not suppress these age warnings.
- Missing/stale Reservation operational snapshots after 45s uptime, cleanup snapshots after 180s,
  and worker errors sustained for five minutes. One fresh replica per backlog family is sufficient.

Lazy counters can be absent before their first event; their alerts use `noDataState: OK`. DLT rules
also detect a newly appearing positive counter so its initial event is visible before a rate baseline exists. The scrape
alerts cover missing application metrics, and alert query execution failures remain visible as Error.
Pricing HTTP and Reservation age thresholds come from their service handoffs and need tuning against
representative workloads. They are not SLOs. Inventory HTTP/SLO and Kafka lag thresholds remain deferred
until a suitable workload baseline is available.

### Verification and lifecycle

During the security rollout, the full suite below is pending Reservation Phase 4. Use
`./scripts/security-smoke-test.sh --inventory` or `--pricing` for scoped Phase 2/3 evidence.
The final three-service business/observability requirements below remain required for restoration.

```bash
./scripts/container-smoke-test.sh
```

The combined smoke test uses the isolated `rentflow-common-smoke` project, enables observability,
checks the existing three-service business flows, and verifies each application's scrapes, ECS logs,
HTTP/JDBC traces and HTTP exemplars. It checks Pricing/Reservation HTTP continuity into Inventory,
durable outbox origin links across a Reservation restart, consumer lag, dashboard queries and alert
expressions. A paused relay allows cancellation to commit before the Reservation JVM is recreated;
the test then checks publication and Inventory release with the retained origin. It stops telemetry
to verify application readiness and reads, observes PostgreSQL recovery and Redis fallback, then
recreates the stack and verifies telemetry persisted. It removes only its own containers and volumes
on exit. Python 3, Docker Compose and curl are required. Export custom application ports before
invoking the combined test; Grafana credentials and port settings also support Compose's `.env`.

To check a running local stack without recreating it:

```bash
python3 scripts/observability-smoke-test.py
```

This issues list requests to all three services and checks their traces/exemplars. Pass
`--cancellation-serial SERIAL` to additionally verify a recently completed cancellation's logs, origin
link and publication/consumption trace. `--pricing-creation-trace TRACE_ID` and
`--reservation-creation-trace TRACE_ID` check existing creation traces for Inventory continuity.
The combined smoke supplies these IDs automatically. This check reads its
ports, credentials and environment from Compose's resolved configuration, including `.env`.

Stop all containers while preserving data with `docker compose --profile observability down`.
Avoid `down --volumes` for the regular project unless deleting database, Kafka and telemetry data
is intentional. Recreating Grafana keeps its administrator password in its volume; changing the
environment variable alone does not reset an existing Grafana account.

Configuration references: [Alloy Docker collection](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.docker/),
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
[Tempo 2 deployment](https://grafana.com/docs/tempo/latest/release-notes/version-2/v2-9/).
