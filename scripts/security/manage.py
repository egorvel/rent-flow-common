#!/usr/bin/env python3
"""Explicit local Keycloak lifecycle; all credentials travel via files/request bodies."""

import fcntl
import json
import os
from pathlib import Path
import secrets
import sys

from common import Api, Local, SecurityError, deliver_recipients, maintenance, require, write_private
from policy import Policy, desired, ensure_user, realm_import


HELP = """Usage: scripts/local-keycloak.sh <operation>
  init                     Generate missing credentials and render non-secret configuration.
  bootstrap                Provision an empty installation; explicitly resume interrupted setup.
  start | stop | status    Manage identity services, preserving all data.
  logs                     Follow bounded-level Keycloak logs.
  policy plan | apply      Report/reconcile Common-owned realm policy.
  fixtures apply           Create/preserve optional customer-a, customer-b and staff users.
  recipients apply         Refresh the two private UID-10001 caller copies without rotating secrets.
  verify                   Verify real machine tokens and host/container issuer networking.
  verify-browser           Additionally verify real browser Code + PKCE using local fixtures.
  rotate-client CLIENT     Explicitly rotate pricing-service or reservation-service credentials.
  help                     Show this help.
Initial setup: init; bootstrap; optionally fixtures apply; verify or verify-browser.
Local HTTP/development mode only. Add '127.0.0.1 auth.rentflow.test' to /etc/hosts for browser use.
"""


def init(local):
    settings = local.settings
    document = desired(settings)
    tooling = document["_rentflow"]
    state = settings.state
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(state.stat().st_uid == os.getuid() and state.stat().st_mode & 0o077 == 0,
            "Secret directory must be owned by this user with mode 0700")
    config = state / "settings.json"
    if config.exists():
        local.check_target()
    names = ["postgres-admin-password", "keycloak-db-password", "bootstrap-admin-client-secret"]
    names += [secret["file"] for secret in tooling["clientSecrets"].values()]
    names += [user["username"] + "-password" for user in [tooling["operator"], *tooling["fixtures"]]]
    for name in names:
        require(Path(name).name == name, "Secret file names must be simple file names")
        path = state / name
        if not path.exists():
            settings.save(name, secrets.token_urlsafe(48) + "\n")
        require(path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077 == 0,
                f"Secret file must be owned by this user with mode 0600: {name}")
    settings.save("settings.json", json.dumps(settings.values, indent=2) + "\n")
    runtime = state / "runtime"
    write_private(runtime / "import" / (settings.realm + "-realm.json"), json.dumps(realm_import(document), indent=2) + "\n")
    override = {"services": {"rentflow-keycloak": {"secrets": ["keycloak_bootstrap_secret"]}},
                "secrets": {"keycloak_bootstrap_secret": {"file": str(state / "bootstrap-admin-client-secret")}}}
    write_private(runtime / "bootstrap.compose.json", json.dumps(override, indent=2) + "\n")
    print("Local configuration initialized; existing credentials preserved. Run bootstrap for first setup.")


def ready(local, bootstrap=False, recreate=False):
    args = ["up", "--detach", "--wait", "--wait-timeout", "240"]
    if recreate:
        args.append("--force-recreate")
    local.compose(*args, "rentflow-keycloak", bootstrap=bootstrap)


def bootstrap(local):
    settings = local.settings
    document = desired(settings)
    tooling = document["_rentflow"]
    complete = settings.state / "initialized"
    if complete.exists():
        ready(local)
        maintenance(settings).admin("GET")
        deliver_recipients(local)
        print("Initialization already complete; identities and credentials preserved.")
        return
    ready(local, bootstrap=True)
    # Resume safely after interruption: a working realm credential proves it was provisioned.
    try:
        api = maintenance(settings)
        api.admin("GET")
        root_api = None
    except SecurityError:
        root_api = Api(settings)
        root_api.bearer = root_api.token("rentflow-bootstrap", settings.secret("bootstrap-admin-client-secret"), "master")
        api = root_api
    Policy(api, document, apply=True).run()
    for client, secret in tooling["clientSecrets"].items():
        filename = secret["file"]
        representation = api.client(client)
        attrs = dict(representation.get("attributes", {}))
        if attrs.get("rentflow.credentials.initialized") != "true":
            attrs["rentflow.credentials.initialized"] = "true"
            api.admin("PUT", "/clients/" + representation["id"],
                      {"secret": settings.secret(filename), "attributes": attrs})
        Api(settings).token(client, settings.secret(filename))
    ensure_user(api, settings, tooling["operator"])
    maintenance(settings).admin("GET")
    # Only a bootstrap token can remove the master-realm account. If it was already removed,
    # successful scoped maintenance proves this is the final resume stage.
    if root_api is None:
        probe = Api(settings, realm="master")
        status, body = probe.token_response("rentflow-bootstrap", settings.secret("bootstrap-admin-client-secret"))
        if status == 200:
            probe.bearer = body["access_token"]
            root_api = probe
    if root_api is not None:
        root_api.realm = "master"
        client = root_api.client("rentflow-bootstrap")
        root_api.admin("DELETE", "/clients/" + client["id"])
    ready(local, recreate=True)
    maintenance(settings).admin("GET")
    settings.save("initialized", "Initialization complete; bootstrap account removed.\n")
    deliver_recipients(local)
    print("Bootstrap complete; scoped maintenance works and global bootstrap access is removed.")


