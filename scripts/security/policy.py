"""Apply declarations from the realm template without replacing identities or credentials."""

import json
from string import Template
from urllib.parse import quote, urlencode

from common import ROOT, require


def desired(settings):
    substitutions = {"KEYCLOAK_REALM": settings.realm, "KEYCLOAK_PKCE_PORT": settings.pkce_port,
                     "INVENTORY_PORT": settings.values["inventory_port"],
                     "PRICING_PORT": settings.values["pricing_port"],
                     "RESERVATION_PORT": settings.values["reservation_port"]}
    text = (ROOT / "security/keycloak/rentflow-realm.template.json").read_text()
    return json.loads(Template(text).substitute(substitutions))


def realm_import(document):
    """_rentflow contains local-tool declarations, not Keycloak realm-import properties."""
    return {key: value for key, value in document.items() if key != "_rentflow"}


def projection_matches(actual, expected):
    for key, value in expected.items():
        if isinstance(value, dict):
            if not projection_matches(actual.get(key, {}), value):
                return False
        elif actual.get(key) != value:
            return False
    return True


class Policy:
    def __init__(self, api, document, apply=False):
        self.api = api
        self.document = document
        self.apply = apply
        self.changes = []

    def change(self, label, operation):
        self.changes.append(label)
        if self.apply:
            operation()

    def role(self, client, name):
        base = f"/clients/{client}/roles" if client else "/roles"
        return self.api.admin("GET", base + "/" + quote(name, safe=""))

    def client_grants(self, mapping, clients):
        grants = {}
        for name, roles in mapping.items():
            if name in clients:
                grants[clients[name]["id"]] = roles
            else:
                require(not self.apply and any(c["clientId"] == name for c in self.document["clients"]),
                        f"Unknown role-owning client: {name}")
        return grants

    def sync_grants(self, base, grants, realm_roles=()):
        """Reconcile direct assignments; role lookup is deferred so plan can show new roles."""
        current = self.api.admin("GET", base)
        for mapping in current.get("clientMappings", {}).values():
            excess = [role for role in mapping["mappings"]
                      if role["name"] not in grants.get(mapping["id"], [])]
            if excess:
                self.change(f"remove excess client roles at {base}",
                            lambda m=mapping, e=excess: self.api.admin("DELETE", base + "/clients/" + m["id"], e))
        unwanted = [role for role in current.get("realmMappings", []) if role["name"] not in realm_roles]
        if unwanted:
            self.change(f"remove excess realm roles at {base}",
                        lambda: self.api.admin("DELETE", base + "/realm", unwanted))
        present_realm = {role["name"] for role in current.get("realmMappings", [])}
        missing_realm = set(realm_roles) - present_realm
        if missing_realm:
            self.change(f"add declared realm roles at {base}", lambda:
                        self.api.admin("POST", base + "/realm", [self.role(None, n) for n in sorted(missing_realm)]))
        for client_id, names in grants.items():
            present = {r["name"] for m in current.get("clientMappings", {}).values()
                       if m["id"] == client_id for r in m["mappings"]}
            missing = set(names) - present
            if missing:
                self.change(f"add declared client roles at {base}", lambda c=client_id, names=sorted(missing):
                            self.api.admin("POST", base + "/clients/" + c, [self.role(c, n) for n in names]))

    def scope_grants(self, kind, name, clients):
        realm_roles = set()
        grants = {}
        for mapping in self.document.get("scopeMappings", []):
            if mapping.get(kind) == name:
                realm_roles.update(mapping.get("roles", []))
        for resource, mappings in self.document.get("clientScopeMappings", {}).items():
            roles = {role for mapping in mappings if mapping.get(kind) == name for role in mapping.get("roles", [])}
            if roles:
                grants.update(self.client_grants({resource: sorted(roles)}, clients))
        return grants, sorted(realm_roles)

    def sync_scope_links(self, base, names, scopes, label):
        attached = self.api.admin("GET", base)
        expected = set(names)
        for scope in attached:
            if scope["name"] not in expected:
                self.change(f"detach scope from {label}", lambda i=scope["id"]:
                            self.api.admin("DELETE", base + "/" + i))
        for name in sorted(expected - {scope["name"] for scope in attached}):
            require(name in scopes or (not self.apply and any(s["name"] == name for s in self.document["clientScopes"])),
                    f"Unknown client scope: {name}")
            self.change(f"attach scope {name} to {label}", lambda n=name:
                        self.api.admin("PUT", base + "/" + scopes[n]["id"]))

    def sync_role(self, base, spec, clients):
        current = self.api.admin("GET", base)
        composite = spec.get("composites", {}) if spec.get("composite", False) else {}
        client_roles = self.client_grants(composite.get("client", {}), clients)
        wanted = {(None, name) for name in composite.get("realm", [])}
        wanted.update((client, name) for client, names in client_roles.items() for name in names)
        members = self.api.admin("GET", base + "/composites")

        def identity(role):
            return (role["containerId"] if role.get("clientRole") else None, role["name"])

        excess = [role for role in members if identity(role) not in wanted]
        if excess:
            self.change(f"remove undeclared composites from {spec['name']}",
                        lambda: self.api.admin("DELETE", base + "/composites", excess))
        missing = wanted - {identity(role) for role in members}
        if missing:
            self.change(f"add declared composites to {spec['name']}", lambda:
                        self.api.admin("POST", base + "/composites", [self.role(c, n) for c, n in missing]))
        fields = {key: value for key, value in spec.items() if key != "composites"}
        if not projection_matches(current, fields):
            self.change(f"role settings {spec['name']}", lambda: self.api.admin("PUT", base, fields))

    def sync_user(self, user, spec, clients):
        base = f"/users/{user['id']}"
        fields = {key: value for key, value in spec.items()
                  if key not in {"realmRoles", "clientRoles", "serviceAccountClientId"}}
        if not projection_matches(user, fields):
            update = dict(fields)
            update["attributes"] = dict(user.get("attributes", {}), **fields.get("attributes", {}))
            self.change(f"user settings {spec['username']}", lambda: self.api.admin("PUT", base, update))
        self.sync_grants(base + "/role-mappings", self.client_grants(spec.get("clientRoles", {}), clients),
                         spec.get("realmRoles", []))

    def run(self):
        actual = self.api.admin("GET")
        require(actual["realm"] == self.document["realm"], "Realm target mismatch")
        require(actual.get("attributes", {}).get("rentflow.environment") == self.document["realm"],
                "Realm ownership/environment mismatch")
        tooling = self.document["_rentflow"]
        profile = self.api.admin("GET", "/users/profile")
        if not projection_matches(profile, tooling["userProfile"]):
            profile.update(tooling["userProfile"])
            self.change("user-profile settings", lambda: self.api.admin("PUT", "/users/profile", profile))
        realm_fields = {key: value for key, value in self.document.items()
                        if key not in {"roles", "clients", "clientScopes", "scopeMappings", "clientScopeMappings",
                                       "users", "defaultDefaultClientScopes", "defaultOptionalClientScopes", "_rentflow"}}
        if not projection_matches(actual, realm_fields):
            update = dict(realm_fields)
            update["attributes"] = dict(actual.get("attributes", {}), **realm_fields.get("attributes", {}))
            self.change("realm settings", lambda: self.api.admin("PUT", data=update))

        clients = {client["clientId"]: client for client in self.api.admin("GET", "/clients")}
        for spec in self.document["clients"]:
            name = spec["clientId"]
            if name not in clients:
                self.change(f"create client {name}", lambda s=spec: self.api.admin("POST", "/clients", s))
                if not self.apply:
                    continue
                clients[name] = self.api.client(name)
            representation = self.api.admin("GET", "/clients/" + clients[name]["id"])
            fields = {key: value for key, value in spec.items() if key not in {"defaultClientScopes", "optionalClientScopes"}}
            if not projection_matches(representation, fields):
                update = dict(fields)
                update["attributes"] = dict(representation.get("attributes", {}), **fields.get("attributes", {}))
                self.change(f"client settings {name}", lambda i=clients[name]["id"], s=update:
                            self.api.admin("PUT", "/clients/" + i, s))

        # Create all declared roles before resolving composite or assignment references.
        roles = [(None, role) for role in self.document["roles"].get("realm", [])]
        roles += [(clients[name]["id"], role) for name, definitions in self.document["roles"].get("client", {}).items()
                  if name in clients for role in definitions]
        role_targets = []
        for client, spec in roles:
            base = f"/clients/{client}/roles" if client else "/roles"
            existing = {role["name"] for role in self.api.admin("GET", base)}
            if spec["name"] not in existing:
                self.change(f"create role {spec['name']}", lambda b=base, s=spec:
                            self.api.admin("POST", b, {k: v for k, v in s.items() if k != "composites"}))
                if not self.apply:
                    continue
            role_targets.append((base + "/" + quote(spec["name"], safe=""), spec))
        for base, spec in role_targets:
            self.sync_role(base, spec, clients)

        scopes = {scope["name"]: scope for scope in self.api.admin("GET", "/client-scopes")}
        for spec in self.document["clientScopes"]:
            name = spec["name"]
            if name not in scopes:
                self.change(f"create scope {name}", lambda s=spec: self.api.admin("POST", "/client-scopes", s))
                if not self.apply:
                    continue
                scopes = {scope["name"]: scope for scope in self.api.admin("GET", "/client-scopes")}
            scope_id = scopes[name]["id"]
            fields = {key: value for key, value in spec.items() if key != "protocolMappers"}
            if not projection_matches(scopes[name], fields):
                self.change(f"scope settings {name}", lambda i=scope_id, s=fields:
                            self.api.admin("PUT", "/client-scopes/" + i, s))
            mapper_base = f"/client-scopes/{scope_id}/protocol-mappers/models"
            existing = self.api.admin("GET", mapper_base)
            expected = {mapper["name"]: mapper for mapper in spec["protocolMappers"]}
            for mapper in existing:
                if mapper["name"] not in expected or not projection_matches(mapper, expected[mapper["name"]]):
                    self.change(f"remove obsolete mapper in {name}", lambda b=mapper_base, i=mapper["id"]:
                                self.api.admin("DELETE", b + "/" + i))
            for mapper in spec["protocolMappers"]:
                if not any(projection_matches(m, mapper) for m in existing):
                    self.change(f"install mapper {mapper['name']}", lambda b=mapper_base, m=mapper:
                                self.api.admin("POST", b, m))
            self.sync_grants(f"/client-scopes/{scope_id}/scope-mappings", *self.scope_grants("clientScope", name, clients))

        for kind, field in [("default", "defaultDefaultClientScopes"), ("optional", "defaultOptionalClientScopes")]:
            self.sync_scope_links(f"/default-{kind}-client-scopes", self.document[field], scopes, f"realm {kind} scopes")
        for spec in self.document["clients"]:
            name = spec["clientId"]
            if name not in clients:
                continue
            client_id = clients[name]["id"]
            for kind in ("default", "optional"):
                self.sync_scope_links(f"/clients/{client_id}/{kind}-client-scopes", spec[f"{kind}ClientScopes"], scopes,
                                      f"{name} {kind} scopes")
            self.sync_grants(f"/clients/{client_id}/scope-mappings", *self.scope_grants("client", name, clients))

        for spec in self.document.get("users", []):
            name = spec["serviceAccountClientId"]
            if name in clients:
                user = self.api.admin("GET", f"/clients/{clients[name]['id']}/service-account-user")
                self.sync_user(user, spec, clients)
        for spec in [tooling["operator"], *tooling["fixtures"]]:
            users = self.api.admin("GET", "/users?" + urlencode({"username": spec["username"], "exact": "true"}))
            if users:
                require(users[0].get("attributes", {}).get("rentflow.managed") == ["true"],
                        "Refusing to modify an unmanaged local user")
                self.sync_user(users[0], spec, clients)
        return self.changes


def ensure_user(api, settings, spec):
    username = spec["username"]
    users = api.admin("GET", "/users?" + urlencode({"username": username, "exact": "true"}))
    require(len(users) <= 1, "Ambiguous managed user")
    if not users:
        fields = {key: value for key, value in spec.items() if key not in {"realmRoles", "clientRoles"}}
        fields["credentials"] = [{"type": "password", "value": settings.secret(username + "-password"), "temporary": False}]
        api.admin("POST", "/users", fields)
        users = api.admin("GET", "/users?" + urlencode({"username": username, "exact": "true"}))
    user = users[0]
    require(user.get("attributes", {}).get("rentflow.managed") == ["true"], "Refusing to modify an unmanaged user")
    clients = {client["clientId"]: client for client in api.admin("GET", "/clients")}
    Policy(api, {}, apply=True).sync_user(user, spec, clients)
    return user["id"]
