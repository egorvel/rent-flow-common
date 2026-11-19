"""Real Keycloak-to-Inventory acceptance. Credentials remain in runner memory only."""

import http.client
import json
import uuid
from urllib.parse import urlencode

from common import Api, SecurityError, maintenance, require
from policy import Policy, desired


class Inventory:
    def __init__(self, settings):
        self.port = int(settings.values["inventory_port"])
        self.name = "Inventory"

    def request(self, method, path, token=None, data=None, key=None):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        body = None
        if data is not None:
            body = json.dumps(data)
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(2_000_000)
            result = json.loads(raw) if raw else None
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), result
        except (OSError, http.client.HTTPException, ValueError):
            raise SecurityError(self.name + " HTTP request failed") from None
        finally:
            connection.close()

    def expect(self, expected, method, path, token=None, data=None, key=None):
        status, headers, body = self.request(method, path, token, data, key)
        require(status == expected, f"{self.name} {method} {path.split('?')[0]} expected {expected}, received {status}")
        require("set-cookie" not in headers, self.name + " API does not create browser sessions")
        if expected in (401, 403):
            require(headers.get("content-type", "").startswith("application/problem+json"), "Safe denial media type")
            if method != "HEAD":
                require(body["code"] == ("AUTHENTICATION_REQUIRED" if expected == 401 else "ACCESS_DENIED"),
                        "Stable authentication/authorization denial")
                require(body["detail"] == ("Valid bearer authentication is required." if expected == 401
                                           else "This operation is not permitted."), "Sanitized denial detail")
            require("idempotency-replayed" not in headers, "Authorization denial does not return saved outcomes")
            if expected == 401:
                require(headers.get("www-authenticate", "").startswith("Bearer"), "Bearer challenge on authentication denial")
        return headers, body


