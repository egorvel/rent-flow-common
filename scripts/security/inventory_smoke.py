#!/usr/bin/env python3
"""C1 acceptance against real Inventory using a temporary, test-only Compose override."""

import http.client
import json
import subprocess
import sys
import time

from common import Local, ROOT, SecurityError, maintenance, require, write_private
from policy import Policy, desired
from smoke import snapshot


def main():
    local = Local()
    settings = local.settings
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-security-smoke",
            "C1 requires the isolated security smoke project")
    before = snapshot(settings)
    override = settings.state / "runtime" / "inventory.compose.json"
    write_private(override, json.dumps({"services": {"inventory": {"environment": {
        "SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_ISSUER_URI": settings.issuer,
        "SPRING_SECURITY_OAUTH2_RESOURCESERVER_JWT_JWK_SET_URI": settings.issuer + "/protocol/openid-connect/certs",
        "TRACING_EXPORT_ENABLED": "false"
    }}}}, indent=2) + "\n")
    command = local.base + ["--file", str(override)]

    def compose(*args):
        result = subprocess.run(command + list(args), cwd=ROOT, env=local.env)
        require(result.returncode == 0, "Inventory acceptance Compose operation failed: " + args[0])

    compose("build", "inventory")
    compose("up", "--detach", "--wait", "--wait-timeout", "240", "inventory")
    for attempt in range(90):
        connection = http.client.HTTPConnection("127.0.0.1", int(settings.values["inventory_port"]), timeout=2)
        try:
            connection.request("GET", "/readyz")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                break
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(1)
    else:
        raise SecurityError("Inventory did not become ready for real-provider acceptance")
    local.runner("inventory")
    require(not Policy(maintenance(settings), desired(settings)).run(), "Inventory tests restore all managed realm policy")
    require(snapshot(settings) == before, "Inventory acceptance preserves subjects, keys and credentials")
    print("Inventory security interoperability verification passed")
    print("Pricing acceptance is available via --pricing; combined acceptance remains pending Reservation Phase 4.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ERROR: " + (str(error) if isinstance(error, SecurityError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
