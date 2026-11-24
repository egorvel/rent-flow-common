"""Real-provider Reservation ownership, scoped replay and machine failure acceptance."""

import hashlib
import http.client
import json
import struct
import time
from urllib.parse import urlencode
import uuid

from common import SecurityError, maintenance, require
from inventory_check import Inventory
from policy import Policy, desired


class Application(Inventory):
    def __init__(self, settings, resource, user="staff", client="rentflow-pkce-check"):
        self.settings, self.resource, self.user, self.client = settings, resource, user, client
        self.name = resource.title()
        self.port = int(settings.values[resource + "_port"])

    def request(self, method, path, token=None, data=None, key=None):
        headers = {"Accept": "application/json", "X-Smoke-Control": self.settings.secret("smoke-proxy-key"),
                   "X-Smoke-User": self.user, "X-Smoke-Client": self.client,
                   "X-User-ID": "untrusted-smoke-caller"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        body = None
        if data is not None:
            body = json.dumps(data)
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.settings.pkce_port + 2, timeout=45)
        try:
            connection.request(method, f"http://127.0.0.1:{self.port}" + path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(2_000_000)
            return response.status, {k.lower(): v for k, v in response.getheaders()}, json.loads(raw) if raw else None
        except (OSError, http.client.HTTPException, ValueError):
            raise SecurityError("Authenticated smoke application request failed") from None
        finally:
            connection.close()


def downstream_key(identity, external):
    def digest(values):
        return hashlib.sha256(b"".join(struct.pack(">I", len(v.encode())) + v.encode() for v in values)).digest()
    scope = "p1:" + digest(["reservation-creation:principal:v1", identity["iss"], identity["sub"]]).hex()
    raw = bytearray(digest(["reservation-creation:inventory-key:v1", scope, external])[:16])
    raw[6], raw[8] = (raw[6] & 15) | 64, (raw[8] & 63) | 128
    return str(uuid.UUID(bytes=bytes(raw)))


def verify_reservation(settings):
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-common-smoke",
            "Reservation tests require the combined isolated project")
    path = "/api/v1/reservations"
    inventory = Application(settings, "inventory")
    a = Application(settings, "reservation", "customer-a")
    b = Application(settings, "reservation", "customer-b")
    staff = Application(settings, "reservation")
    swagger_a = Application(settings, "reservation", "customer-a", "reservation-swagger")
    admin = maintenance(settings)
    client = admin.client("reservation-service")
    original_attributes = dict(client.get("attributes", {}))
    admin.admin("PUT", "/clients/" + client["id"], {"attributes": dict(original_attributes, **{"access.token.lifespan": "45"})})

    def helper(port, route, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
        try:
            connection.request(method, route, headers={"X-Smoke-Control": settings.secret("smoke-proxy-key")})
            response = connection.getresponse()
            require(response.status == 200, "Reservation smoke helper operation: " + route.split("?")[0])
            return json.loads(response.read(4_000_000))
        finally:
            connection.close()

    def control(action):
        return helper(settings.pkce_port + 1, "/" + action)

    def identity(user, forget=False):
        return helper(settings.pkce_port + 2, "/_smoke/" + ("forget" if forget else "identity") + "?" +
                      urlencode({"user": user, "resource": "reservation"}), "GET")

    def observations():
        return helper(int(settings.values["reservation_port"]) + 1, "/_checks/observations", "GET")

    def payload(serial):
        return {"orderId": "C1-SAME-OPAQUE-ORDER", "items": [
            {"serialNumber": serial, "startDate": "9999-12-30", "endDate": "9999-12-31"}]}

    def create(caller, serial, key):
        headers, rows = caller.expect(201, "POST", path, data=payload(serial), key=key)
        require(len(rows) == 1 and rows[0]["customerId"] == identity(caller.user)["sub"]
                and "customerIssuer" not in rows[0], "Creation uses verified subject and never exposes issuer")
        return headers, rows[0]

    def replay_failure(status, caller, serial, key):
        headers, _ = caller.expect(status, "POST", path, data=payload(serial), key=key)
        require("idempotency-replayed" not in headers and "location" not in headers, "Denied replay exposes no stored result headers")

    def dependency_failure(status, serial, key):
        before = control("data-state")
        headers, body = a.expect(status, "POST", path, data=payload(serial), key=key)
        require("www-authenticate" not in headers and "idempotency-replayed" not in headers, "Dependency errors carry no customer challenge or saved outcome")
        require(body["code"] == ("INVENTORY_SERVICE_ERROR" if status == 502 else "INVENTORY_SERVICE_UNAVAILABLE") and
                body["detail"] == ("Inventory returned an unexpected response." if status == 502 else
                                    "Inventory is temporarily unavailable; retry this request with the same key."), "Sanitized Reservation dependency failure")
        require(control("data-state") == before, "Failed claim preserves reservation, replay and logical outbox rows")

    def restore():
        Policy(maintenance(settings), desired(settings), apply=True).run()

    serials = ["C1-OWNER-A", "C1-OWNER-B", "C1-STAFF", "C1-RENEW", "C1-WRONG-AUD", "C1-NO-GRANT",
               "C1-BAD-SECRET", "C1-CACHED-OUTAGE", "C1-EXPIRED-OUTAGE"]
    shared = str(uuid.uuid4())
    try:
        plain = Inventory(settings)
        plain.port, plain.name = staff.port, "Reservation"
        plain.expect(401, "GET", path)
        plain.expect(401, "GET", path, "invalid")
        require(control("data-state")["no_mapping"], "No identity mapping table exists")
        for caller in (a, b):
            require(caller.expect(200, "GET", path)[1]["page"]["totalElements"] == 0, "New customer can list without provisioning")
        for serial in serials:
            inventory.expect(201, "POST", "/api/v1/inventory", data={"serialNumber": serial,
                "type": "Reservation security", "name": "Isolated ownership fixture", "status": "AVAILABLE"})
        _, row_a = create(a, serials[0], shared)
        _, row_b = create(b, serials[1], shared)
        _, row_s = create(staff, serials[2], shared)
        first = observations()[:3]
        require(len({record["digest"] for record in first}) == 1 and all(record["reservation_identity"] and
                record["reservation_grants"] and record["all_reserved"] and not record["user_headers"] for record in first),
                "Actual Reservation shares only its reserve-only machine token across customers/staff")
        expected_keys = [downstream_key(identity(caller.user), shared) for caller in (a, b, staff)]
        require([record["key"] for record in first] == expected_keys and len(set(expected_keys)) == 3,
                "Derived Inventory keys bind issuer/subject and isolate equal external UUIDs")
        headers, replay = create(swagger_a, serials[0], shared)
        require(headers["idempotency-replayed"] == "true" and replay == row_a, "Same subject replays across approved clients")
        for caller in (a, b):
            page = caller.expect(200, "GET", path)[1]
            require(page["page"]["totalElements"] == 1, "Own page totals do not reveal another customer")
        require(staff.expect(200, "GET", path)[1]["page"]["totalElements"] == 3, "Explicit staff any-list sees all customers")
        before = control("data-state")
        for foreign in (row_b["id"], str(uuid.uuid4())):
            a.expect(404, "GET", path + "/" + foreign)
            a.expect(404, "POST", path + "/" + foreign + "/cancel")
        a.expect(403, "PUT", path + "/" + row_a["id"], data={})
        a.expect(403, "DELETE", path + "/" + row_a["id"])
        staff.expect(400, "POST", path, data=dict(payload(serials[3]), customerId="untrusted-owner"), key=str(uuid.uuid4()))
        require(control("data-state") == before, "Foreign access and owner-input denial preserve business/replay/outbox state")
        replacement = {"serialNumber": serials[1], "orderId": "C1-STAFF-EDIT", "startDate": "9999-12-30",
                       "endDate": "9999-12-31", "status": "CONFIRMED"}
        admin_outbox = control("data-state")["outbox"]
        staff.expect(400, "PUT", path + "/" + row_b["id"], data=dict(replacement, customerId="untrusted-owner"))
        _, updated = staff.expect(200, "PUT", path + "/" + row_b["id"], data=replacement)
        require(all(updated[field] == row_b[field] for field in ("customerId", "timestamp", "holdExpiresAt")),
                "Staff replacement preserves original owner, creation time and hold deadline")
        require(control("data-state")["outbox"] == admin_outbox, "Administrative replacement emits no release event")
        b.expect(200, "GET", path + "/" + row_b["id"])
        a.expect(404, "GET", path + "/" + row_b["id"])
        staff.expect(204, "POST", path + "/" + row_b["id"] + "/cancel")
        require(b.expect(200, "GET", path + "/" + row_b["id"])[1]["status"] == "CANCELLED", "Staff any-cancel retains customer ownership")
        a.expect(204, "POST", path + "/" + row_a["id"] + "/cancel")
        for _ in range(60):
            if inventory.expect(200, "GET", "/api/v1/inventory/" + serials[0])[1]["status"] == "AVAILABLE":
                break
            time.sleep(1)
        else:
            raise SecurityError("Real cancellation outbox/Kafka release did not complete")
        _, replay = create(a, serials[0], shared)
        require(replay == row_a, "Cancelled owned row replays its original creation snapshot")
        admin_outbox = control("data-state")["outbox"]
        staff.expect(204, "DELETE", path + "/" + row_s["id"])
        require(control("data-state")["outbox"] == admin_outbox and
                inventory.expect(200, "GET", "/api/v1/inventory/" + serials[2])[1]["status"] == "RESERVED",
                "Administrative deletion emits no release and retains the Inventory claim")
        replay_failure(404, staff, serials[2], shared)

        user = admin.admin("GET", "/users?" + urlencode({"username": "customer-a", "exact": "true"}))[0]
        require(user.get("attributes", {}).get("rentflow.managed") == ["true"], "Only managed smoke fixture grants may change")
        admin.admin("DELETE", f"/users/{user['id']}/role-mappings/realm", [admin.admin("GET", "/roles/CUSTOMER")])
        resource = admin.client("reservation-api")
        admin.admin("POST", f"/users/{user['id']}/role-mappings/clients/{resource['id']}",
                    [admin.admin("GET", f"/clients/{resource['id']}/roles/reservation%3A" + role) for role in ("read", "list", "cancel")])
        try:
            identity("customer-a", forget=True)
            before = control("data-state")
            replay_failure(403, a, serials[0], shared)
            a.expect(200, "GET", path + "/" + row_a["id"])
            require(control("data-state") == before, "Reduced create permission denies saved replay before access")
        finally:
            restore()
            identity("customer-a", forget=True)
        wait = max(0, first[0]["expires_at"] - 30 - time.time() + 2)
        require(wait <= 50, "Bounded machine renewal wait")
        time.sleep(wait)
        create(a, serials[3], str(uuid.uuid4()))
        require(observations()[-1]["digest"] != first[0]["digest"], "Actual Reservation token renewal uses the 30-second boundary")
        print("Reservation token-derived owners, scoped pages/replay, immutable staff updates and machine key/renewal checks passed", flush=True)

        scope = next(s for s in admin.admin("GET", "/client-scopes") if s["name"] == "reservation-inventory-access")
        mapper = next(m for m in admin.admin("GET", f"/client-scopes/{scope['id']}/protocol-mappers/models") if m["name"] == "inventory-api-audience")
        mapper["config"]["included.client.audience"] = "pricing-api"
        admin.admin("PUT", f"/client-scopes/{scope['id']}/protocol-mappers/models/{mapper['id']}", mapper)
        control("restart-reservation")
        key = str(uuid.uuid4())
        count = len(observations())
        dependency_failure(502, serials[4], key)
        require(len(observations()) == count + 1 and observations()[-1]["status"] == 401, "Wrong audience fails once at secured Inventory")
        restore()
        create(a, serials[4], key)
        require(observations()[-1]["status"] == 204, "Inventory 401 evicts and later explicit retry reacquires")
        inventory_client = admin.client("inventory-api")
        role = admin.admin("GET", f"/clients/{inventory_client['id']}/roles/inventory%3Areserve")
        account = admin.admin("GET", f"/clients/{client['id']}/service-account-user")
        admin.admin("DELETE", f"/users/{account['id']}/role-mappings/clients/{inventory_client['id']}", [role])
        admin.admin("DELETE", f"/client-scopes/{scope['id']}/scope-mappings/clients/{inventory_client['id']}", [role])
        control("restart-reservation")
        key = str(uuid.uuid4())
        dependency_failure(502, serials[5], key)
        forbidden = observations()[-1]
        require(forbidden["status"] == 403, "Valid Inventory audience without reserve permission is denied")
        restore()
        dependency_failure(502, serials[5], key)
        require(observations()[-1]["digest"] == forbidden["digest"], "Inventory 403 retains cached Reservation token")
        control("restart-reservation")
        create(a, serials[5], key)
        control("bad-reservation-secret")
        key = str(uuid.uuid4())
        count = len(observations())
        dependency_failure(502, serials[6], key)
        require(len(observations()) == count, "Rejected Reservation credential makes no claim")
        control("restart-reservation")
        create(a, serials[6], key)
        cached = observations()[-1]
        identity("customer-a", forget=True)
        control("stop-identity")
        try:
            create(a, serials[7], str(uuid.uuid4()))
            require(observations()[-1]["digest"] == cached["digest"], "Cached Reservation machine token survives provider outage")
            wait = max(0, cached["expires_at"] - 30 - time.time() + 2)
            require(wait <= 50, "Bounded outage renewal wait")
            time.sleep(wait)
            key = str(uuid.uuid4())
            count = len(observations())
            dependency_failure(503, serials[8], key)
            require(len(observations()) == count, "Expired machine token fails closed without a claim during outage")
            a.expect(200, "GET", path + "/" + row_a["id"])
            a.expect(200, "GET", "/readyz")
        finally:
            control("start-identity")
        create(a, serials[8], key)
        print("Reservation real-provider wrong-audience, permission, recipient and outage/recovery checks passed", flush=True)
    finally:
        control("start-identity")
        restore()
        maintenance(settings).admin("PUT", "/clients/" + client["id"], {"attributes": original_attributes})
        control("restart-reservation")


if __name__ == "__main__":
    from check import settings
    try:
        verify_reservation(settings())
    except Exception as error:
        print("ERROR: " + (str(error) if isinstance(error, SecurityError) else type(error).__name__), flush=True)
        raise SystemExit(1)
