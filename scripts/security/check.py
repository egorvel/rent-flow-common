#!/usr/bin/env python3
"""Real-provider checks. No token bodies, browser traces or callback queries are logged."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt

from common import Api, SecurityError, Settings, maintenance, require
from policy import Policy, desired

# Independent acceptance expectations, never used to configure or assign permissions.
BUSINESS_CLIENTS = {"pricing-service": "inventory:read", "reservation-service": "inventory:reserve"}
FIXTURES = {"customer-a": "CUSTOMER", "customer-b": "CUSTOMER", "staff": "STAFF"}
INVENTORY_HUMAN_ROLES = {
    "CUSTOMER": {"inventory:read", "inventory:list"},
    "STAFF": {"inventory:read", "inventory:list", "inventory:manage", "inventory:history"},
}
PRICING_HUMAN_ROLES = {
    "CUSTOMER": {"pricing:read", "pricing:list"},
    "STAFF": {"pricing:read", "pricing:list", "pricing:manage"},
}
RESERVATION_HUMAN_ROLES = {
    "CUSTOMER": {"reservation:create", "reservation:read", "reservation:list", "reservation:cancel"},
    "STAFF": {"reservation:create", "reservation:read:any", "reservation:list:any", "reservation:cancel:any", "reservation:manage"},
}


def human_permissions(claims, user, audience):
    roles = {"inventory-api": INVENTORY_HUMAN_ROLES, "pricing-api": PRICING_HUMAN_ROLES,
             "reservation-api": RESERVATION_HUMAN_ROLES}
    expected = roles[audience][FIXTURES[user]] if audience in roles else set()
    resource = claims.get("resource_access", {})
    require(set(resource) == ({audience} if expected else set()), "Human roles belong only to requested API")
    require(set(resource.get(audience, {}).get("roles", [])) == expected, "Exact approved human permissions")
    require(not claims.get("realm_access", {}).get("roles"), "Realm business labels are not API authorities")


def settings():
    return Settings(json.loads(Path("/state/settings.json").read_text()), "/state")


def validate(api, token, audience, issuer=None, bearer=True):
    status, jwks = api.request("GET", f"/realms/{api.settings.realm}/protocol/openid-connect/certs")
    require(status == 200, "JWKS retrieval")
    header = jwt.get_unverified_header(token)
    require(header.get("alg") == "RS256" and header.get("typ") == "JWT", "JWT algorithm/type policy")
    keys = [key for key in jwks["keys"] if key.get("kid") == header.get("kid")]
    require(len(keys) == 1, "Trusted signing key selection")
    key = jwt.PyJWK.from_dict(keys[0], algorithm="RS256").key
    required = ["iss", "sub", "aud", "exp", "iat", "azp"] + (["typ"] if bearer else [])
    claims = jwt.decode(token, key, algorithms=["RS256"], audience=audience,
                        issuer=issuer or api.settings.issuer, leeway=30,
                        options={"require": required})
    if bearer:
        require(claims["typ"] == "Bearer", "Access-token payload type")
    require(all(isinstance(claims[k], str) and claims[k] for k in ("iss", "sub", "azp")), "Identity claim types")
    lifetime = claims["exp"] - claims["iat"]
    # Keycloak 26.8.1 samples iat and exp separately in TokenManager. A second
    # boundary can add one tick; policy read-back independently enforces 300s.
    require(0 < lifetime <= 301, f"Five-minute token lifetime ({audience}, bearer={bearer}, seconds={lifetime})")
    return claims


def audiences(claims):
    aud = claims["aud"]
    return {aud} if isinstance(aud, str) else set(aud)


def rejected(operation, label):
    try:
        operation()
    except (SecurityError, jwt.PyJWTError):
        return
    raise SecurityError("Expected rejection: " + label)


def machine(settings, loopback=True):
    api = Api(settings, loopback=loopback)
    status, discovery = api.request("GET", f"/realms/{settings.realm}/.well-known/openid-configuration")
    require(status == 200 and discovery["issuer"] == settings.issuer, "Canonical discovery issuer")
    for name, suffix in [("token_endpoint", "token"), ("jwks_uri", "certs"), ("authorization_endpoint", "auth")]:
        require(discovery[name] == settings.issuer + "/protocol/openid-connect/" + suffix,
                "Canonical advertised " + name)
    subjects = set()
    for client, permission in BUSINESS_CLIENTS.items():
        status, response = api.token_response(client, settings.secret(client + "-client-secret"))
        require(status == 200 and "refresh_token" not in response, "Client Credentials token without refresh token")
        token = response["access_token"]
        claims = validate(api, token, "inventory-api")
        require(claims["azp"] == client, "Machine caller azp")
        require(audiences(claims) == {"inventory-api"}, "Exact machine audience")
        require(claims.get("resource_access") == {"inventory-api": {"roles": [permission]}}, "Exact machine permissions")
        require(not claims.get("realm_access", {}).get("roles"), "No machine realm/business roles")
        subjects.add(claims["sub"])
        status, _ = api.token_response(client, "deliberately-invalid-test-secret")
        require(status == 401, "Wrong machine credential denial")
        for scope in ["pricing-access", "reservation-access", "inventory-access", "offline_access"]:
            status, body = api.token_response(client, settings.secret(client + "-client-secret"), scope=scope)
            if status == 200:
                restricted = validate(api, body["access_token"], "inventory-api")
                require(audiences(restricted) == {"inventory-api"} and restricted.get("resource_access") == claims["resource_access"],
                        "Unattached scope cannot expand grants")
            else:
                require(status == 400, "Restricted scope denial")
        for grant in ["password", "urn:ietf:params:oauth:grant-type:token-exchange"]:
            status, _ = api.token_response(client, settings.secret(client + "-client-secret"), grant_type=grant)
            require(status in (400, 401, 403), "Prohibited machine grant")
        rejected(lambda: validate(api, token, "pricing-api"), "wrong audience")
        rejected(lambda: validate(api, token, "inventory-api", issuer=settings.issuer + "-other"), "wrong issuer")
        segments = token.split(".")
        segments[1] = base64.urlsafe_b64encode(json.dumps(dict(claims, sub="tampered")).encode()).decode().rstrip("=")
        rejected(lambda: validate(api, ".".join(segments), "inventory-api"), "modified signature payload")
    require(len(subjects) == 2, "Distinct machine subjects")
    if loopback:
        admin = maintenance(settings)
        require(not Policy(admin, desired(settings)).run(), "Managed realm matches current policy")
        for resource in ["inventory-api", "pricing-api", "reservation-api"]:
            status, _ = api.token_response(resource)
            require(status in (400, 401), "Resource registration cannot issue tokens")
        for client in ["inventory-swagger", "pricing-swagger", "reservation-swagger", "rentflow-pkce-check"]:
            status, _ = api.token_response(client)
            require(status in (400, 401), "Public interactive client cannot use Client Credentials")
            status, _ = api.token_response(client, grant_type="password", username="unused", password="unused")
            require(status in (400, 401), "Public client password grant disabled")
        bootstrap = Api(settings, realm="master")
        status, _ = bootstrap.token_response("rentflow-bootstrap", settings.secret("bootstrap-admin-client-secret"))
        require(status == 401, "Removed global bootstrap account")
        for client in BUSINESS_CLIENTS:
            caller = Api(settings, bearer=api.token(client, settings.secret(client + "-client-secret")))
            status, _ = caller.request("GET", f"/admin/realms/{settings.realm}")
            require(status == 403, "Business caller cannot administer realm")
        maintainer = maintenance(settings)
        status, _ = maintainer.request("GET", "/admin/realms/master")
        require(status == 403, "Realm maintainer has no master administration")
    print("Machine token, grant restrictions and " + ("host" if loopback else "container") + " issuer checks passed")


class Callback:
    def __init__(self, port):
        self.attempts = {}
        attempts = self.attempts

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                fields = parse_qs(url.query)
                state = fields.get("state", [""])[0]
                valid = url.path == "/callback" and state in attempts
                if valid:
                    attempts[state]["response"] = fields
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"Login verification callback received" if valid else b"Invalid login state")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()


def browser(settings, inventory=False, pricing=False, serve=False):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

    machine(settings)
    api = Api(settings)
    callback_uri = f"http://127.0.0.1:{settings.pkce_port}/callback"
    with Callback(settings.pkce_port) as callback, sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
        sessions = {}

        def authorize(user, scope="inventory-access", changes=None, client="rentflow-pkce-check"):
            state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
            swagger = client in ("inventory-swagger", "pricing-swagger", "reservation-swagger")
            port = settings.values[client.replace("-swagger", "_port")] if swagger else None
            redirect = f"http://localhost:{port}/swagger-ui/oauth2-redirect.html" if swagger else callback_uri
            attempt = {"verifier": verifier, "nonce": nonce, "client": client, "redirect": redirect}
            callback.attempts[state] = attempt
            fields = {"client_id": client, "response_type": "code", "redirect_uri": redirect,
                      "scope": ("openid " + scope).strip(), "state": state, "nonce": nonce,
                      "code_challenge": challenge, "code_challenge_method": "S256"}
            if changes:
                fields.update(changes)
                fields = {k: v for k, v in fields.items() if v is not None}
            # No tracing, screenshot, video, HAR, storage-state or console capture is enabled.
            context = chromium.new_context(accept_downloads=False)
            step = "open login"
            try:
                page = context.new_page()
                if swagger:
                    # Routing cannot intercept later URLs in a redirect chain. Forward the real
                    # login POST without following redirects, then capture Keycloak's callback
                    # Location in memory. No code-bearing request reaches the application's logs.
                    def receive(route):
                        if route.request.method != "POST":
                            route.continue_()
                            return
                        try:
                            response = route.fetch(max_redirects=0)
                            location = response.headers.get("location", "")
                            if location.startswith(redirect + "?"):
                                attempt["response"] = parse_qs(urlsplit(location).query)
                                route.fulfill(status=200, content_type="text/plain", body="PKCE callback received")
                            else:
                                route.fulfill(response=response)
                        except Exception:
                            attempt["callback_error"] = True
                            route.fulfill(status=503, content_type="text/plain", body="Callback verification failed")
                    page.route(re.compile("^" + re.escape(settings.issuer) + "/login-actions/"), receive)
                page.goto(settings.issuer + "/protocol/openid-connect/auth?" + urlencode(fields))
                if changes:
                    require(page.locator("#username").count() == 0, "Invalid authorization request rejected before login")
                    require("code" not in parse_qs(urlsplit(page.url).query), "Invalid authorization request has no code")
                    return None
                step = "fill login form"
                page.locator("#username").fill(user)
                page.locator("#password").fill(settings.secret(user + "-password"))
                step = "submit login"
                page.locator("#kc-login").click()
                step = "receive callback"
                if not swagger:
                    page.wait_for_url(lambda url: url.startswith(redirect), timeout=20000)
                require(not attempt.get("callback_error"), "Swagger login redirect acquisition")
                response = attempt.get("response", {})
                require(response.get("state") == [state] and len(response.get("code", [])) == 1,
                        "Callback state and authorization code")
                return dict(attempt, code=response["code"][0])
            except BrowserTimeout:
                location = urlsplit(page.url)
                # Path/origin only: never include the callback query or form-action parameters.
                raise SecurityError(f"Browser timeout during {step} ({client}, {user}, {scope}); "
                                    f"page={location.scheme}://{location.netloc}{location.path}; "
                                    f"callback_received={'response' in attempt}") from None
            finally:
                context.close()

        def exchange(attempt, verifier=None):
            return api.token_response(attempt["client"], grant_type="authorization_code",
                                      code=attempt["code"], redirect_uri=attempt["redirect"],
                                      code_verifier=verifier or attempt["verifier"])

        try:
            connection = http.client.HTTPConnection("127.0.0.1", settings.pkce_port)
            connection.request("GET", "/callback?state=unrecognized-test-state&code=ignored")
            require(connection.getresponse().status == 400, "Local callback rejects invalid state")
            connection.close()
            subjects = {}
            inventory_tokens = {}
            pricing_tokens = {}
            other_audience_token = None
            for user in FIXTURES:
                for scope in ["inventory-access", "pricing-access", "reservation-access"]:
                    attempt = authorize(user, scope)
                    status, tokens = exchange(attempt)
                    require(status == 200, "Code and verifier exchange")
                    audience = scope.replace("-access", "-api")
                    claims = validate(api, tokens["access_token"], audience)
                    sessions[user, audience.removesuffix("-api"), "rentflow-pkce-check"] = tokens
                    require(audiences(claims) == {audience}, "Single requested human audience")
                    require(claims["azp"] == "rentflow-pkce-check", "Verification client azp")
                    human_permissions(claims, user, audience)
                    if audience == "inventory-api":
                        inventory_tokens[user] = tokens
                    elif audience == "pricing-api":
                        pricing_tokens[user] = tokens
                    else:
                        other_audience_token = tokens["access_token"]
                    require("customerId" not in claims, "No trusted domain customer ID in provider token")
                    previous = subjects.setdefault(user, claims["sub"])
                    require(previous == claims["sub"], "Stable subject across audience scopes")
                    identity = validate(api, tokens["id_token"], "rentflow-pkce-check", bearer=False)
                    require(identity["nonce"] == attempt["nonce"], "ID token nonce")
                    require(identity["sub"] == claims["sub"], "ID/access subject match")
                    rejected(lambda: validate(api, tokens["id_token"], audience), "ID token is not an API credential")
                    if "refresh_token" in tokens:
                        rejected(lambda: validate(api, tokens["refresh_token"], audience), "Refresh token is not an API credential")
            require(len(set(subjects.values())) == 3, "Three distinct human identities")
            swagger_tokens = {"inventory": {}, "pricing": {}, "reservation": {}}
            for resource in swagger_tokens:
                client, audience = resource + "-swagger", resource + "-api"
                for user in FIXTURES:
                    attempt = authorize(user, scope="", client=client)
                    status, tokens = exchange(attempt)
                    require(status == 200, "Swagger default scope Code and PKCE exchange")
                    claims = validate(api, tokens["access_token"], audience)
                    require(audiences(claims) == {audience} and claims["azp"] == client,
                            "Swagger has exactly its own audience and client identity")
                    human_permissions(claims, user, audience)
                    require(claims["sub"] == subjects[user], "Same subject through both approved human clients")
                    identity = validate(api, tokens["id_token"], client, bearer=False)
                    require(identity["nonce"] == attempt["nonce"] and identity["sub"] == claims["sub"],
                            "Swagger ID token nonce and identity")
                    swagger_tokens[resource][user] = tokens
                    sessions[user, resource, client] = tokens

            def renew(user, scope="inventory-access"):
                status, tokens = exchange(authorize(user, scope))
                require(status == 200, "Renewed browser Code and PKCE exchange")
                validate(api, tokens["access_token"], scope.replace("-access", "-api"))
                return tokens

            if inventory:
                from inventory_check import verify_inventory
                verify_inventory(settings, inventory_tokens, swagger_tokens["inventory"], other_audience_token, renew)
            if pricing:
                from pricing_check import verify_pricing
                verify_pricing(settings, pricing_tokens, swagger_tokens["pricing"], inventory_tokens,
                               lambda user: renew(user, "pricing-access"))
            attempt = authorize("customer-a")
            status, _ = exchange(attempt, verifier=secrets.token_urlsafe(48))
            require(status == 400, "Incorrect PKCE verifier rejected")
            for changes in [{"code_challenge": None, "code_challenge_method": None},
                            {"code_challenge_method": "plain"},
                            {"redirect_uri": "http://127.0.0.1:1/unregistered"}]:
                authorize("customer-a", changes=changes)
            # Keycloak detaches the client session when a code is reused. Keep that
            # negative test on a separate login so saved sessions can still refresh.
            attempt = authorize("customer-a")
            status, _ = exchange(attempt)
            require(status == 200, "Separate code-reuse fixture authenticates")
            status, _ = exchange(attempt)
            require(status == 400, "Authorization code cannot be reused")
            for (user, resource, client), tokens in sessions.items():
                status, refreshed = api.token_response(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
                require(status == 200, "Live browser session refresh survives separate PKCE denial tests")
                audience = resource + "-api"
                claims = validate(api, refreshed["access_token"], audience)
                require(claims["sub"] == subjects[user] and claims["azp"] == client and audiences(claims) == {audience},
                        "Refreshed browser token preserves subject, client and exact audience")
                human_permissions(claims, user, audience)
                sessions[user, resource, client] = refreshed
            if serve:
                def session(user, resource, client="rentflow-pkce-check", forget=False, refresh=False):
                    require(user in FIXTURES and resource in swagger_tokens and
                            client in ("rentflow-pkce-check", resource + "-swagger"), "Unknown smoke identity/client")
                    key = user, resource, client
                    if forget:
                        sessions.pop(key, None)
                    tokens = sessions.get(key)
                    if refresh or not tokens or jwt.decode(tokens["access_token"], options={"verify_signature": False})["exp"] < time.time() + 30:
                        if tokens and tokens.get("refresh_token"):
                            status, tokens = api.token_response(client, grant_type="refresh_token", refresh_token=tokens["refresh_token"])
                            category = tokens.get("error") if isinstance(tokens, dict) else None
                            category = category if category in {"invalid_grant", "invalid_scope", "invalid_client", "unauthorized_client", "unsupported_grant_type", "invalid_request"} else "unexpected"
                            require(status == 200, f"Smoke browser refresh (HTTP {status}, {category})")
                        else:
                            status, tokens = exchange(authorize(user, resource + "-access" if client == "rentflow-pkce-check" else "", client=client))
                            require(status == 200, "Smoke browser login")
                        validate(api, tokens["access_token"], resource + "-api")
                        sessions[key] = tokens
                    return tokens["access_token"]
                from smoke_proxy import serve_proxy
                serve_proxy(settings, session)
        finally:
            chromium.close()
    print("Browser login, human identities, nonce/state and PKCE success/denial checks passed")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) == 2 else "invalid"
    try:
        config = settings()
        if stage == "network":
            machine(config, loopback=False)
        elif stage == "machine":
            machine(config)
        elif stage == "browser":
            browser(config)
        elif stage == "inventory":
            browser(config, inventory=True)
        elif stage == "pricing":
            browser(config, inventory=True, pricing=True)
        elif stage == "broker":
            browser(config, serve=True)
        elif stage == "reservation":
            from reservation_check import verify_reservation
            verify_reservation(config)
        else:
            raise SecurityError("Unknown verification stage")
    except Exception as error:
        # Playwright/JWT exception strings can include URLs, codes and credentials. Never print them.
        message = str(error) if isinstance(error, SecurityError) else type(error).__name__
        if isinstance(error, jwt.MissingRequiredClaimError):
            message += " (missing " + error.claim + ")"
        print(f"ERROR: {stage} check: {message}", file=sys.stderr)
        sys.exit(1)
