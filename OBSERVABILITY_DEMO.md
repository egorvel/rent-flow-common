# Observability demo

`scripts/observability-demo.py` generates real application traffic so you can explore the
provisioned Grafana dashboards, logs, traces, and alerts without issuing requests manually.
It uses only Python's standard library and the existing Docker Compose stack.

The default workload lasts **10 minutes**, plus image build/startup, readiness checks, and final
verification. This gives the five-minute rate windows time to develop and shows recovery after
faults.

## Start a demonstration

From this repository, with Docker running and the three sibling service repositories available:

```bash
python3 scripts/observability-demo.py --start --with-faults
```

This command:

1. Builds and starts all applications and observability components using Compose.
2. Enables trace export with 100% sampling, shortens new reservation holds to 45 seconds, enables
   the cancellation relay, and sets Pricing's cache TTL to 30 seconds. It also initializes or updates
   the PostgreSQL monitoring login, including when reusing an existing database volume.
3. Creates uniquely named demo inventory and pricing records, then generates phased traffic.
4. Temporarily stops Redis and Inventory during their respective fault phases and restarts them.
5. Checks that demo logs reached Loki, creation traces crossed service boundaries in Tempo,
   application scrapes recovered, and a cancelled reservation released its inventory item.
6. Leaves the stack, demo records, and telemetry available for inspection.

The script writes through public application APIs. It does not reset volumes, modify SQL tables
directly, or change tracked configuration files. Records use a unique `OBS-<run-id>` prefix;
repeated runs create separate datasets. Existing business records are not selected for updates.

**Fault mode stops the selected project's Redis and Inventory containers.** Other traffic using
that same stack will experience the outages too. To generate traffic without stopping anything:

```bash
python3 scripts/observability-demo.py --start
```

If the stack is already running with tracing enabled, omit `--start` to preserve its runtime
settings:

```bash
python3 scripts/observability-demo.py
# Or explicitly include the temporary outages:
python3 scripts/observability-demo.py --with-faults
```

Without `--start`, the script reports the actual Reservation hold duration. With normal 10-minute
holds, automatically expired reservations may appear only near the end or after the run. The
45-second setting used by `--start` makes expiration visible during the demonstration. It applies
to newly created reservations; previously stored deadlines are unchanged.

## Watch Grafana while it runs

Open the Grafana URL printed by the script, normally <http://localhost:3000>.
The default login is `admin` / `rentflow-local`. Open the **RentFlow** dashboard folder,
select the printed environment (normally `local`), choose **Last 15 minutes**, and keep the
15-second refresh enabled.

Allow at least 30–60 seconds for the first useful graphs. Scrapes occur every 15 seconds;
the rate and percentile queries generally use rolling five-minute windows. A fault's effect
can remain visible in those windows after the underlying service has recovered.

The console announces phase changes with UTC timestamps and reports request counts every
30 seconds. For the default run with `--with-faults`, the approximate schedule is:

| Elapsed workload time | Phase | What to look for |
| --- | --- | --- |
| 0–1 minute | Warmup | Initial cache misses, database reads, low request rate |
| 1–3 minutes | Steady traffic | Higher request rate, cache hits, reservation creation/replay and cancellations |
| 3–6 minutes | Redis outage | `redis_up=0`, cache errors, database fallback, successful cached API responses |
| 6–7 minutes | Cache recovery | Redis returns, cache repopulates, fallback rate gradually declines |
| 7–8 minutes | Inventory outage | Failed Inventory scrapes, failed Pricing dependency probes, circuit/retry activity, queued Kafka cancellations |
| 8–10 minutes | Recovery and drain | Inventory returns, consumer catches up, delayed releases complete, traffic decreases |

Startup and recovery checks can shift the boundaries slightly. With no fault flag, the middle
of the run is busy healthy traffic instead. Durations other than 600 seconds scale the phase
boundaries proportionally.

## What each dashboard demonstrates

### RentFlow overview

- **Scrape health:** Inventory drops to zero during its outage. Other application scrapes remain
  available. Stopping Redis does not stop its exporter, so the exporter's scrape can remain healthy.
- **Redis availability:** `redis_up` falls during the Redis outage and recovers afterward.
- **PostgreSQL connections / Redis memory:** observe database pool activity and cache population.
- **Consumer lag:** can grow while Inventory is stopped, then drain when its Kafka consumer resumes.
- **Application HTTP requests:** compare the warmup, busy period, expected 400/404 responses,
  expected 422 responses for missing pricing dependencies, dependency 503 responses, and recovery.

