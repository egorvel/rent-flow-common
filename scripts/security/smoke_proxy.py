"""Isolated smoke HTTP proxy: real PKCE credentials stay in runner memory."""

import hmac
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from urllib.parse import parse_qs, urlsplit

import jwt

from common import SecurityError, require


def serve_proxy(settings, session):
    require(settings.realm == "rentflow-smoke" and settings.values["project"] == "rentflow-common-smoke",
            "HTTP smoke proxy requires the combined isolated project")
    capability = settings.secret("smoke-proxy-key")
    resources = {int(settings.values[name + "_port"]): name for name in ("inventory", "pricing", "reservation")}

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            if not hmac.compare_digest(self.headers.get("X-Smoke-Control", ""), capability):
                self.send_error(403)
                return
            target = urlsplit(self.path)
            stage = "validate target"
            try:
                if target.path in ("/_smoke/ready", "/_smoke/identity", "/_smoke/forget", "/_smoke/refresh") and not target.netloc:
                    fields = parse_qs(target.query)
                    user = fields.get("user", ["staff"])[0]
                    resource = fields.get("resource", ["reservation"])[0]
                    client = fields.get("client", ["rentflow-pkce-check"])[0]
                    body = {}
                    if target.path != "/_smoke/ready":
                        stage = "obtain browser token"
                        token = session(user, resource, client, forget=target.path.endswith("forget"),
                                        refresh=target.path.endswith("refresh"))
                        claims = jwt.decode(token, options={"verify_signature": False})
                        body = {key: claims[key] for key in ("iss", "sub")}
                    raw = json.dumps(body).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                require(target.scheme == "http" and target.hostname in ("localhost", "127.0.0.1")
                        and not target.username and not target.password and target.port in resources,
                        "Proxy target outside isolated application origins")
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in {"host", "connection", "proxy-connection", "proxy-authorization",
                                                  "x-smoke-control", "x-smoke-user", "x-smoke-client"}}
                if target.path.startswith("/api/v1/") and "Authorization" not in headers:
                    resource = resources[target.port]
                    stage = "obtain browser token"
                    headers["Authorization"] = "Bearer " + session(self.headers.get("X-Smoke-User", "staff"), resource,
                                                                  self.headers.get("X-Smoke-Client", "rentflow-pkce-check"))
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                stage = "forward application request"
                connection = http.client.HTTPConnection("127.0.0.1", target.port, timeout=35)
                try:
                    connection.request(self.command, target.path + ("?" + target.query if target.query else ""), body=data, headers=headers)
                    response = connection.getresponse()
                    body = response.read(4_000_000)
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    connection.close()
            except Exception as error:
                # Our SecurityError labels are bounded; library exceptions can contain credentials.
                label = str(error) if isinstance(error, SecurityError) else type(error).__name__
                print(f"Smoke proxy failure during {stage}: {label}", flush=True)
                self.send_error(502, "Isolated authenticated request failed")

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = handle_request

        def log_message(self, *args):
            pass

    # A single thread keeps every Playwright operation on its owning thread.
    HTTPServer(("127.0.0.1", settings.pkce_port + 2), Handler).serve_forever()