def verify_inventory(settings, pkce, swagger, other_audience, renew):
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-security-smoke",
            "Inventory mutation tests require the isolated security smoke project")
    inventory = Inventory(settings)
    path = "/api/v1/inventory"
    history = "/api/v1/inventory-history?page=0&size=100&sort=timestamp&direction=asc"
    staff = pkce["staff"]["access_token"]
    staff_swagger = swagger["staff"]["access_token"]
    identity = Api(settings)
    pricing = identity.token("pricing-service", settings.secret("pricing-service-client-secret"))
    reservation = identity.token("reservation-service", settings.secret("reservation-service-client-secret"))
    serials = ["SECURITY-C1-A", "SECURITY-C1-B", "SECURITY-C1-C"]

    def item(serial, status="AVAILABLE"):
        return {"serialNumber": serial, "type": "Security test", "name": "Isolated Inventory fixture", "status": status}

    def transition(serial, target):
        return [{"serialNumber": serial, "status": target}]

    def state():
        return ([inventory.expect(200, "GET", path + "/" + serial, staff)[1] for serial in serials],
                inventory.expect(200, "GET", history, staff)[1])

    inventory.expect(200, "GET", "/livez")
    inventory.expect(200, "GET", "/readyz")
    for suffix, resource in [("", ""), ("/", "/"), ("/api/v1/inventory/SECURITY%2DC1", "/api/v1/inventory/SECURITY%2DC1")]:
        _, metadata = inventory.expect(200, "GET", "/.well-known/oauth-protected-resource" + suffix + "?ignored=true",
                                       "malformed-credential")
        require(metadata == {"resource": f"http://127.0.0.1:{inventory.port}" + resource,
                             "authorization_servers": [settings.issuer], "bearer_methods_supported": ["header"],
                             "tls_client_certificate_bound_access_tokens": False}, "Native public resource metadata")
    inventory.expect(401, "GET", path)
    for token in ["malformed-credential", other_audience, pkce["customer-a"]["id_token"],
                  pkce["customer-a"]["refresh_token"]]:
        inventory.expect(401, "GET", path, token)
    for serial in serials:
        inventory.expect(201, "POST", path, staff_swagger, item(serial))
    # PUT deliberately permits an arbitrary defined administrative status assignment.
    inventory.expect(200, "PUT", path + "/" + serials[2], staff, item(serials[2], "RETIRED"))
    require(inventory.expect(200, "GET", history, staff)[1]["page"]["totalElements"] == 0,
            "Administrative POST/PUT do not create lifecycle history")
    inventory.expect(200, "PUT", path + "/" + serials[2], staff, item(serials[2]))

    before = state()
    for user in ("customer-a", "customer-b"):
        for tokens in (pkce, swagger):
            token = tokens[user]["access_token"]
            inventory.expect(200, "GET", path, token)
            inventory.expect(200, "GET", path + "/" + serials[0], token)
            inventory.expect(403, "GET", history, token)
            inventory.expect(403, "POST", path, token, item("SECURITY-DENIED"))
            inventory.expect(403, "PUT", path + "/" + serials[0], token, item(serials[0], "RETIRED"))
            inventory.expect(403, "DELETE", path + "/" + serials[0], token)
            inventory.expect(403, "PATCH", path + "/status", token, transition(serials[0], "RESERVED"), str(uuid.uuid4()))
    inventory.expect(200, "GET", path + "/" + serials[0], pricing)
    for token in (pricing, reservation):
        inventory.expect(403, "GET", path, token)
        inventory.expect(403, "GET", history, token)
        inventory.expect(403, "POST", path, token, item("SECURITY-DENIED"))
        inventory.expect(403, "PUT", path + "/" + serials[0], token, item(serials[0], "RETIRED"))
        inventory.expect(403, "DELETE", path + "/" + serials[0], token)
    for serial in (serials[0], "SECURITY-NONEXISTENT"):
        inventory.expect(403, "GET", path + "/" + serial, reservation)
    inventory.expect(403, "PATCH", path + "/status", pricing, transition(serials[0], "RESERVED"), str(uuid.uuid4()))
    denied_key = str(uuid.uuid4())
    for target in ("AVAILABLE", "RENTED", "RETIRED", "INSPECTION_REQUIRED", "UNDER_MAINTENANCE"):
        inventory.expect(403, "PATCH", path + "/status", reservation, transition(serials[0], target), denied_key)
    inventory.expect(403, "PATCH", path + "/status", reservation,
                     transition(serials[0], "RESERVED") + transition(serials[1], "RETIRED"), denied_key)
    require(state() == before, "Denied customer/machine writes preserve items and lifecycle history")
    headers, _ = inventory.expect(204, "PATCH", path + "/status", reservation,
                                  transition(serials[0], "RESERVED"), denied_key)
    require(headers.get("idempotency-replayed") == "false", "Denied targets did not record a saved outcome")
    expiry = headers["idempotency-key-expires-at"]
    require(inventory.expect(200, "GET", path + "/" + serials[0], staff)[1]["status"] == "RESERVED",
            "Reservation machine successfully claims AVAILABLE equipment")
    headers, _ = inventory.expect(204, "PATCH", path + "/status", reservation,
                                  transition(serials[0], "RESERVED"), denied_key)
    require(headers.get("idempotency-replayed") == "true" and headers["idempotency-key-expires-at"] == expiry,
            "Authorized machine replay retains expiry")
    before = state()
    inventory.expect(403, "PATCH", path + "/status", reservation, transition(serials[0], "AVAILABLE"), denied_key)
    require(state() == before, "Forbidden Reservation target is denied before replay or mutation")

    # Staff can release; the same key used by another identity must not return staff's saved result.
    inventory.expect(204, "PATCH", path + "/status", staff, transition(serials[0], "AVAILABLE"), str(uuid.uuid4()))
    shared_key = str(uuid.uuid4())
    headers, _ = inventory.expect(204, "PATCH", path + "/status", staff_swagger,
                                  transition(serials[1], "RESERVED"), shared_key)
    require(headers.get("idempotency-replayed") == "false", "Staff status transition executes")
    staff_expiry = headers["idempotency-key-expires-at"]
    headers, _ = inventory.expect(204, "PATCH", path + "/status", staff,
                                  transition(serials[1], "RESERVED"), shared_key)
    require(headers.get("idempotency-replayed") == "true" and headers["idempotency-key-expires-at"] == staff_expiry,
            "Same verified subject replays across approved interactive clients")
    headers, _ = inventory.expect(409, "PATCH", path + "/status", reservation,
                                  transition(serials[1], "RESERVED"), shared_key)
    require(headers.get("idempotency-replayed") == "false", "Different machine subject cannot replay staff outcome")
    inventory.expect(403, "PATCH", path + "/status", pkce["customer-a"]["access_token"],
                     transition(serials[1], "RESERVED"), shared_key)

    # Only isolated, managed fixture grants change. Fresh reduced tokens must fail before replay.
    admin = maintenance(settings)
    user = admin.admin("GET", "/users?" + urlencode({"username": "staff", "exact": "true"}))[0]
    require(user.get("attributes", {}).get("rentflow.managed") == ["true"], "Managed isolated staff fixture")
    try:
        admin.admin("DELETE", f"/users/{user['id']}/role-mappings/realm", [admin.admin("GET", "/roles/STAFF")])
        admin.admin("POST", f"/users/{user['id']}/role-mappings/realm", [admin.admin("GET", "/roles/CUSTOMER")])
        reduced = renew("staff")["access_token"]
        before = state()
        inventory.expect(200, "GET", path, reduced)
        inventory.expect(403, "PATCH", path + "/status", reduced, transition(serials[1], "RESERVED"), shared_key)
        require(state() == before, "Reduced-permission replay denial leaves state unchanged")
    finally:
        Policy(admin, desired(settings), apply=True).run()
    renewed_staff = renew("staff")["access_token"]
    headers, _ = inventory.expect(204, "PATCH", path + "/status", renewed_staff,
                                  transition(serials[1], "RESERVED"), shared_key)
    require(headers.get("idempotency-replayed") == "true", "Renewed authorized token retains the subject replay scope")
    inventory.expect(200, "GET", history, renewed_staff)
    inventory.expect(204, "DELETE", path + "/" + serials[2], renewed_staff)
    for method in ("HEAD", "OPTIONS"):
        inventory.expect(403, method, path, staff)
    inventory.expect(403, "GET", "/actuator/prometheus", staff)
    print("Real Keycloak-to-Inventory customer/staff/machine permissions, denied-write state, metadata and replay checks passed")
