"""Test-only transparent Inventory observer; no tokens, bodies or URLs are recorded."""

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import jwt

from check import settings
from common import Api, require


def main():
    config = settings()
    require(config.realm == "rentflow-smoke" and config.values["project"] == "rentflow-security-smoke",
            "Observer requires the isolated security smoke project")
    status, jwks = Api(config, loopback=False).request("GET", f"/realms/{config.realm}/protocol/openid-connect/certs")
    require(status == 200, "Observer public key retrieval")
    keys = {key["kid"]: jwt.PyJWK.from_dict(key, algorithm="RS256").key for key in jwks["keys"] if key.get("alg") == "RS256"}
    observations = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/_checks/observations":
                with lock:
                    body = json.dumps(observations).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return
            if not self.path.startswith("/api/v1/inventory/"):
                self.send_error(403)
                return
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            record = {"digest": hashlib.sha256(token.encode()).hexdigest(), "pricing_identity": False,
                      "exact_grants": False, "expires_at": 0,
                      "user_headers": any(name.lower().startswith("x-user-") for name in self.headers)}
            try:
                header = jwt.get_unverified_header(token)
                claims = jwt.decode(token, keys[header["kid"]], algorithms=["RS256"], issuer=config.issuer,
                                    leeway=30, options={"verify_aud": False, "require": ["sub", "azp", "exp", "iat"]})
                audience = claims.get("aud", [])
                audience = {audience} if isinstance(audience, str) else set(audience)
                record.update(pricing_identity=claims["azp"] == "pricing-service",
                              exact_grants=audience == {"inventory-api"} and
                              claims.get("resource_access") == {"inventory-api": {"roles": ["inventory:read"]}} and
                              not claims.get("realm_access", {}).get("roles"), expires_at=claims["exp"])
            except (jwt.PyJWTError, KeyError, TypeError, ValueError):
                pass
            connection = http.client.HTTPConnection("inventory", 8080, timeout=5)
            try:
                connection.request("GET", self.path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
                response = connection.getresponse()
                body = response.read(2_000_000)
                record["status"] = response.status
                with lock:
                    observations.append(record)
                self.send_response(response.status)
                for name in ("Content-Type", "WWW-Authenticate"):
                    if response.getheader(name):
                        self.send_header(name, response.getheader(name))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, http.client.HTTPException):
                if "status" not in record:
                    record["status"] = 503
                    with lock:
                        observations.append(record)
                self.send_error(503)
            finally:
                connection.close()

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Pricing observer failed") from None
