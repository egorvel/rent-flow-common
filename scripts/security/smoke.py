#!/usr/bin/env python3
"""Destructive checks run only in an explicitly isolated smoke project/realm."""

import hashlib
import json
import subprocess
import sys
from urllib.parse import urlencode

from common import Api, Local, ROOT, SecurityError, maintenance, require
from manage import ready
from policy import Policy, desired, realm_import


def command(*args):
    result = subprocess.run([str(ROOT / "scripts/local-keycloak.sh"), *args])
    require(result.returncode == 0, "Smoke lifecycle operation failed: " + args[0])


def snapshot(settings):
    api = maintenance(settings)
    tooling = desired(settings)["_rentflow"]
    users = {}
    for definition in [tooling["operator"], *tooling["fixtures"]]:
        username = definition["username"]
        user = api.admin("GET", "/users?" + urlencode({"username": username, "exact": "true"}))[0]
        users[username] = user["id"]
    status, jwks = api.request("GET", f"/realms/{settings.realm}/protocol/openid-connect/certs")
    require(status == 200, "Persistent key retrieval")
    files = ["keycloak-db-password"]
    files += [secret["file"] for secret in tooling["clientSecrets"].values()]
    files += [user["username"] + "-password" for user in [tooling["operator"], *tooling["fixtures"]]]
    owned_clients = {client["clientId"] for client in desired(settings)["clients"]}
    clients = {client["clientId"]: client["id"] for client in api.admin("GET", "/clients")
               if client["clientId"] in owned_clients}
    return {"users": users, "clients": clients, "keys": sorted(k["kid"] for k in jwks["keys"]),
            "credentials": {n: hashlib.sha256(settings.secret(n).encode()).hexdigest() for n in files}}


def isolation(local):
    result = local.compose("exec", "--no-TTY", "rentflow-keycloak-postgres", "psql", "--username",
                           "rentflow_identity_admin", "--dbname", "postgres", "--tuples-only", "--no-align",
                           "--command", "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname='rentflow_keycloak'",
                           capture=True)
    require(result.stdout.strip() == "f", "Unprivileged identity runtime DB account")
    result = local.compose("exec", "--no-TTY", "rentflow-keycloak-postgres", "psql", "--username",
                           "rentflow_identity_admin", "--dbname", "postgres", "--tuples-only", "--no-align",
                           "--command", "SELECT count(*) FROM pg_roles WHERE rolname IN ('inventory','pricing','reservation')",
                           capture=True)
    require(result.stdout.strip() == "0", "No application users in identity database")
    # Bounded failed attempts: passwords are deliberately invalid public test values, not actual secrets.
    for username in ("inventory", "pricing", "reservation"):
        result = local.compose("exec", "--no-TTY", "--env", "PGPASSWORD=invalid-smoke-password",
                               "rentflow-keycloak-postgres", "psql", "--host", "127.0.0.1", "--username", username,
                               "--dbname", "rentflow_keycloak", "--command", "SELECT 1", capture=True, check=False)
        require(result.returncode != 0, "Application account cannot connect to identity database")
    container_id = local.compose("ps", "--quiet", "rentflow-keycloak-postgres", capture=True).stdout.strip()
    inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=True)
    data = json.loads(inspect.stdout)[0]
    require(not data["HostConfig"].get("PortBindings"), "Identity database has no host ports")
    require(set(data["NetworkSettings"]["Networks"]) == {local.settings.values["project"] + "_identity-db"},
            "Identity database only on private network")
    container_id = local.compose("ps", "--quiet", "rentflow-keycloak", capture=True).stdout.strip()
    inspect = subprocess.run(["docker", "inspect", container_id], capture_output=True, text=True, check=True)
    mounts = json.loads(inspect.stdout)[0]["Mounts"]
    require(not any(m["Destination"] == "/run/secrets/keycloak_bootstrap_secret" for m in mounts),
            "Normal Keycloak has no bootstrap credential mount")
    print("Identity database, runtime privileges and bootstrap mount isolation passed")