def policy(local, apply):
    api = maintenance(local.settings)
    operation = Policy(api, desired(local.settings), apply=apply)
    changes = operation.run()
    for label in changes:
        print(("Applied: " if apply else "Drift: ") + label)
    if apply:
        require(not Policy(api, desired(local.settings)).run(), "Policy failed read-back verification")
    if not changes:
        print("Realm policy matches; no changes.")
    return bool(changes)


def rotate(local, client):
    settings = local.settings
    credentials = desired(settings)["_rentflow"]["clientSecrets"]
    require(client in credentials and credentials[client]["rotatable"],
            "Client is not enabled for rotation in the realm template")
    api = maintenance(settings)
    filename = credentials[client]["file"]
    pending = settings.state / (filename + ".pending")
    # Persist first so interruption cannot lose the new secret. Rerun completes the same rotation.
    if not pending.exists():
        settings.save(pending.name, json.dumps({"previous": settings.secret(filename),
                                               "next": secrets.token_urlsafe(48)}) + "\n")
    rotation = json.loads(pending.read_text())
    new, old = rotation["next"], rotation["previous"]
    representation = api.client(client)
    api.admin("PUT", "/clients/" + representation["id"], {"secret": new})
    Api(settings).token(client, new)
    status, _ = Api(settings).token_response(client, old)
    require(status == 401, "Old credential must fail token acquisition after rotation")
    settings.save(filename, new + "\n")
    deliver_recipients(local, (client,))
    pending.unlink()
    for other, secret in credentials.items():
        if other != client and secret["rotatable"]:
            Api(settings).token(other, settings.secret(secret["file"]))
    print(f"Rotated {client}; previous secret rejected and other caller unaffected.")
    print("Recipient copy refreshed; recreate only the affected caller to load its new file.")


def main():
    args = sys.argv[1:]
    if not args or args == ["help"]:
        print(HELP)
        return
    os.umask(0o077)
    local = Local()
    state = local.settings.state
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(state / ".operation.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SecurityError("Another local identity operation is already running") from None
        if args == ["init"]:
            init(local)
            return
        local.check_target()
        if args == ["bootstrap"]:
            bootstrap(local)
        elif args == ["start"]:
            require((state / "initialized").exists(), "Setup incomplete; run bootstrap explicitly")
            ready(local)
            require(not policy(local, False), "Policy drift detected; run policy plan/apply explicitly")
        elif args == ["stop"]:
            local.compose("stop", "rentflow-keycloak", "rentflow-keycloak-postgres")
        elif args == ["status"]:
            local.compose("ps", "rentflow-keycloak", "rentflow-keycloak-postgres")
        elif args == ["logs"]:
            local.compose("logs", "--follow", "--tail", "100", "rentflow-keycloak")
        elif args in (["policy", "plan"], ["policy", "apply"]):
            drift = policy(local, args[1] == "apply")
            if drift and args[1] == "plan":
                sys.exit(2)
        elif args == ["fixtures", "apply"]:
            api = maintenance(local.settings)
            for user in desired(local.settings)["_rentflow"]["fixtures"]:
                ensure_user(api, local.settings, user)
            print("Local fixtures applied; existing subjects/passwords preserved.")
        elif args == ["recipients", "apply"]:
            deliver_recipients(local)
            print("Private caller recipient copies refreshed; source credentials preserved.")
        elif args in (["verify"], ["verify-browser"]):
            local.runner("machine" if args == ["verify"] else "browser")
            local.runner(network=True)
        elif len(args) == 2 and args[0] == "rotate-client":
            rotate(local, args[1])
        else:
            raise SecurityError("Unknown operation; use help")


if __name__ == "__main__":
    try:
        main()
    except (SecurityError, OSError, ValueError, KeyError) as error:
        # Only our bounded messages may be shown; library exceptions can contain secret bodies.
        print("ERROR: " + (str(error) if isinstance(error, SecurityError) else
                           "Local identity operation failed (" + type(error).__name__ + ")"), file=sys.stderr)
        sys.exit(1)