The overview's Environment dropdown filters only the HTTP requests panel. Infrastructure panels
query the targets in the connected Prometheus instance without an environment selector.

### Pricing service

- **Single cache hits and misses / Batch entries:** single reads and four-item batch reads both run.
  The short TTL creates repeated misses and repopulation rather than only an initial warmup.
- **Database entries:** distinguish normal `miss` from `read_error` during the Redis outage.
- **Cache provider errors / Batch cache errors:** errors should increase while Redis is stopped.
  These metrics overlap; do not add them as unique failures.
- **Cache eviction:** periodic updates to one demo price exercise eviction and later repopulation.
  During the Redis outage, failed evictions can trigger the eviction alert.
- **Logical Inventory lookup outcomes / Physical Inventory HTTP attempts / Circuit state / Retries:**
  the script repeatedly tries to price a deliberately missing Inventory serial. Normally this
  produces 422 (Inventory itself returns 404); while Inventory is stopped it produces dependency
  failures and can open the circuit.
  An open circuit can reject a logical lookup without sending an HTTP request.
- **HTTP p95:** compare normal cache reads with fallback reads and dependency failures. This is a
  modest demonstration workload, so healthy latency may remain very low.

### Inventory service

- **HTTP request rate and latency:** real reads, writes, and status transitions run throughout.
- **Idempotency outcomes:** a status transition is repeated with the same key to demonstrate replay.
- **JVM heap / Hikari pool:** observe the application resources supporting those requests.
- **Cleanup/backlog measurement age:** monitoring continues even when no cleanup work is due.

### Reservation service

- **Creation execution / Committed outcomes:** each successful creation is replayed using its original
  idempotency key. Compare execution/replay telemetry with actual new committed creations.
- **Inventory claim:** creation traces include the Inventory status claim.
- **Publication outcomes and delays:** most new reservations are cancelled explicitly; others are
  left to expire. Both produce outbox/Kafka activity and eventual Inventory release.
- **Fresh backlog panels / Worker outcomes:** inspect measurement freshness and background progress.
- **Worker timestamps:** these should now display real dates following the timestamp-unit fix.

The script stops creating new reservations during the final recovery/drain phase. If you attached
to a stack with long holds, some expiration work will still happen after the script ends.

### Cancellation workflow

Follow Reservation publication, Kafka lag, Inventory outcomes, and completion-delay p95 together.
Immediately before the Inventory outage, the script creates three holds, then cancels them while
Inventory is stopped. Reservation can publish the events while Inventory's consumer is offline.
This provides work to catch up on after restart.

Fast healthy cancellation backlogs can remain zero because processing completes between scrapes.
That is expected. Kafka lag can also be zero while an event is still waiting in Reservation's
outbox: Kafka has not received it yet.

This scenario does not deliberately create malformed Kafka records or DLT failures. It also does
not wait for daily cleanup schedules or seven-day data retention. Those panels may remain quiet.

## Find logs and traces

Each run prints an output directory, for example:

```text
/tmp/rentflow-demo-20261027T120000Z-abcdef
```

It contains:

| File | Contents |
| --- | --- |
| `summary.json` | Run prefix, project, environment, HTTP status counts, demo serials/reservation IDs, sample trace IDs, dashboard URLs with the run's time range |
| `events.jsonl` | UTC phase/fault timestamps and each request's service, path, HTTP status, duration, trace ID, and whether its result was expected |

The final console output includes the dashboard URLs and sample trace IDs too. The files do not
contain Grafana passwords or request authorization headers. Metric charts aggregate all traffic
in the selected service/environment; the run prefix is for records and log searches, not a new
high-cardinality metric label.

In **Explore → Loki**, paste your actual printed prefix:

```logql
{environment="local",service=~"rent-flow-(inventory|pricing|reservation)"}
  |= "OBS-YOUR-RUN-ID"
```

For a particular serial, use a serial listed in `summary.json`:

```logql
{service="rent-flow-inventory",environment="local"}
  | json
  | serialNumber="YOUR_DEMO_SERIAL"
```

Open a result's fields and follow **traceId → View trace**. Reservation publication records also
provide **originTraceId → View cancellation origin**. The asynchronous publication has a separate
trace linked to the original cancellation request; Inventory continues the publication trace.

In **Explore → Tempo**, choose the Trace ID query and paste `pricing_to_inventory`,
`reservation_to_inventory`, or `cancellation_origin` from the summary. Expand the spans to compare
HTTP calls, database work, and service boundaries. From a span, use its logs link to return to Loki.