def declarative_policy(settings):
    """A different template declaration must change grants, not require a Python branch."""
    api = maintenance(settings)
    baseline = desired(settings)
    changed = json.loads(json.dumps(baseline))
    permission = "smoke:catalogue"
    changed["roles"]["client"]["inventory-api"].append({"name": permission, "composite": False})
    role = next(role for role in changed["roles"]["realm"] if role["name"] == "STAFF")
    role.update({"composite": True, "composites": {"realm": ["CUSTOMER"], "client": {"inventory-api": [permission]}}})
    pricing = next(user for user in changed["users"] if user["serviceAccountClientId"] == "pricing-service")
    pricing["clientRoles"] = {"inventory-api": [permission]}
    mapping = next(m for m in changed["clientScopeMappings"]["inventory-api"]
                   if m.get("clientScope") == "pricing-inventory-access")
    mapping["roles"] = [permission]
    changed["scopeMappings"] = [{"client": "pricing-service", "roles": ["STAFF"]}]
    changed["defaultOptionalClientScopes"] = ["pricing-access"]
    changed["attributes"]["rentflow.smoke.declaration"] = "present"
    changed["_rentflow"]["userProfile"]["unmanagedAttributePolicy"] = "ENABLED"
    fixture = changed["_rentflow"]["fixtures"][0]
    fixture["realmRoles"] = ["STAFF"]
    fixture["clientRoles"] = {"inventory-api": [permission]}
    before = snapshot(settings)
    require(bool(Policy(api, changed).run()), "Plan supports a newly declared permission")
    Policy(api, changed, apply=True).run()
    require(not Policy(api, changed).run(), "Changed template converges without hardcoded grants")
    resource = api.client("inventory-api")
    client = api.client("pricing-service")
    user = api.admin("GET", f"/clients/{client['id']}/service-account-user")
    grants = api.admin("GET", f"/users/{user['id']}/role-mappings/clients/{resource['id']}")
    require({role["name"] for role in grants} == {permission}, "Service-account grant comes from template users")
    scope = next(scope for scope in api.admin("GET", "/client-scopes") if scope["name"] == "pricing-inventory-access")
    grants = api.admin("GET", f"/client-scopes/{scope['id']}/scope-mappings/clients/{resource['id']}")
    require({role["name"] for role in grants} == {permission}, "Client-role scope mapping comes from template")
    grants = api.admin("GET", f"/clients/{client['id']}/scope-mappings/realm")
    require({role["name"] for role in grants} == {"STAFF"}, "Realm-role scope mapping comes from template")
    defaults = api.admin("GET", "/default-optional-client-scopes")
    require({scope["name"] for scope in defaults} == {"pricing-access"}, "Realm default scopes come from template")
    composites = api.admin("GET", "/roles/STAFF/composites")
    require({role["name"] for role in composites} == {"CUSTOMER", permission}, "Role composites come from template")
    user = api.admin("GET", "/users?" + urlencode({"username": fixture["username"], "exact": "true"}))[0]
    grants = api.admin("GET", f"/users/{user['id']}/role-mappings/realm")
    require({role["name"] for role in grants} == {"STAFF"}, "Existing local-user roles come from template")
    require(api.admin("GET", "/users/profile")["unmanagedAttributePolicy"] == "ENABLED", "Profile comes from template")
    require(api.admin("GET")["attributes"]["rentflow.smoke.declaration"] == "present", "Realm metadata comes from template")
    require(snapshot(settings) == before, "Declarative changes preserve identities, keys and credentials")
    Policy(api, baseline, apply=True).run()
    require(not Policy(api, baseline).run(), "Baseline declarations restore all grants")
    api.admin("DELETE", f"/clients/{resource['id']}/roles/" + permission)
    print("Template-driven role grants, scope mappings, composites, defaults, profile and local users passed")


def pricing_policy_update(settings):
    """Upgrade an existing Inventory-enabled realm without disturbing its grants or identities."""
    api = maintenance(settings)
    before = snapshot(settings)
    previous = desired(settings)
    for role in previous["roles"]["realm"]:
        role["composites"]["client"].pop("pricing-api")
    previous["clientScopeMappings"].pop("pricing-api")
    Policy(api, previous, apply=True).run()
    resource = api.client("pricing-api")
    for name in ("pricing:read", "pricing:list", "pricing:manage"):
        api.admin("DELETE", f"/clients/{resource['id']}/roles/" + name)
    require(bool(Policy(api, desired(settings)).run()), "Existing realm reports missing Pricing policy")
    command("policy", "apply")
    require(not Policy(api, desired(settings)).run(), "Pricing policy reapply is a no-op")
    require(snapshot(settings) == before, "Pricing policy update preserves subjects, clients, keys and credentials")
    print("Existing Inventory-enabled realm upgraded to Pricing policy without identity or credential changes")


def reservation_policy_update(settings):
    api = maintenance(settings)
    before = snapshot(settings)
    previous = desired(settings)
    for role in previous["roles"]["realm"]:
        role["composites"]["client"].pop("reservation-api")
    previous["clientScopeMappings"].pop("reservation-api")
    Policy(api, previous, apply=True).run()
    resource = api.client("reservation-api")
    for role in previous["roles"]["client"]["reservation-api"]:
        api.admin("DELETE", f"/clients/{resource['id']}/roles/" + role["name"])
    require(bool(Policy(api, desired(settings)).run()), "Existing realm detects missing Reservation policy")
    command("policy", "apply")
    require(not Policy(api, desired(settings)).run(), "Reservation second apply is a no-op")
    require(snapshot(settings) == before, "Reservation policy upgrade preserves identities, keys and source credentials")
    print("Existing Pricing-enabled realm upgraded to eight Reservation roles and exact composites")


