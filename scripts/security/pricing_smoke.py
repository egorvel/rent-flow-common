#!/usr/bin/env python3
"""Actual Pricing/Inventory/Keycloak acceptance, scoped to temporary smoke resources."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import subprocess
import sys
import threading

from common import Local, ROOT, SecurityError, maintenance, require, write_private
from policy import Policy, desired
from smoke import snapshot

SECRET_TARGET = "/run/secrets/spring.security.oauth2.client.registration.inventory.client-secret"


def main():
    local = Local()
    settings = local.settings
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-security-smoke",
            "Pricing acceptance requires the isolated security smoke project")
    before = snapshot(settings)
    observer_port = int(settings.values["pricing_port"]) + 1
    control_port = settings.pkce_port + 1
    require(observer_port <= 65535 and control_port <= 65535, "Smoke helper ports out of range")
    image = local.config["services"]["security-check"]["image"]
    recipient = settings.state / "runtime" / "pricing-recipient-secret"
    control_key = secrets.token_urlsafe(48)
    settings.save("pricing-smoke-control-key", control_key)
    issuer_environment = {
        "SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_ISSUER_URI": settings.issuer,
        "SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_JWK_SET_URI": settings.issuer + "/protocol/openid-connect/certs",
        "TRACING_EXPORT_ENABLED": "false"}
    override = settings.state / "runtime" / "pricing.compose.json"
    write_private(override, json.dumps({"services": {
        "inventory": {"environment": issuer_environment},
        "pricing": {"environment": dict(issuer_environment,
            SPRING_PROFILES_ACTIVE="local",
            SPRING_SECURITY_OAUTH2_CLIENT_PROVIDER_KEYCLOAK_TOKEN_URI=settings.issuer + "/protocol/openid-connect/token",
            INVENTORY_BASE_URL="http://inventory-observer:8080"),
            "secrets": [{"source": "pricing_recipient", "target": SECRET_TARGET.split("/")[-1]}]},
        "inventory-observer": {"image": image, "entrypoint": ["python3", "/check/pricing_observer.py"],
            "user": str(os.getuid()), "read_only": True, "tmpfs": ["/tmp:rw,nosuid,size=32m"],
            "volumes": [f"{settings.state / 'settings.json'}:/state/settings.json:ro"],
            "ports": [f"127.0.0.1:{observer_port}:8080"]}},
        "secrets": {"pricing_recipient": {"file": str(recipient)}}}, indent=2) + "\n")
    command = local.base + ["--file", str(override)]

    def compose(*args, capture=False):
        result = subprocess.run(command + list(args), cwd=ROOT, env=local.env, capture_output=capture, text=True)
        require(result.returncode == 0, "Pricing acceptance Compose operation failed: " + args[0])
        return result

    def deliver(invalid=False):
        write_private(recipient, (secrets.token_urlsafe(48) if invalid else
                                  settings.secret("pricing-service-client-secret")) + "\n")
        # Only this isolated recipient copy changes ownership. The original host secret stays
        # private to its owner. A direct file bind is readable inside Pricing as UID 10001.
        result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "0", "--read-only",
            "--volume", f"{recipient}:/recipient:rw", "--entrypoint", "python3", image, "-c",
            "import os; os.chown('/recipient',10001,10001); os.chmod('/recipient',0o400)"], cwd=ROOT)
        require(result.returncode == 0, "Pricing recipient file ownership setup")

    def restart(invalid=False):
        deliver(invalid)
        compose("up", "--detach", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "180", "pricing")

    deliver()
    compose("build", "inventory", "pricing")
    compose("up", "--detach", "--wait", "--wait-timeout", "240", "inventory", "rentflow-redis", "inventory-observer", "pricing")
    compose("exec", "--no-TTY", "--user", "10001", "pricing", "sh", "-ec",
            f"test -r {SECRET_TARGET} && test \"$(id -u)\" = 10001")
    inspect = subprocess.run(["docker", "inspect", compose("ps", "--quiet", "pricing", capture=True).stdout.strip()],
                             capture_output=True, text=True, check=True)
    mounts = json.loads(inspect.stdout)[0]["Mounts"]
    credential_mounts = [m for m in mounts if m["Destination"].startswith("/run/secrets/")]
    require(len(credential_mounts) == 1 and credential_mounts[0]["Destination"] == SECRET_TARGET
            and not credential_mounts[0]["RW"], "Pricing receives exactly its own read-only recipient secret")

    def data_state():
        # No credentials in argv/output: psql uses its existing container-local admin identity.
        rows = compose("exec", "--no-TTY", "rentflow-postgres", "psql", "--username",
            local.config["services"]["rentflow-postgres"]["environment"]["POSTGRES_USER"],
            "--dbname", local.config["services"]["rentflow-postgres"]["environment"]["POSTGRES_DB"],
            "--tuples-only", "--no-align", "--command",
            "SELECT COALESCE(json_agg(row_to_json(p) ORDER BY p.serial_number)::text, '[]') FROM pricing.pricing_entries p", capture=True)
        # Read raw cache values with a tiny in-container Lua script; never read recipient secrets.
        cache = compose("exec", "--no-TTY", "rentflow-redis", "redis-cli", "--raw", "EVAL",
            "local out={} for _,k in ipairs(redis.call('KEYS','rentflow:pricing:v1:*')) do "
            "table.insert(out,{k,redis.call('GET',k)}) end table.sort(out,function(a,b) return a[1]<b[1] end) "
            "return cjson.encode(out)", "0", capture=True)
        return {"rows": json.loads(rows.stdout), "cache": json.loads(cache.stdout)}

    class Control(BaseHTTPRequestHandler):
        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("X-Smoke-Control", ""), control_key):
                self.send_error(403)
                return
            try:
                response = {}
                if self.path == "/restart-pricing":
                    restart()
                elif self.path == "/bad-pricing-secret":
                    restart(invalid=True)
                elif self.path == "/stop-identity":
                    local.compose("stop", "rentflow-keycloak")
                elif self.path == "/start-identity":
                    local.compose("up", "--detach", "--wait", "--wait-timeout", "240", "rentflow-keycloak")
                elif self.path == "/data-state":
                    response = data_state()
                else:
                    self.send_error(404)
                    return
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_error(500, "Isolated smoke operation failed")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", control_port), Control)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        local.runner("pricing")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    require(not Policy(maintenance(settings), desired(settings)).run(), "Pricing tests restore managed realm policy")
    require(snapshot(settings) == before, "Pricing acceptance preserves subjects, client IDs, keys and original credentials")
    print("Pricing security interoperability verification passed")
    print("Combined acceptance remains pending Reservation Phase 4; no full-suite acceptance is claimed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ERROR: " + (str(error) if isinstance(error, SecurityError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