Latency panels can also expose clickable exemplars. Not every request is retained as an exemplar,
so use the saved trace IDs when you want a reliable starting point.

## Alerts

Open **Alerting → Alert rules → RentFlow operations** during and after the fault phases.
An individual scrape or metric change does not necessarily fire an alert immediately: rules
evaluate once a minute and have their own pending periods.

The default three-minute Redis outage gives the infrastructure-unavailable alert an opportunity
to fire. Cache fallback and eviction alerts can also become active. The one-minute Inventory
outage illustrates metrics, retries, circuit behavior and Kafka recovery; it is shorter than the
two-minute scrape alert's pending period. Increasing the duration to 1200 seconds extends that
outage to two minutes, although evaluation alignment still affects whether an alert fires.

No external notification delivery is configured. The demonstration does not guarantee every
alert will fire, and ordinary expected 400/404 responses are not HTTP 5xx errors.

## Options and interruption

```bash
# A shorter, three-minute walkthrough; useful for checking the script.
python3 scripts/observability-demo.py --start --with-faults --duration 180

# Twenty minutes gives longer plateaus and more time for alert pending periods.
python3 scripts/observability-demo.py --start --with-faults --duration 1200

# Keep the run files somewhere persistent; this must be a new directory.
python3 scripts/observability-demo.py --output-dir ./demo-run-001

python3 scripts/observability-demo.py --help
```

`--duration` accepts 120–1800 seconds. Short runs still generate useful traces/logs, but the
five-minute rolling queries and alert timing will have less time to develop.

Press **Ctrl+C** to stop traffic early. The script attempts to restart services it stopped before
exiting and saves the partial results. This also happens on ordinary errors and SIGTERM. A forced
kill or Docker failure can prevent recovery; the summary's `not_restored` list records remaining
faults. Recover them with the same project name and port environment you used for the run:

```bash
docker compose --profile observability start inventory rentflow-redis
```

A successful run exits with 0; unexpected responses or failed telemetry verification exit with 1;
an interrupted run exits with 130. Expected fault responses are recorded without failing the run.

## Compose settings and using a separate project

The script uses Compose to resolve `.env`, ports, credentials and the project name. You can select
another project using `--project-name`. A separate project also needs free host ports, for example:

```bash
INVENTORY_PORT=19080 PRICING_PORT=19081 RESERVATION_PORT=19082 \
POSTGRES_PORT=15442 REDIS_PORT=16382 KAFKA_PORT=19092 \
GRAFANA_PORT=13000 OTLP_HTTP_PORT=14318 \
python3 scripts/observability-demo.py --project-name rentflow-demo --start --with-faults
```

The printed Grafana links use the selected port. The script ensures Alloy's Compose-project filter
matches that project. Separate projects have separate default named volumes.

`--start` changes the running containers' hold duration, cache TTL and tracing settings; these
remain active after the script exits. To reconcile the stack with your ordinary Compose/.env
settings afterward, use the same project and port environment with:

```bash
docker compose --profile observability up --detach
```

To stop a stack while keeping its data, use `docker compose --profile observability down` with
the matching project/port environment. Demo data is retained intentionally for inspection; the
script does not perform automatic deletion or volume cleanup.

## Troubleshooting

- **Tracing is disabled:** rerun with `--start`, or explicitly enable tracing when recreating the
  application containers. Merely exporting a variable after startup cannot change an existing JVM.
- **PostgreSQL exporter connection never becomes ready:** existing volumes may predate the monitoring
  login. `--start` now initializes it automatically. For an already running stack, run
  `docker compose exec --no-TTY rentflow-postgres bash /docker-entrypoint-initdb.d/40-init-monitoring.sh`
  in the same project, then retry. This does not reset data. PostgreSQL can report a missing role
  as "password authentication failed" in the exporter; its own logs identify "Role ... does not exist".
- **Grafana rejects credentials:** an existing Grafana volume retains its administrator password.
  Supply the actual login using `--grafana-user` and `--grafana-password` if it differs from Compose.
- **A host port is occupied:** stop the conflicting stack or use a separate project and free ports.
- **Charts appear empty:** verify the selected environment and time range, and use the exact URLs
  in `summary.json`. A stopped script no longer adds traffic; use its saved absolute time range.
- **No fresh measurement / no data:** inspect scrape health and measurement-age panels. Empty data
  does not establish that a backlog is zero. Percentiles can also be undefined without observations.
- **Unexpected statuses:** inspect the corresponding `events.jsonl` entries and trace IDs. The script
  reports unexpected results rather than hiding them in the deliberate fault phases.
