#!/usr/bin/env python3
"""Combined smoke lifecycle helpers; never operate on a developer realm or project."""

import http.client
import json
import os
import secrets
import subprocess
import sys
import time

from common import Local, ROOT, SecurityError, require


def main():
    local = Local()
    settings = local.settings
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-common-smoke",
            "Combined helpers require the isolated common smoke project")
    name = settings.values["project"] + "-security-proxy"
    if sys.argv[1:] == ["start"]:
        capability = secrets.token_urlsafe(48)
        settings.save("smoke-proxy-key", capability)
        settings.save("runtime/smoke-proxy-header", "X-Smoke-Control: " + capability + "\n")
        local.compose("build", "--quiet", "security-check")
        image = local.config["services"]["security-check"]["image"]
        result = subprocess.run(["docker", "run", "--detach", "--name", name,
            "--label", "rentflow.security.smoke=" + settings.values["project"], "--network", "host",
            "--add-host", "auth.rentflow.test:127.0.0.1", "--user", str(os.getuid()),
            "--read-only", "--shm-size", "512m", "--tmpfs", "/tmp:rw,nosuid,size=512m",
            "--volume", f"{settings.state}:/state:ro", image, "broker"], cwd=ROOT, capture_output=True, text=True)
        require(result.returncode == 0, "Smoke proxy container startup")
        for _ in range(90):
            connection = http.client.HTTPConnection("127.0.0.1", settings.pkce_port + 2, timeout=2)
            try:
                connection.request("GET", "/_smoke/ready", headers={"X-Smoke-Control": capability})
                response = connection.getresponse()
                response.read()
                if response.status == 200:
                    for resource in ("inventory", "pricing", "reservation"):
                        connection.close()
                        connection = http.client.HTTPConnection("127.0.0.1", settings.pkce_port + 2, timeout=15)
                        connection.request("GET", "/_smoke/refresh?resource=" + resource,
                                           headers={"X-Smoke-Control": capability})
                        refreshed = connection.getresponse()
                        refreshed.read()
                        require(refreshed.status == 200, "Smoke browser refresh for " + resource)
                    print("Authenticated smoke proxy ready; provider tokens remain in runner memory")
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(1)
        raise SecurityError("Authenticated smoke proxy did not become ready")
    raise SecurityError("Unknown combined helper command")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ERROR: " + (str(error) if isinstance(error, SecurityError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
