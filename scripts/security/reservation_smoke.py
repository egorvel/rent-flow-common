#!/usr/bin/env python3
"""Reservation C1 control plane, only inside the disposable combined smoke project."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading

from common import Local, ROOT, SecurityError, deliver_recipients, maintenance, require, write_private
from policy import Policy, desired
from smoke import snapshot


def main():
    local = Local()
    settings = local.settings
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-common-smoke",
            "Reservation C1 requires the combined isolated project")
    before = snapshot(settings)
    image = local.config["services"]["security-check"]["image"]
    local.compose("build", "--quiet", "security-check")
    override = settings.state / "runtime/reservation-check.compose.json"
    write_private(override, json.dumps({"services": {
        "reservation": {"environment": {"INVENTORY_BASE_URL": "http://inventory-observer:8080",
                                         "RESERVATION_CANCELLATION_EXPIRATION_HOLD_DURATION": "1h",
                                         "RESERVATION_CANCELLATION_RELAY_ENABLED": "true"}},
        "inventory-observer": {"image": image, "entrypoint": ["python3", "/check/pricing_observer.py"],
            "user": str(os.getuid()), "read_only": True, "tmpfs": ["/tmp:rw,nosuid,size=32m"],
            "volumes": [f"{settings.state / 'settings.json'}:/state/settings.json:ro"],
            "ports": [f"127.0.0.1:{int(settings.values['reservation_port']) + 1}:8080"]}}}, indent=2) + "\n")
    command = local.base + ["--file", str(override)]

    def compose(*args, capture=False):
        result = subprocess.run(command + list(args), cwd=ROOT, env=local.env, capture_output=capture, text=True)
        require(result.returncode == 0, "Reservation C1 Compose operation failed: " + args[0])
        return result

    def restart(invalid=False):
        deliver_recipients(local, ("reservation-service",), invalid=invalid)
        compose("up", "--detach", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "reservation")

    compose("up", "--detach", "--no-deps", "--wait", "--wait-timeout", "180", "inventory-observer")
    restart()
    for service in ("pricing", "reservation"):
        local.compose("exec", "--no-TTY", service, "sh", "-ec",
            'test "$(id -u)" = 10001; test -r /run/secrets/spring.security.oauth2.client.registration.inventory.client-secret')
        container = local.compose("ps", "--quiet", service, capture=True).stdout.strip()
        inspected = json.loads(subprocess.check_output(["docker", "inspect", container], text=True))[0]
        mounted = [m for m in inspected["Mounts"] if m["Destination"].startswith("/run/secrets/")]
        require(len(mounted) == 1 and not mounted[0]["RW"] and
                mounted[0]["Source"].endswith(service + "-recipient-secret"), "Only the caller's own read-only secret is mounted")

    def state():
        environment = local.config["services"]["rentflow-postgres"]["environment"]
        sql = """SELECT json_build_object(
          'rows', (SELECT COALESCE(json_agg(r ORDER BY r.id)::text,'[]') FROM reservation.reservations r),
          'ledger', (SELECT COALESCE(json_agg(r ORDER BY r.caller_scope,r.idempotency_key)::text,'[]') FROM reservation.reservation_creation_requests r),
          'outbox', (SELECT COALESCE(json_agg(r.event_id ORDER BY r.event_id)::text,'[]') FROM reservation.reservation_cancellation_outbox r),
          'no_mapping', to_regclass('reservation.customer_identity_mappings') IS NULL);"""
        result = compose("exec", "--no-TTY", "rentflow-postgres", "psql", "--username", environment["POSTGRES_USER"],
                         "--dbname", environment["POSTGRES_DB"], "--tuples-only", "--no-align", "--command", sql, capture=True)
        return json.loads(result.stdout)

    class Control(BaseHTTPRequestHandler):
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("X-Smoke-Control", ""), settings.secret("smoke-proxy-key")):
                self.send_error(403)
                return
            try:
                response = {}
                if self.path == "/restart-reservation":
                    restart()
                elif self.path == "/bad-reservation-secret":
                    restart(invalid=True)
                elif self.path == "/stop-identity":
                    local.compose("stop", "rentflow-keycloak")
                elif self.path == "/start-identity":
                    local.compose("up", "--detach", "--wait", "--wait-timeout", "240", "rentflow-keycloak")
                elif self.path == "/data-state":
                    response = state()
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)
            except Exception:
                self.send_error(500, "Isolated Reservation operation failed")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", settings.pkce_port + 1), Control)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        local.runner("reservation")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        local.compose("up", "--detach", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "reservation")
        compose("rm", "--stop", "--force", "inventory-observer")
    require(not Policy(maintenance(settings), desired(settings)).run(), "Reservation C1 restores owned provider policy")
    require(snapshot(settings) == before, "Reservation C1 preserves subjects, keys, clients and source credentials")
    print("Reservation security interoperability verification passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ERROR: " + (str(error) if isinstance(error, SecurityError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
