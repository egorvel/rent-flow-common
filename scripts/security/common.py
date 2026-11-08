"""Local-only control plane. Never include HTTP bodies or credentials in diagnostics."""

import http.client
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, urlencode, urlsplit

ROOT = Path(os.environ["RENTFLOW_ROOT"]) if "RENTFLOW_ROOT" in os.environ else Path(__file__).resolve().parents[2]


class SecurityError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise SecurityError(message)


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)
    temporary.replace(path)


class Settings:
    def __init__(self, values, state):
        self.values = values
        self.state = Path(state)
        self.realm = values["realm"]
        self.port = int(values["port"])
        self.pkce_port = int(values["pkce_port"])
        self.origin = f"http://auth.rentflow.test:{self.port}"
        self.issuer = f"{self.origin}/realms/{self.realm}"
        require(re.fullmatch(r"rentflow-[a-z0-9-]+", self.realm), "Invalid local realm name")
        require(all(1024 <= int(values[k]) <= 65535 for k in ("port", "pkce_port")), "Invalid local port")

    def secret(self, name):
        try:
            value = (self.state / name).read_text().strip()
        except OSError:
            raise SecurityError(f"Missing secret file: {name}; run init") from None
        require(bool(value), f"Empty secret file: {name}")
        return value

    def save(self, name, value):
        write_private(self.state / name, value)


class Local:
    def __init__(self):
        self.env = dict(os.environ)
        self.env["SECURITY_UID"] = str(os.getuid())
        self.base = ["docker", "compose", "--project-directory", str(ROOT), "--file", str(ROOT / "compose.yaml")]
        result = subprocess.run(self.base + ["--profile", "security-tools", "config", "--format", "json"], cwd=ROOT,
                                env=self.env, capture_output=True, text=True)
        require(result.returncode == 0, "Compose configuration failed; check non-secret settings")
        config = json.loads(result.stdout)
        identity = config["services"]["rentflow-keycloak"]
        environment = identity["environment"]
        state = Path(config["secrets"]["keycloak_db_password"]["file"]).parent
        self.env["SECURITY_STATE_DIR"] = str(state)
        self.settings = Settings({"realm": environment["KEYCLOAK_REALM"],
                                  "port": int(environment["KC_HTTP_PORT"]),
                                  "pkce_port": int(environment["KEYCLOAK_PKCE_PORT"]),
                                  "project": config["name"],
                                  "inventory_port": int(config["services"]["inventory"]["ports"][0]["published"]),
                                  "pricing_port": int(config["services"]["pricing"]["ports"][0]["published"]),
                                  "reservation_port": int(config["services"]["reservation"]["ports"][0]["published"])}, state)
        self.config = config

    def compose(self, *args, bootstrap=False, capture=False, check=True):
        command = list(self.base)
        if bootstrap:
            command += ["--file", str(self.settings.state / "runtime" / "bootstrap.compose.json")]
        result = subprocess.run(command + list(args), cwd=ROOT, env=self.env,
                                capture_output=capture, text=True)
        if check:
            require(result.returncode == 0, f"Compose operation failed ({args[0]})")
        return result

    def check_target(self):
        path = self.settings.state / "settings.json"
        require(path.exists(), "Run init before this operation")
        require(json.loads(path.read_text()) == self.settings.values,
                "Settings differ from initialized state; use a separate state directory/project")

    def runner(self, *args, network=False):
        # Compose gives the probe container the application-network DNS alias. Browser tests
        # instead use Linux host networking for the loopback-only callback/publication.
        self.compose("build", "--quiet", "security-check")
        if network:
            return self.compose("run", "--rm", "--no-deps", "security-check", "network")
        image = self.config["services"]["security-check"]["image"]
        command = ["docker", "run", "--rm", "--network", "host", "--add-host", "auth.rentflow.test:127.0.0.1",
                   "--user", str(os.getuid()), "--shm-size", "512m", "--read-only",
                   "--tmpfs", "/tmp:rw,nosuid,size=512m", "--env", "HOME=/tmp",
                   "--volume", f"{self.settings.state}:/state:ro", image, *args]
        result = subprocess.run(command, cwd=ROOT)
        require(result.returncode == 0, "Security verification failed; see bounded assertion above")


class Api:
    def __init__(self, settings, bearer=None, realm=None, loopback=True):
        self.settings = settings
        self.realm = realm or settings.realm
        self.bearer = bearer
        self.loopback = loopback

    def request(self, method, path, data=None, form=False):
        headers = {"Host": f"auth.rentflow.test:{self.settings.port}", "Accept": "application/json"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        body = None
        if data is not None:
            body = urlencode(data) if form else json.dumps(data)
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        connection = http.client.HTTPConnection("127.0.0.1" if self.loopback else "auth.rentflow.test",
                                                self.settings.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(2_000_000)
            try:
                result = json.loads(raw) if raw else None
            except (ValueError, UnicodeError):
                result = None
            return response.status, result
        except (OSError, http.client.HTTPException):
            raise SecurityError("Identity HTTP connection failed") from None
        finally:
            connection.close()

    def token_response(self, client, secret=None, realm=None, **fields):
        data = {"grant_type": "client_credentials", "client_id": client}
        if secret is not None:
            data["client_secret"] = secret
        data.update(fields)
        return self.request("POST", f"/realms/{realm or self.realm}/protocol/openid-connect/token", data, form=True)

    def token(self, client, secret, realm=None):
        status, data = self.token_response(client, secret, realm)
        require(status == 200 and isinstance(data, dict) and "access_token" in data,
                f"Token acquisition failed for {client} (HTTP {status})")
        return data["access_token"]

    def admin(self, method, path="", data=None, expected=None):
        status, result = self.request(method, f"/admin/realms/{quote(self.realm, safe='')}{path}", data)
        require(status in (expected or (200, 201, 204)),
                f"Administrative operation failed: {method} (HTTP {status})")
        return result

    def client(self, name):
        clients = self.admin("GET", "/clients?" + urlencode({"clientId": name}))
        matches = [c for c in clients if c["clientId"] == name]
        require(len(matches) == 1, f"Expected one client registration: {name}")
        return matches[0]


def maintenance(settings):
    api = Api(settings)
    api.bearer = api.token("rentflow-realm-maintainer", settings.secret("realm-maintainer-client-secret"))
    return api
