#!/usr/bin/env python3
"""Generate a ten-minute RentFlow workload for Grafana; optionally demonstrate outages."""

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("inventory", "pricing", "reservation")
ROUTES = {"inventory": "inventory", "pricing": "pricing", "reservation": "reservations"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def http_request(url, method="GET", body=None, headers=None, timeout=8):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read()
        return response.code, json.loads(raw) if raw else None


def wait_ready(description, check, timeout=180):
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
            last_error = str(error)
        time.sleep(2)
    raise RuntimeError(f"{description} did not become ready within {timeout}s: {last_error}")


class Demo:
    def __init__(self, args):
        self.args = args
        self.env = os.environ.copy()
        if args.project_name:
            self.env["COMPOSE_PROJECT_NAME"] = args.project_name
        if args.start:
            self.env.update(TRACING_EXPORT_ENABLED="true", TRACING_SAMPLING_PROBABILITY="1.0",
                            RESERVATION_CANCELLATION_EXPIRATION_HOLD_DURATION="45s",
                            RESERVATION_CANCELLATION_RELAY_ENABLED="true", PRICING_CACHE_TTL="30s")
        config = self.compose_config()
        # Alloy must use the same project label even when the name came from .env.
        self.env["COMPOSE_PROJECT_NAME"] = config["name"]
        self.config = self.compose_config()
        self.services = self.config["services"]
        self.urls = {name: self.service_url(name) for name in SERVICES}
        self.grafana = self.service_url("rentflow-grafana")
        grafana_env = self.services["rentflow-grafana"]["environment"]
        user = args.grafana_user or grafana_env["GF_SECURITY_ADMIN_USER"]
        password = args.grafana_password or grafana_env["GF_SECURITY_ADMIN_PASSWORD"]
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}
        self.environment = self.services["inventory"]["environment"]["OBSERVABILITY_ENVIRONMENT"]
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.prefix = "OBS-" + self.run_id
        self.output = Path(args.output_dir) if args.output_dir else Path(tempfile.gettempdir()) / ("rentflow-demo-" + self.run_id)
        self.output.mkdir(parents=True, exist_ok=False)
        self.events = (self.output / "events.jsonl").open("w", buffering=1)
        self.lock = threading.Lock()
        self.counts = Counter()
        self.unexpected = 0
        self.disrupted = set()
        self.phase = "setup"
        self.resources = []
        self.samples = {}
        self.sequence = 0
        self.catalog = [f"{self.prefix}-C{i:02}" for i in range(12)]
        self.started = time.time()
        self.workload_started = None
        self.event("run", project=self.config["name"], prefix=self.prefix)

    def compose(self, *arguments, capture=False):
        return subprocess.run(["docker", "compose", "--profile", "observability", *arguments],
                              cwd=ROOT, env=self.env, check=True, text=True,
                              stdout=subprocess.PIPE if capture else None, timeout=900)

    def compose_config(self):
        return json.loads(self.compose("config", "--format", "json", capture=True).stdout)

    def service_url(self, name):
        ports = self.services[name].get("ports", [])
        target = 3000 if name == "rentflow-grafana" else 8080
        port = next(port["published"] for port in ports if int(port["target"]) == target)
        return f"http://127.0.0.1:{port}"

    def event(self, kind, **fields):
        with self.lock:
            self.events.write(json.dumps({"time": utc_now(), "kind": kind, "phase": self.phase, **fields}) + "\n")

    def say(self, message):
        print(f"[{utc_now()}] {message}", flush=True)

    def grafana_api(self, path):
        status, body = http_request(self.grafana + path, headers=self.auth)
        if status != 200:
            raise RuntimeError(f"Grafana {path}: HTTP {status}; check credentials (--grafana-user/--grafana-password)")
        return body

    def prom(self, expression):
        query = urllib.parse.urlencode({"query": expression})
        return self.grafana_api("/api/datasources/proxy/uid/rentflow-prometheus/api/v1/query?" + query)["data"]["result"]

    def request(self, service, path, method="GET", body=None, expected=(200,), headers=None, sample=None):
        trace_id = uuid.uuid4().hex
        request_headers = {"traceparent": f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01", **(headers or {})}
        start = time.monotonic()
        error = None
        try:
            status, payload = http_request(self.urls[service] + path, method, body, request_headers)
        except (OSError, ValueError, http.client.HTTPException) as failure:
            status, payload, error = 0, None, str(failure)
        allowed = status in expected or (service == "inventory" and "inventory" in self.disrupted and status == 0)
        with self.lock:
            self.counts[f"{service}:{status}"] += 1
            if not allowed:
                self.unexpected += 1
            if sample and allowed and 200 <= status < 300:
                self.samples.setdefault(sample, trace_id)
        self.event("request", service=service, method=method, path=path, status=status,
                   duration_ms=round((time.monotonic() - start) * 1000, 2), trace_id=trace_id,
                   expected=allowed, error=error)
        if not allowed:
            self.say(f"Unexpected response: {service} {method} {path}: {status} {str(payload or error)[:180]}")
        return status, payload

    @staticmethod
    def item(serial):
        return {"serialNumber": serial, "type": "Observability demo", "name": "Demo equipment", "status": "AVAILABLE"}

    @staticmethod
    def price(serial, amount=125.50):
        return {"serialNumber": serial, "price": amount, "weekendRate": 1.25,
                "longRentalCondition": 7, "longRentalDiscount": 0.1, "deposit": 300}

    def require(self, result, expected=201):
        status, payload = result
        if status != expected:
            raise RuntimeError(f"Cannot prepare demo data: expected HTTP {expected}, got {status}: {payload}")
        return payload

    def runtime_settings(self, name):
        container = self.compose("ps", "--quiet", name, capture=True).stdout.strip()
        if not container or "\n" in container:
            raise RuntimeError(f"Expected one running Compose container for {name}; start the stack with --start")
        actual = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
        if name in SERVICES or name == "rentflow-grafana":
            target = "3000/tcp" if name == "rentflow-grafana" else "8080/tcp"
            expected = str(urllib.parse.urlparse(self.service_url(name)).port)
            published = actual["NetworkSettings"]["Ports"].get(target) or []
            if not any(binding["HostPort"] == expected for binding in published):
                raise RuntimeError(f"{name} runtime ports differ from Compose; use its original port environment or --start")
        return dict(entry.split("=", 1) for entry in actual["Config"]["Env"] if "=" in entry)

    def prepare(self):
        self.say(f"Project: {self.config['name']}; output: {self.output}")
        if self.args.start:
            self.say("Starting stack with tracing, 45s holds and a 30s cache TTL (build/startup is additional time).")
            self.compose("up", "--detach", "--build")
            # PostgreSQL's entrypoint skips init scripts on an existing data volume.
            # Reconcile the monitoring login/password without changing application data.
            self.say("Initializing the PostgreSQL monitoring login for new or existing volumes.")
            self.compose("exec", "--no-TTY", "rentflow-postgres", "bash",
                         "/docker-entrypoint-initdb.d/40-init-monitoring.sh")
        for name in SERVICES:
            settings = self.runtime_settings(name)
            if settings.get("TRACING_EXPORT_ENABLED", "false").lower() != "true":
                raise RuntimeError(f"{name} tracing is disabled. Rerun with --start to recreate applications with tracing.")
            if settings.get("OBSERVABILITY_ENVIRONMENT") != self.environment:
                raise RuntimeError(f"{name} runtime environment differs from Compose; use --start to reconcile it.")
            if name == "reservation":
                self.say("Running Reservation hold duration: " + settings.get("RESERVATION_CANCELLATION_EXPIRATION_HOLD_DURATION", "10m"))
            wait_ready(name, lambda name=name: http_request(self.urls[name] + "/readyz")[0] == 200)
        self.runtime_settings("rentflow-grafana")
        if self.runtime_settings("rentflow-alloy").get("COMPOSE_PROJECT_NAME") != self.config["name"]:
            raise RuntimeError("Alloy's project filter differs from this Compose project; use --start to reconcile it")
        wait_ready("Grafana", lambda: self.grafana_api("/api/health").get("database") == "ok")
        self.grafana_api("/api/datasources")
        jobs = "inventory|pricing|reservation|postgres|redis|kafka|prometheus|alloy|loki|tempo"
        wait_ready("all ten scrape targets", lambda: len(self.prom(f'up{{job=~"{jobs}"}} == 1')) == 10)
        try:
            wait_ready("PostgreSQL exporter connection", lambda: bool(self.prom("pg_up == 1")), timeout=45)
        except RuntimeError as error:
            raise RuntimeError(f"{error}. Check rentflow-postgres-exporter logs; rerun with --start "
                               "to initialize the monitoring login on an existing database volume. "
                               "See OBSERVABILITY_DEMO.md for manual initialization.") from error
        wait_ready("Redis and Kafka backends", lambda: bool(self.prom("redis_up == 1")) and bool(self.prom("kafka_brokers >= 1")), timeout=45)
        for serial in self.catalog:
            self.require(self.request("inventory", "/api/v1/inventory", "POST", self.item(serial), (201,)))
            self.require(self.request("pricing", "/api/v1/pricing", "POST", self.price(serial), (201,), sample="pricing_to_inventory"))
            self.resources.append({"serial": serial, "kind": "catalog"})
        self.save_summary("running")

    def new_reservation(self, cancel=True):
        self.sequence += 1
        serial = f"{self.prefix}-R{self.sequence:04}"
        status, _ = self.request("inventory", "/api/v1/inventory", "POST", self.item(serial), (201,))
        if status != 201:
            return None
        record = {"serial": serial, "kind": "cancel" if cancel else "expire"}
        self.resources.append(record)
        tomorrow = date.today() + timedelta(days=1)
        body = {"customerId": self.prefix, "orderId": serial,
                "items": [{"serialNumber": serial, "startDate": tomorrow.isoformat(),
                           "endDate": (tomorrow + timedelta(days=2)).isoformat()}]}
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        status, payload = self.request("reservation", "/api/v1/reservations", "POST", body, (201,), headers,
                                       sample="reservation_to_inventory")
        if status != 201:
            return None
        reservation_id = payload[0]["id"]
        record["reservation_id"] = reservation_id
        self.request("reservation", "/api/v1/reservations", "POST", body, (201,), headers)
        self.request("reservation", "/api/v1/reservations/" + reservation_id)
        if cancel:
            self.cancel(reservation_id)
        self.event("reservation", resource=record)
        return reservation_id

    def cancel(self, reservation_id):
        self.request("reservation", f"/api/v1/reservations/{reservation_id}/cancel", "POST", expected=(204,), sample="cancellation_origin")

    def read_bundle(self):
        serial = random.choice(self.catalog)
        self.request("inventory", "/api/v1/inventory/" + serial)
        self.request("pricing", f"/api/v1/pricing/{serial}/cached")
        self.request("pricing", "/api/v1/pricing/batch/cached", "POST", random.sample(self.catalog, 4))
        self.request("reservation", "/api/v1/reservations?page=0&size=10")

    def change_phase(self, name):
        self.phase = name
        self.event("phase", elapsed_seconds=round(time.monotonic() - self.workload_started))
        self.say("Phase: " + name)

    def stop_service(self, service):
        # Mark first: restoration also runs if Docker stop is interrupted midway.
        self.disrupted.add(service)
        self.event("fault_start", service=service)
        self.compose("stop", "--timeout", "5", service)

    def restore(self):
        for service in list(self.disrupted):
            self.say("Restoring " + service)
            self.compose("start", service)
            if service == "inventory":
                wait_ready("Inventory recovery", lambda: http_request(self.urls["inventory"] + "/readyz")[0] == 200)
            else:
                wait_ready("Redis recovery", lambda: bool(self.prom("redis_up == 1")), timeout=90)
            self.disrupted.remove(service)
            self.event("fault_end", service=service)

    def workload(self):
        self.workload_started = time.monotonic()
        next_lifecycle = next_probe = next_progress = 0
        last_phase = None
        with ThreadPoolExecutor(max_workers=3) as workers:
            while True:
                elapsed = time.monotonic() - self.workload_started
                fraction = elapsed / self.args.duration
                if fraction >= 1:
                    break
                phase = ("warmup" if fraction < .1 else "steady traffic" if fraction < .3 else
                         "Redis outage" if fraction < .6 else "cache recovery" if fraction < .7 else
                         "Inventory outage" if fraction < .8 else "recovery and drain")
                if not self.args.with_faults:
                    phase = "warmup" if fraction < .1 else "busy traffic" if fraction < .8 else "recovery and drain"
                if phase != last_phase:
                    self.restore()
                    self.change_phase(phase)
                    if self.args.with_faults and phase == "Redis outage":
                        self.stop_service("rentflow-redis")
                    elif self.args.with_faults and phase == "Inventory outage":
                        # Commit holds while Inventory is up, then queue cancellations while its consumer is down.
                        pending = [self.new_reservation(cancel=False) for _ in range(3)]
                        self.stop_service("inventory")
                        for reservation_id in pending:
                            if reservation_id:
                                self.cancel(reservation_id)
                    last_phase = phase
                bundles = 1 if fraction < .1 or fraction >= .8 else 3
                for future in [workers.submit(self.read_bundle) for _ in range(bundles)]:
                    future.result()
                if elapsed >= next_probe:
                    self.request("inventory", "/api/v1/inventory/" + self.prefix + "-MISSING", expected=(404,))
                    self.request("pricing", "/api/v1/pricing/batch/cached", "POST", [], (400,))
                    # New serial: repository duplicate checks must not bypass the Inventory dependency.
                    self.request("pricing", "/api/v1/pricing", "POST", self.price(self.prefix + "-MISSING"),
                                 (422, 503) if self.args.with_faults else (422,))
                    next_probe = elapsed + 5
                if elapsed >= next_lifecycle and "inventory" not in self.disrupted and fraction < .8:
                    self.new_reservation(cancel=self.sequence % 3 != 0)
                    # A separate idempotent Inventory status request and replay, confined to demo data.
                    body = [{"serialNumber": self.catalog[0], "status": "RESERVED"}]
                    headers = {"Idempotency-Key": str(uuid.uuid4())}
                    for _ in range(2):
                        self.request("inventory", "/api/v1/inventory/status", "PATCH", body, (204,), headers)
                    self.request("inventory", "/api/v1/inventory/status", "PATCH",
                                 [{"serialNumber": self.catalog[0], "status": "AVAILABLE"}], (204,),
                                 {"Idempotency-Key": str(uuid.uuid4())})
                    # Updating a demo price also exercises cache eviction and subsequent repopulation.
                    self.request("pricing", "/api/v1/pricing/" + self.catalog[0], "PUT",
                                 self.price(self.catalog[0], 120 + self.sequence % 10))
                    next_lifecycle = elapsed + 15
                if elapsed >= next_progress:
                    self.say(f"{int(elapsed)}/{self.args.duration}s; {sum(self.counts.values())} requests; "
                             f"{self.unexpected} unexpected responses")
                    next_progress = elapsed + 30
                    self.save_summary("running")
                time.sleep(1 if bundles > 1 else 2)
        self.restore()

    def save_summary(self, status):
        end = time.time()
        params = urllib.parse.urlencode({"from": int(self.started * 1000), "to": int(end * 1000),
                                         "var-environment": self.environment})
        dashboards = {name: f"{self.grafana}/d/rentflow-{name}?{params}" for name in
                      ("overview", "inventory", "pricing", "reservation", "cancellation")}
        summary = {"run_id": self.run_id, "status": status, "project": self.config["name"],
                   "prefix": self.prefix, "environment": self.environment,
                   "started_at": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
                   "ended_at": utc_now(), "duration_seconds": self.args.duration,
                   "with_faults": self.args.with_faults, "requests": dict(self.counts),
                   "unexpected_responses": self.unexpected, "not_restored": sorted(self.disrupted),
                   "sample_traces": self.samples, "resources": self.resources, "dashboards": dashboards}
        temporary = self.output / "summary.tmp"
        temporary.write_text(json.dumps(summary, indent=2) + "\n")
        temporary.replace(self.output / "summary.json")
        return dashboards

    def verify_results(self):
        self.change_phase("verify collected telemetry")
        for sample, caller in (("pricing_to_inventory", "rent-flow-pricing"),
                               ("reservation_to_inventory", "rent-flow-reservation")):
            trace_id = self.samples[sample]

            def collected_trace():
                trace = self.grafana_api("/api/datasources/proxy/uid/rentflow-tempo/api/traces/" + trace_id)
                content = json.dumps(trace)
                return caller in content and "rent-flow-inventory" in content

            wait_ready(sample + " trace in Tempo", collected_trace, timeout=90)
            self.event("verified_trace", sample=sample, trace_id=trace_id)
        query = urllib.parse.urlencode({
            "query": '{service="rent-flow-inventory"} |= ' + json.dumps(self.prefix),
            "start": str(int(self.started * 10**9)), "end": str(time.time_ns()), "limit": 10})
        wait_ready("demo logs in Loki", lambda: bool(self.grafana_api(
            "/api/datasources/proxy/uid/rentflow-loki/loki/api/v1/query_range?" + query)["data"]["result"]), timeout=90)
        for service in SERVICES:
            wait_ready(service + " recovered scrape", lambda service=service: bool(self.prom(f'up{{job="{service}"}} == 1')), timeout=90)
        cancelled = next(record for record in self.resources if record.get("reservation_id") and record["kind"] == "cancel")
        wait_ready("Inventory release after cancellation", lambda: http_request(
            self.urls["inventory"] + "/api/v1/inventory/" + cancelled["serial"])[1].get("status") == "AVAILABLE", timeout=90)
        self.event("verified", checks=["cross-service traces", "demo logs", "recovered scrapes", "committed cancellation release"])
        self.say("Verified demo logs in Loki, cross-service traces in Tempo, recovered scrapes and an Inventory release.")

    def finish(self, status):
        links = self.save_summary(status)
        self.say(f"Run {status}; saved events and summary in {self.output}")
        for name, link in links.items():
            print(f"  {name}: {link}", flush=True)
        print("Sample trace IDs: " + json.dumps(self.samples), flush=True)
        self.events.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true", help="Build/start Compose with tracing, 45s holds and 30s cache TTL")
    parser.add_argument("--with-faults", action="store_true", help="Temporarily stop Redis and Inventory in this Compose project")
    parser.add_argument("--duration", type=int, default=600, metavar="SECONDS", help="Workload duration, 120–1800 seconds (default: 600)")
    parser.add_argument("--project-name", help="Compose project; defaults to the normal Compose/.env project")
    parser.add_argument("--output-dir", help="New directory for summary.json and events.jsonl (default: a unique /tmp directory)")
    parser.add_argument("--grafana-user", help="Existing Grafana login if different from Compose")
    parser.add_argument("--grafana-password", help="Existing Grafana password if different from Compose")
    args = parser.parse_args()
    if not 120 <= args.duration <= 1800:
        parser.error("--duration must be between 120 and 1800 seconds")
    return args


def main():
    args = parse_args()
    demo = None
    status = "failed"
    interrupted = False

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        demo = Demo(args)
        demo.prepare()
        demo.say(f"Open {demo.grafana}; select Last 15 minutes and environment {demo.environment}.")
        demo.workload()
        demo.verify_results()
        status = "completed" if demo.unexpected == 0 else "completed_with_errors"
    except KeyboardInterrupt:
        interrupted = True
        status = "interrupted"
        print("Stopping traffic and restoring interrupted services...", flush=True)
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
    finally:
        if demo:
            # A second Ctrl+C must not interrupt normal recovery of stopped services.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            try:
                demo.restore()
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                status = "recovery_failed"
                print(f"ERROR restoring services: {error}. Run docker compose --profile observability start inventory rentflow-redis",
                      file=sys.stderr, flush=True)
            demo.finish(status)
    return 130 if interrupted else 0 if status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