def main():
    local = Local()
    settings = local.settings
    require(settings.realm == "rentflow-smoke" and settings.values["project"] in
            ("rentflow-common-smoke", "rentflow-security-smoke"), "Refusing mutation tests outside smoke projects")
    command("init")
    rendered = json.loads((settings.state / "runtime/import" / (settings.realm + "-realm.json")).read_text())
    require(rendered == realm_import(desired(settings)) and "_rentflow" not in rendered,
            "Only native realm fields are sent to startup import")
    command("bootstrap")
    for user in desired(settings)["_rentflow"]["fixtures"]:
        require(not maintenance(settings).admin("GET", "/users?" + urlencode({"username": user["username"], "exact": "true"})),
                "Optional fixtures are not created by bootstrap/import")
    command("fixtures", "apply")
    before = snapshot(settings)
    command("init")
    command("bootstrap")
    command("fixtures", "apply")
    require(snapshot(settings) == before, "Repeated provisioning preserves identities, keys and credentials")
    local.runner("browser")
    local.runner(network=True)
    isolation(local)

    print("Testing existing-realm import skip and explicit policy updates")
    api = maintenance(settings)
    # Reproduce the pre-Inventory policy on this isolated existing realm. Updating it must
    # create the new roles/composites/mapping while retaining all subjects and credentials.
    legacy = desired(settings)
    for role in legacy["roles"]["realm"]:
        role["composite"] = False
        role.pop("composites", None)
    legacy["clientScopeMappings"]["inventory-api"] = [
        mapping for mapping in legacy["clientScopeMappings"]["inventory-api"]
        if mapping["clientScope"] != "inventory-access"]
    Policy(api, legacy, apply=True).run()
    resource = api.client("inventory-api")
    for permission in ("inventory:list", "inventory:manage", "inventory:history"):
        api.admin("DELETE", f"/clients/{resource['id']}/roles/" + permission)
    api.admin("POST", "/clients", {"clientId": "unrelated-smoke-client", "enabled": True,
                                   "standardFlowEnabled": False, "publicClient": True})
    unrelated_id = api.client("unrelated-smoke-client")["id"]
    api.admin("PUT", data={"accessTokenLifespan": 240})
    resource = api.client("inventory-api")
    caller = api.client("pricing-service")
    api.admin("PUT", "/clients/" + caller["id"], {"fullScopeAllowed": True})
    account = api.admin("GET", "/clients/" + caller["id"] + "/service-account-user")
    role = api.admin("GET", "/clients/" + resource["id"] + "/roles/inventory%3Areserve")
    api.admin("POST", f"/users/{account['id']}/role-mappings/clients/{resource['id']}", [role])
    scope = next(s for s in api.admin("GET", "/client-scopes") if s["name"] == "pricing-inventory-access")
    api.admin("POST", f"/client-scopes/{scope['id']}/protocol-mappers/models", {
        "name": "obsolete-audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
        "config": {"included.client.audience": "pricing-api", "access.token.claim": "true"}})
    ready(local, recreate=True)
    api = maintenance(settings)
    require(api.admin("GET")["accessTokenLifespan"] == 240, "Startup import skips existing realm")
    require(bool(Policy(api, desired(settings)).run()), "Policy drift is detected")
    command("policy", "apply")
    require(not Policy(api, desired(settings)).run(), "Second policy apply is a no-op")
    require(api.client("unrelated-smoke-client")["id"] == unrelated_id, "Unowned objects preserved")
    require(snapshot(settings) == before, "Policy update preserves identities, signing keys and credentials")
    declarative_policy(settings)
    pricing_policy_update(settings)
    reservation_policy_update(settings)
    local.runner("machine")
    for client, secret in desired(settings)["_rentflow"]["clientSecrets"].items():
        if secret["rotatable"]:
            command("rotate-client", client)
    command("policy", "apply")
    after_rotation = snapshot(settings)

    print("Testing Keycloak/database recreation with persistent identity storage")
    local.compose("stop", "rentflow-keycloak", "rentflow-keycloak-postgres")
    ready(local, recreate=True)
    require(snapshot(settings) == after_rotation, "Recreation preserves subjects, keys and credentials")
    command("fixtures", "apply")
    local.runner("browser")
    local.runner(network=True)
    isolation(local)
    print("Keycloak foundation smoke verification passed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("ERROR: " + (str(error) if isinstance(error, SecurityError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
