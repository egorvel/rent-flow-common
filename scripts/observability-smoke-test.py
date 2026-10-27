#!/usr/bin/env python3
"""Verify actual telemetry through Grafana's authenticated datasource proxies."""

import argparse
import base64
import http.client
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def request(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as response:
        return json.load(response)


def eventually(description, check, timeout=120):
    deadline = time.monotonic() + timeout
    error = "no matching data"
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                print(f"Verified {description}", flush=True)
                return result
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, KeyError, AssertionError) as failure:
            error = str(failure)
        time.sleep(2)
    raise SystemExit(f"ERROR: {description}: {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-only", action="store_true")
    parser.add_argument("--cancellation-serial", help="Verify a committed cancellation's log-to-trace link")
    parser.add_argument("--pricing-creation-trace", help="Verify a Pricing creation trace continues into Inventory")
    parser.add_argument("--reservation-creation-trace", help="Verify a Reservation creation trace continues into Inventory")
    parser.add_argument("--redis-fallback-only", action="store_true", help="Check live Pricing fallback while Redis is stopped")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    # Let Compose interpret .env/interpolation rather than evaluating .env as shell
    # code. Keep the configuration (which contains passwords) out of test output.
    config = json.loads(subprocess.check_output(
        ["docker", "compose", "--profile", "observability", "config", "--format", "json"], cwd=root))
    services = config["services"]
    grafana_service = services["rentflow-grafana"]
    grafana = f"http://127.0.0.1:{grafana_service['ports'][0]['published']}"
    grafana_env = grafana_service["environment"]
    credentials = grafana_env["GF_SECURITY_ADMIN_USER"] + ":" + grafana_env["GF_SECURITY_ADMIN_PASSWORD"]
    auth = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}

    def api(path):
        return request(grafana + path, auth)

    def proxy(source, path, params=None):
        suffix = "?" + urllib.parse.urlencode(params) if params else ""
        return api(f"/api/datasources/proxy/uid/rentflow-{source}" + path + suffix)

    def prom(expression):
        payload = proxy("prometheus", "/api/v1/query", {"query": expression})
        assert payload["status"] == "success", payload
        return payload["data"]["result"]

    eventually("Grafana database health", lambda: api("/api/health")["database"] == "ok")
    eventually("three provisioned datasources", lambda: {item["uid"] for item in api("/api/datasources")} >=
               {"rentflow-prometheus", "rentflow-loki", "rentflow-tempo"})
    if args.ready_only:
        return
    if args.redis_fallback_only:
        eventually("Pricing database fallback during Redis outage", lambda: prom(
            'pricing_cache_database_entries_total{service="rent-flow-pricing",reason="read_error"} > 0'))
        eventually("Redis outage visible to exporter", lambda: prom('redis_up == 0'))
        return

    datasources = {item["uid"]: item for item in api("/api/datasources")}
    derived = datasources["rentflow-loki"]["jsonData"]["derivedFields"][0]
    assert derived["datasourceUid"] == "rentflow-tempo" and derived["url"] == "${__value.raw}"
    origin_derived = next(field for field in datasources["rentflow-loki"]["jsonData"]["derivedFields"]
                          if field["name"] == "originTraceId")
    assert origin_derived["datasourceUid"] == "rentflow-tempo" and origin_derived["url"] == "${__value.raw}"
    reverse = datasources["rentflow-tempo"]["jsonData"]["tracesToLogsV2"]
    assert reverse["query"] == '{${__tags}} | json | traceId="${__trace.traceId}"'
    assert {tag["value"] for tag in reverse["tags"]} == {"service", "environment"}
    exemplars = datasources["rentflow-prometheus"]["jsonData"]["exemplarTraceIdDestinations"]
    assert exemplars[0]["name"] == "trace_id" and exemplars[0]["datasourceUid"] == "rentflow-tempo"

    for job in ("inventory", "pricing", "reservation", "postgres", "redis", "kafka", "prometheus", "alloy", "loki", "tempo"):
        eventually(f"{job} successful scrape", lambda job=job: prom(f'up{{job="{job}"}} == 1'))
    for job in ("inventory", "pricing", "reservation"):
        for metric in ("process_uptime_seconds", "hikaricp_connections"):
            expression = f'{metric}{{service="rent-flow-{job}"}}'
            eventually(expression, lambda expression=expression: prom(expression))
    for expression in ("pg_up == 1", "redis_up == 1", "kafka_brokers >= 1",
                       'inventory_idempotency_cleanup_backlog_last_success_timestamp > 0',
                       'inventory_idempotency_cleanup_expired_backlog >= 0',
                       'reservation_cancellation_outbox_pending >= 0',
                       'reservation_cancellation_expiration_overdue >= 0'):
        eventually(expression, lambda expression=expression: prom(expression))

    environment = services["inventory"]["environment"]["OBSERVABILITY_ENVIRONMENT"]
    for path in (root / "observability/grafana/dashboards").glob("*.json"):
        dashboard = json.loads(path.read_text())
        assert api("/api/dashboards/uid/" + dashboard["uid"])["dashboard"]["uid"] == dashboard["uid"]
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                prom(target["expr"].replace("$environment", environment))
    rules = api("/api/v1/provisioning/alert-rules")
    required_rules = {f"{job}-scrape-missing" for job in ("inventory", "pricing", "reservation")}
    required_rules.update({"pricing-database-fallback", "pricing-open-circuit", "reservation-outbox-aged",
                           "reservation-holds-overdue", "reservation-operational-stale", "reservation-cleanup-stale"})
    assert required_rules <= {rule["uid"] for rule in rules}, "Missing service alert rules"
    for rule in rules:
        prom(rule["data"][0]["model"]["expr"])
    print("Verified dashboards and alert query syntax", flush=True)

    def trace_with_sql(trace_id, service):
        trace = proxy("tempo", "/api/traces/" + trace_id)
        # Verify real HTTP and JDBC telemetry rather than a collector health endpoint.
        serialized = json.dumps(trace)
        return trace if service in serialized and (
            "jdbc.query[0]" in serialized or "db.statement" in serialized or "db.query.text" in serialized) else None

    def exemplar(trace_id, service, endpoint, headers):
        # Repeat after Micrometer's exemplar sampling interval if another request
        # won the first sample. The supplied trace keeps a single test identity.
        request(endpoint, headers)
        payload = proxy("prometheus", "/api/v1/query_exemplars", {
            "query": f'http_server_requests_seconds_bucket{{service="{service}"}}',
            "start": time.time() - 300, "end": time.time()})
        return any(sample["labels"].get("trace_id") == trace_id
                   for series in payload.get("data", []) for sample in series["exemplars"])

    for job, route in (("inventory", "inventory"), ("pricing", "pricing"), ("reservation", "reservations")):
        trace_id = uuid.uuid4().hex
        service = "rent-flow-" + job
        endpoint = f"http://127.0.0.1:{services[job]['ports'][0]['published']}/api/v1/{route}"
        headers = {"traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"}
        request(endpoint, headers)
        eventually(job + " HTTP/JDBC trace through Alloy and Tempo", lambda:
                   trace_with_sql(trace_id, service))
        eventually(job + " Prometheus exemplar linked to Tempo", lambda:
                   exemplar(trace_id, service, endpoint, headers))

    def cross_service_trace(trace_id, caller):
        trace = proxy("tempo", "/api/traces/" + trace_id)
        names = {attribute["value"].get("stringValue")
                 for batch in trace.get("batches", [])
                 for attribute in batch.get("resource", {}).get("attributes", [])
                 if attribute["key"] == "service.name"}
        return trace if {caller, "rent-flow-inventory"} <= names else None

    for trace_id, caller in ((args.pricing_creation_trace, "rent-flow-pricing"),
                             (args.reservation_creation_trace, "rent-flow-reservation")):
        if trace_id:
            eventually(caller + " HTTP trace continuity into Inventory", lambda:
                       cross_service_trace(trace_id, caller))

    def logs(serial=None, service="rent-flow-inventory", outcome=None, event_id=None):
        query = '{service=' + json.dumps(service) + '} | json'
        if serial:
            query += ' | serialNumber=' + json.dumps(serial) + ' | traceId!=""'
        if outcome:
            query += ' | outcome=' + json.dumps(outcome)
        if event_id:
            query += ' | eventId=' + json.dumps(event_id)
        payload = proxy("loki", "/loki/api/v1/query_range", {
            "query": query, "start": str(time.time_ns() - 1800 * 10**9),
            "end": str(time.time_ns()), "limit": "1000"})
        results = payload["data"]["result"]
        for stream in results:
            # Query-time JSON extraction produces extra labels; raw stream labels
            # are verified separately using /series below.
            for _, line in stream["values"]:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("service", {}).get("name") == service and (
                        outcome or not serial or record.get("outcome") in ("released", "already_available", "duplicate")):
                    return record
        return None

    for job in ("inventory", "pricing", "reservation"):
        service = "rent-flow-" + job
        eventually(job + " ECS JSON logs in Loki", lambda: logs(service=service))
        streams = proxy("loki", "/loki/api/v1/series", {"match[]": '{service=' + json.dumps(service) + '}'})["data"]
        assert streams and all(
            {"service", "environment"} <= set(stream) <= {"service", "environment", "service_name"}
            and stream.get("service_name", stream["service"]) == stream["service"]
            for stream in streams), streams
    print("Verified bounded service/environment Loki stream labels", flush=True)
    if args.cancellation_serial:
        eventually("committed cancellation metrics retained in Prometheus", lambda: prom(
            'max_over_time(inventory_reservation_cancellation_records_total{outcome="released"}[30m]) > 0'))
        eventually("completion delay histogram retained in Prometheus", lambda: prom(
            'max_over_time(inventory_reservation_cancellation_completion_delay_seconds_count[30m]) > 0'))
        record = eventually("committed cancellation JSON log", lambda: logs(args.cancellation_serial))
        cancellation_trace = record["traceId"]
        assert re.search(derived["matcherRegex"], json.dumps(record)).group(1) == cancellation_trace
        eventually("cancellation log-to-trace correlation", lambda:
                   proxy("tempo", "/api/traces/" + cancellation_trace))
        published = eventually("Reservation committed publication JSON log", lambda:
                               logs(service="rent-flow-reservation", outcome="published", event_id=record["eventId"]))
        assert published["traceId"] == cancellation_trace, "Inventory did not continue the publication trace"
        origin = published["originTraceId"]
        assert origin != cancellation_trace, "Publication should have its own trace linked to the origin"
        assert re.search(origin_derived["matcherRegex"], json.dumps(published)).group(1) == origin
        eventually("durable cancellation origin trace", lambda: proxy("tempo", "/api/traces/" + origin))

        def origin_link():
            trace = cross_service_trace(cancellation_trace, "rent-flow-reservation")
            if not trace:
                return None
            for batch in trace.get("batches", []):
                for scope in batch.get("scopeSpans", []):
                    for span in scope.get("spans", []):
                        for link in span.get("links", []):
                            value = link["traceId"]
                            linked_id = value if re.fullmatch(r"[a-fA-F0-9]{32}", value) else base64.b64decode(value).hex()
                            if linked_id == origin:
                                return True
            return None

        eventually("publication span link to durable cancellation origin", origin_link)
        eventually("cancellation consumer lag metrics", lambda: prom(
            'kafka_consumergroup_lag{consumergroup="rentflow.inventory.reservation-cancellation.v1"}'))
    print("Observability smoke verification passed", flush=True)


if __name__ == "__main__":
    main()
