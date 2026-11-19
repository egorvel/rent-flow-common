"""Pricing C1: real browsers, provider, application images and protected recipient mount."""

import hashlib
import http.client
import json
import time
from urllib.parse import urlencode

from common import Api, SecurityError, maintenance, require
from inventory_check import Inventory
from policy import Policy, desired


class Pricing(Inventory):
    def __init__(self, settings):
        self.name = "Pricing"
        self.port = int(settings.values["pricing_port"])


def verify_pricing(settings, pkce, swagger, inventory_tokens, renew):
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-security-smoke",
            "Pricing mutation checks require the isolated smoke project")
    pricing, inventory = Pricing(settings), Inventory(settings)
    path = "/api/v1/pricing"
    staff = pkce["staff"]["access_token"]
    inventory_staff = inventory_tokens["staff"]["access_token"]
    admin = maintenance(settings)
    client = admin.client("pricing-service")
    original_attributes = dict(client.get("attributes", {}))
    attributes = dict(original_attributes, **{"access.token.lifespan": "45"})
    admin.admin("PUT", "/clients/" + client["id"], {"attributes": attributes})

    def control(action):
        connection = http.client.HTTPConnection("127.0.0.1", settings.pkce_port + 1, timeout=300)
        try:
            connection.request("POST", "/" + action,
                               headers={"X-Smoke-Control": settings.secret("pricing-smoke-control-key")})
            response = connection.getresponse()
            raw = response.read(2_000_000)
            require(response.status == 200, "Isolated Pricing control operation: " + action)
            return json.loads(raw)
        except (OSError, http.client.HTTPException, ValueError):
            raise SecurityError("Isolated Pricing control connection failed: " + action) from None
        finally:
            connection.close()

    def observations():
        connection = http.client.HTTPConnection("127.0.0.1", pricing.port + 1, timeout=5)
        try:
            connection.request("GET", "/_checks/observations")
            response = connection.getresponse()
            require(response.status == 200, "Inventory observation retrieval")
            return json.loads(response.read(2_000_000))
        finally:
            connection.close()

    def price(serial, value=125.50):
        return {"serialNumber": serial, "price": value, "weekendRate": 1.25,
                "longRentalCondition": 7, "longRentalDiscount": 0.1, "deposit": 300}

    def create(serial, token=None):
        return pricing.expect(201, "POST", path, token or staff, price(serial))

    def downstream_failure(expected, serial):
        before = control("data-state")
        headers, body = pricing.expect(expected, "POST", path, staff, price(serial))
        require("www-authenticate" not in headers, "Dependency failure is not a customer authentication challenge")
        require(body["code"] == ("INVENTORY_SERVICE_ERROR" if expected == 502 else "INVENTORY_SERVICE_UNAVAILABLE"),
                "Safe downstream security failure category")
        require(body["detail"] == ("Inventory service returned an unexpected response." if expected == 502
                                   else "Inventory service is temporarily unavailable."), "Sanitized dependency failure detail")
        require(control("data-state") == before, "Failed outbound authentication preserves PostgreSQL rows and warm cache values")

    def restore_policy():
        Policy(maintenance(settings), desired(settings), apply=True).run()

    def refresh_staff():
        nonlocal staff
        staff = renew("staff")["access_token"]

    serials = ["PRICING-C1-BASE", "PRICING-C1-SECOND", "PRICING-C1-RENEW", "PRICING-C1-WRONG-AUD",
               "PRICING-C1-FORBIDDEN", "PRICING-C1-BAD-SECRET", "PRICING-C1-CACHED-OUTAGE", "PRICING-C1-EXPIRED-OUTAGE"]
    try:
        for serial in serials:
            inventory.expect(201, "POST", "/api/v1/inventory", inventory_staff,
                             {"serialNumber": serial, "type": "Pricing security", "name": "Isolated C1 equipment", "status": "AVAILABLE"})
        pricing.expect(200, "GET", "/readyz")
        pricing.expect(200, "GET", "/livez")
        for suffix in ("", "/", "/api/v1/pricing/PRICING%2DC1"):
            _, metadata = pricing.expect(200, "GET", "/.well-known/oauth-protected-resource" + suffix + "?ignored=true", "invalid")
            require(metadata == {"resource": f"http://127.0.0.1:{pricing.port}" + suffix,
                                 "authorization_servers": [settings.issuer], "bearer_methods_supported": ["header"],
                                 "tls_client_certificate_bound_access_tokens": False}, "Pricing native public metadata")
        pricing.expect(401, "GET", path)
        for token in ("invalid", inventory_staff, pkce["staff"]["id_token"], pkce["staff"]["refresh_token"]):
            pricing.expect(401, "GET", path, token)
        # Genuine Pricing image acquires its first machine token; the observer forwards to real Inventory.
        create(serials[0], swagger["staff"]["access_token"])
        first = observations()[-1]
        require(first["pricing_identity"] and first["exact_grants"] and first["status"] == 200 and not first["user_headers"],
                "Actual Pricing image calls Inventory as pricing-service with exactly inventory:read")
        human_digests = {hashlib.sha256(tokens["access_token"].encode()).hexdigest()
                         for source in (pkce, swagger, inventory_tokens) for tokens in source.values()}
        require(first["digest"] not in human_digests, "Incoming human bearer token is never forwarded")

        # Temporarily elevate only an isolated customer fixture to test the application-wide token cache.
        customer = admin.admin("GET", "/users?" + urlencode({"username": "customer-b", "exact": "true"}))[0]
        require(customer.get("attributes", {}).get("rentflow.managed") == ["true"], "Managed isolated customer fixture")
        admin.admin("POST", f"/users/{customer['id']}/role-mappings/realm", [admin.admin("GET", "/roles/STAFF")])
        try:
            create(serials[1], renew("customer-b")["access_token"])
        finally:
            restore_policy()
        require(observations()[-1]["digest"] == first["digest"], "Machine token cache is shared across human subjects and clients")

        for user in ("customer-a", "customer-b"):
            for source in (pkce, swagger):
                token = source[user]["access_token"]
                pricing.expect(200, "GET", path, token)
                pricing.expect(200, "GET", path + "/" + serials[0], token)
                pricing.expect(200, "GET", path + "/" + serials[0] + "/cached", token)
                _, batch = pricing.expect(200, "POST", path + "/batch/cached", token, [serials[0], serials[1], serials[0]])
                require({row["serialNumber"] for row in batch} == set(serials[:2]) and len(batch) == 2,
                        "Customer cached batch retains deduplication and records")
                before = control("data-state")
                pricing.expect(403, "POST", path, token, price("PRICING-DENIED"))
                pricing.expect(403, "PUT", path + "/" + serials[0], token, price(serials[0], 999))
                pricing.expect(403, "DELETE", path + "/" + serials[0], token)
                require(control("data-state") == before, "Denied customer writes preserve database and cache")
        before_count = len(observations())
        pricing.expect(200, "PUT", path + "/" + serials[0], staff, price(serials[0], 150))
        require(pricing.expect(200, "GET", path + "/" + serials[0] + "/cached", staff)[1]["price"] == 150,
                "Staff committed replacement evicts and reloads cache")
        pricing.expect(200, "POST", path + "/batch/cached", staff, serials[:2])
        require(len(observations()) == before_count, "Local read/batch/list/replace paths do not call Inventory")
        for method in ("HEAD", "OPTIONS", "PATCH"):
            pricing.expect(403, method, path, staff)
        pricing.expect(403, "GET", "/actuator/prometheus", staff)
        machine = Api(settings).token("pricing-service", settings.secret("pricing-service-client-secret"))
        pricing.expect(401, "GET", path, machine)

        # Newly issued reduced token cannot read an existing warm single or batch cache entry.
        user = admin.admin("GET", "/users?" + urlencode({"username": "staff", "exact": "true"}))[0]
        admin.admin("DELETE", f"/users/{user['id']}/role-mappings/realm", [admin.admin("GET", "/roles/STAFF")])
        admin.admin("POST", f"/users/{user['id']}/role-mappings/clients/{admin.client('pricing-api')['id']}",
                    [admin.admin("GET", f"/clients/{admin.client('pricing-api')['id']}/roles/pricing%3Alist")])
        try:
            reduced = renew("staff")["access_token"]
            before = control("data-state")
            pricing.expect(200, "GET", path, reduced)
            pricing.expect(403, "GET", path + "/" + serials[0] + "/cached", reduced)
            pricing.expect(403, "POST", path + "/batch/cached", reduced, serials[:2])
            require(control("data-state") == before, "Warm cache cannot bypass reduced permissions")
        finally:
            restore_policy()
        refresh_staff()

        wait = max(0, first["expires_at"] - 30 - time.time() + 2)
        require(wait <= 50, "Bounded isolated renewal wait")
        time.sleep(wait)
        create(serials[2])
        require(observations()[-1]["digest"] != first["digest"], "Actual Pricing token renews at the 30-second skew boundary")
        print("Pricing customer/staff matrix, cache authorization, actual machine identity and token reuse/renewal passed")

        scope = next(s for s in admin.admin("GET", "/client-scopes") if s["name"] == "pricing-inventory-access")
        mapper = next(m for m in admin.admin("GET", f"/client-scopes/{scope['id']}/protocol-mappers/models")
                      if m["name"] == "inventory-api-audience")
        mapper["config"]["included.client.audience"] = "pricing-api"
        admin.admin("PUT", f"/client-scopes/{scope['id']}/protocol-mappers/models/{mapper['id']}", mapper)
        control("restart-pricing")
        count = len(observations())
        downstream_failure(502, serials[3])
        require(len(observations()) == count + 1 and observations()[-1]["status"] == 401, "Wrong audience fails once at actual Inventory")
        restore_policy()
        create(serials[3])
        require(observations()[-1]["status"] == 200, "Inventory 401 evicts; next create obtains corrected token")

        resource = admin.client("inventory-api")
        role = admin.admin("GET", f"/clients/{resource['id']}/roles/inventory%3Aread")
        account = admin.admin("GET", f"/clients/{client['id']}/service-account-user")
        admin.admin("DELETE", f"/users/{account['id']}/role-mappings/clients/{resource['id']}", [role])
        admin.admin("DELETE", f"/client-scopes/{scope['id']}/scope-mappings/clients/{resource['id']}", [role])
        control("restart-pricing")
        downstream_failure(502, serials[4])
        forbidden = observations()[-1]
        require(forbidden["status"] == 403, "Insufficient Inventory permission yields 403")
        restore_policy()
        downstream_failure(502, serials[4])
        require(observations()[-1]["digest"] == forbidden["digest"] and observations()[-1]["status"] == 403,
                "Inventory 403 retains the original token without renewal")
        control("restart-pricing")
        create(serials[4])

        control("bad-pricing-secret")
        count = len(observations())
        downstream_failure(502, serials[5])
        require(len(observations()) == count, "Rejected client credentials make no Inventory request")
        control("restart-pricing")
        create(serials[5])

        # Inbound keys and a valid machine token are warm before the real provider is stopped.
        cached = observations()[-1]
        control("stop-identity")
        try:
            create(serials[6])
            require(observations()[-1]["digest"] == cached["digest"], "Cached machine token works during provider outage")
            wait = max(0, cached["expires_at"] - 30 - time.time() + 2)
            require(wait <= 50, "Bounded isolated outage renewal wait")
            time.sleep(wait)
            count = len(observations())
            downstream_failure(503, serials[7])
            require(len(observations()) == count, "Unavailable provider prevents Inventory call when token renewal is needed")
            pricing.expect(200, "GET", path + "/" + serials[0] + "/cached", staff)
            pricing.expect(200, "GET", "/readyz")
        finally:
            control("start-identity")
        refresh_staff()
        create(serials[7])
        pricing.expect(204, "DELETE", path + "/" + serials[1], staff)
        print("Pricing real-provider wrong-audience, insufficient-permission, invalid-secret and outage/recovery checks passed")
    finally:
        control("start-identity")
        restore_policy()
        maintenance(settings).admin("PUT", "/clients/" + client["id"], {"attributes": original_attributes})
        control("restart-pricing")
