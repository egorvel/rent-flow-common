# Local identity walkthrough and operations

Common implements the Phase 0/1 foundation and all three service provider handoffs in Phases 2–4.
Inventory, Pricing and Reservation enforce their HTTP policies. Pricing and Reservation call
Inventory using their own narrow machine identities. This is an HTTP development stack;
production hardening remains Phase 7.

## Follow a login without exposing credentials

After the setup in [README](README.md), run:

```bash
./scripts/local-keycloak.sh verify-browser
```

The pinned test container opens a headless Chromium browser, logs in through Keycloak's actual
form as each local fixture, and exercises `rentflow-pkce-check`, `inventory-swagger`,
`pricing-swagger` and `reservation-swagger`. It starts a temporary loopback
callback server, creates a random PKCE verifier, sends only its SHA-256 challenge at authorization,
checks callback state, and exchanges the returned code with the original verifier. It validates
the ID-token nonce and verifies token signatures against the realm's actual JWKS. Refresh checks
preserve each fixture's subject, client, audience and held permissions. Code-reuse denial uses
a separate login because Keycloak invalidates that client session on reuse. For Swagger's
exact registered callback, the browser runner captures the redirect in memory and exchanges the
code itself; it does not persist browser tokens or change the application's callback handler.

The checker requests `openid inventory-access`, `openid pricing-access` and
`openid reservation-access` separately. It also checks incorrect verifiers, code reuse, missing
PKCE, plain challenges, unregistered redirects and invalid callback state. No password grant is
enabled. Screenshots, browser traces, video, HAR files, callback queries and token bodies are not
recorded. Output names the assertions rather than displaying credentials.

| Claim | Meaning in RentFlow |
| --- | --- |
| `iss` | Exact realm URL; a token from another environment is rejected |
| `sub` | Stable identity inside that issuer; `(iss, sub)` identifies the person |
| `aud` | Intended recipient API(s); each API checks its own membership |
| `azp` | Client that obtained the token, such as `rentflow-pkce-check` or `pricing-service` |
| `resource_access.inventory-api.roles` | Inventory-specific permissions, filtered by allowed scope mappings |
| `resource_access.pricing-api.roles` | Pricing-specific read/list/manage permissions, present only for the Pricing audience |
| `resource_access.reservation-api.roles` | Reservation own/any/manage permissions held by the user |
| `exp`, `iat`, optional `nbf` | Time constraints; access-token lifetime is 300 seconds |
| Payload `typ` | `Bearer` for the default Keycloak access-token profile |

The three Swagger registrations have exact callbacks/origins and wired local Authorize
buttons. Each Swagger UI defaults to its own API audience. Multiple audiences are supported by the contract; a future frontend can request
the set its workflow needs. Separate tokens/client registrations can reuse one Keycloak SSO login.

`CUSTOMER` now includes Inventory `read` and `list`; `STAFF` explicitly includes `read`, `list`,
`manage` and `history`. Their Inventory scope allows exactly those four roles and never `reserve`.
Pricing CUSTOMER includes `pricing:read`/`pricing:list`; STAFF explicitly also includes
`pricing:manage`. The Pricing scope filters those roles to the user's held set; its cached batch
POST is a read operation. Reservation CUSTOMER includes `reservation:create`, `reservation:read`,
`reservation:list` and `reservation:cancel`. STAFF explicitly includes `reservation:create`,
`reservation:read:any`, `reservation:list:any`, `reservation:cancel:any` and `reservation:manage`.
It does not inherit CUSTOMER; there is no `reservation:create:any` role. Staff creates only for
itself, while explicit any/manage grants authorize operations on other owners.

Reservation stores the exact verified issuer/subject as the owner. The response `customerId` is
that opaque subject, not an input field or a trusted customerId claim. No mapping table,
provisioning operation or provider lookup is needed. Customer lists/counts are owner-scoped;
foreign get/cancel requests return 404. Staff replacement preserves the original owner and hold
deadline. Replay is scoped to issuer/subject, survives approved client changes and token renewal,
and requires current create permission plus live owned rows. Staff PUT/DELETE are administrative
operations and do not publish Inventory releases; use cancellation for that workflow.

## Machine identities and administrative identities

`verify` checks machine credentials and issuer networking without requiring human fixtures:

```bash
./scripts/local-keycloak.sh verify
```

Pricing gets only audience `inventory-api` and role `inventory:read`. Reservation gets only that
audience and `inventory:reserve`. Inventory limits the latter to reservation
claims (`AVAILABLE -> RESERVED`) and authorizes before idempotent replay. Common grants no general
status-management, release or catalogue-write permission to machines. Inventory enforcement is
Phase 2; Pricing and Reservation outbound wiring landed in Phases 3 and 4 respectively. Inventory
also checks verified `azp`, so accidentally overgranting a machine cannot enable human-only routes.

Pricing now acquires and caches its machine token using Spring's application principal
`pricing-service`, independent of the incoming customer. Only creation needs Inventory; reads,
cached reads, list, replace and delete stay local. A downstream 401 fails that request and evicts
the token so the next create reacquires; 403 fails without eviction. Rejected credentials and
Inventory 401/403 become sanitized Pricing 502 responses; token-provider outages become 503.
These failures never become the customer's 401 or successful fallback data.

Reservation similarly caches a `reservation-service` token across human subjects and sends only
all-RESERVED Inventory claims. Its downstream idempotency UUID is deterministically derived from
the exact issuer/subject scope and the external UUIDv4, preventing customer-key collisions.
Rejected credentials and Inventory 401/403 map to safe 502; unavailable token acquisition maps to
503. These failures leave no local reservation/replay/outbox writes. Inventory 401 evicts once
without an immediate retry; 403 retains the cached token. Renewal uses the same 30-second skew.

Each caller reads only its own client secret from the config-tree filename documented in README.
Source files stay host-owned 0600. `bootstrap` or explicit `recipients apply` creates private
UID 10001-owned mode-0400 copies; Compose mounts just the appropriate copy read-only. No host
ACL prerequisite is needed. IDE runs import each caller's optional `.secrets/ide/` directory.
Explicit rotation refreshes the affected copy; force-recreate that caller to load it.

During `bootstrap`, Keycloak creates a temporary administrative service account in `master`.
The script uses Client Credentials to establish `rentflow-operator` and
`rentflow-realm-maintainer` in the RentFlow realm, verifies scoped access, deletes the temporary
master client, then recreates Keycloak without its bootstrap-secret mount. Repeated provisioning
preserves credentials. Interrupted setup can be resumed with explicit `bootstrap`; ordinary startup
never recreates an administrator. The local operator is separate from the `staff` business fixture.

The realm maintainer has realm-administration privileges only for this RentFlow realm. Its secret
is accessible to explicit maintenance tooling, never to application containers. The checker proves
that the maintainer cannot administer `master`, and business callers cannot administer RentFlow.
Unmanaged user attributes are editable only by administrators so tooling ownership markers survive
provisioning without becoming user-controlled identity claims.

## Policy updates and credentials

The checked-in realm template contains policy, service-account grants and fake local-user
definitions, with no passwords, client secrets, private keys or real people. `init` renders
non-secret environment values into the ignored runtime import directory and strips `_rentflow`.
Startup import initializes a missing realm and skips an existing one. Use explicit updates:

```bash
./scripts/local-keycloak.sh policy plan
./scripts/local-keycloak.sh policy apply
```

The template declares the policy; Python implements the API operations needed to apply it:

| Template section | What Python reads and applies |
| --- | --- |
| `roles` | Realm/client role definitions and their `composite`/`composites` declarations |
| `scopeMappings` | Allowed realm roles for a client or client scope |
| `clientScopeMappings` | Allowed client roles for a client or client scope, including maintenance |
| `users` | Service-account identity and its `realmRoles`/`clientRoles` assignments |
| `defaultDefaultClientScopes`, `defaultOptionalClientScopes` | Realm defaults, including nonempty lists |
| `_rentflow.userProfile` | Profile settings applied through Keycloak's separate user-profile API |
| `_rentflow.clientSecrets` | Credential-file references and which clients allow explicit rotation |
| `_rentflow.operator`, `_rentflow.fixtures` | Local-user properties and explicit role assignments |

For example, this native Keycloak user declaration assigns Pricing its role:

```json
{
  "username": "service-account-pricing-service",
  "enabled": true,
  "serviceAccountClientId": "pricing-service",
  "realmRoles": [],
  "clientRoles": {"inventory-api": ["inventory:read"]}
}
```

`clientScopeMappings` separately allows that role in `pricing-inventory-access`; the assignment
and the allowed scope must both permit it. Adding a different grant no longer requires a
Pricing/Reservation-specific branch in Python. An empty role list removes undeclared direct grants.
Role composites are updated from their declarations rather than always being cleared.

`_rentflow` is our own template extension, not a Keycloak realm property. It is never sent to
startup import. This keeps fake customers optional and credentials in private files. `bootstrap`
creates the declared operator, while `fixtures apply` creates declared fixture users. `policy apply`
updates those managed users only when they already exist; it does not create optional fixtures.
Existing passwords and subjects are preserved. Service-account entries in `users` are native
Keycloak fields and are included in import.

The tool owns its template's declared settings and role grants. It preserves unrelated objects and existing user/client IDs, signing keys, sessions
and passwords. There are no migration files, version markers or upgrade sequences. The current
template defines the desired settings; `plan` shows the differences and `apply`
updates them through the Admin API, then reads them back. A second apply makes no changes.

To change the supported policy declarations, edit the checked-in template, run
`policy plan`, then `policy apply`. Run `verify` or `verify-browser` when you want token/login
verification; policy updates do not build or start the test container. Updates are serialized by
a state-directory lock and can be rerun after partial failure. Keycloak's Admin API does not
provide a transaction across all these objects. Existing realms work without resetting data;
any old policy-version attribute is unused. Back up before risky updates.

Python still renders placeholders, resolves Keycloak's internal IDs, generates/injects secrets,
performs bootstrap/rotation, preserves existing data and executes HTTP/Docker operations. Those
are mechanisms rather than grant decisions. Verification code independently asserts the agreed
RentFlow token contract; intentionally changing that contract also requires updating its tests.

Source secret files under `.secrets/security/` have mode `0600`, and their directory has mode `0700`.
Container recipient copies use UID 10001 ownership and mode `0400` under the private runtime directory.
Compose mounts only selected files into each server. Files are not encrypted by Compose and must
be treated as credentials. Keep private backups of both the identity database and local secret
files; never attach them to issues or include them in source/build contexts. The setup command
preserves existing files and refuses a changed project/realm/port target. To change initialized
ports deliberately, stop the old instance, back up its files, update the non-secret settings and
matching `settings.json` target, rerun `init`, then `bootstrap`/`policy apply` as appropriate. Do not
point two independent projects at the same state directory.

Rotate one business caller explicitly:

```bash
./scripts/local-keycloak.sh rotate-client pricing-service
./scripts/local-keycloak.sh rotate-client reservation-service
```

Rotation persists a private pending value before updating Keycloak, making interruption resumable.
It verifies the new credential, denial of the previous credential and continued operation of the
other caller, then replaces the source secret file and refreshes only that caller's recipient.
Force-recreate Pricing or Reservation afterward; bind mounts and credentials are not hot-reloaded.
The local single-secret model has a brief acquisition interruption;
existing access tokens remain usable until expiry. Policy updates never silently rotate credentials.
Production overlap rotation is a later design.

## Persistence, upgrades and recovery

`stop`, `start` and ordinary Compose recreation preserve the dedicated identity database volume.
`local-postgres.sh reset` affects only application storage. `docker compose down --volumes` also
deletes identity state in that Compose project; use it only for intentional complete removal.
No identity reset operation is provided.

The `.secrets/security/initialized` file records completed provisioning, but lives outside Docker's
identity volume. If that volume is recreated while private files remain, Keycloak can import the
realm with newly generated client secrets while the old completion marker causes `bootstrap` to
skip credential provisioning. `start` and `bootstrap` then fail with maintainer-token HTTP `401`.
Running `init` again preserves the saved files and cannot repair the database's client credentials.
Restore the matching identity database backup to recover previous users and keys.

If the new database is intentional and the imported realm has **not yet been provisioned** (no
`rentflow-operator` account or `rentflow.credentials.initialized` client attributes), finish its
setup using a temporary bootstrap service account. First back up the current identity database
as below, omitting the final `start` until recovery completes. With the default state directory, run:

```bash
docker compose stop rentflow-keycloak
docker compose --file compose.yaml \
  --file .secrets/security/runtime/bootstrap.compose.json \
  run --rm --no-deps rentflow-keycloak bootstrap-admin service \
  --client-id rentflow-bootstrap \
  --client-secret:env KC_BOOTSTRAP_ADMIN_CLIENT_SECRET --no-prompt
mv .secrets/security/initialized .secrets/security/backups/initialized.before-recovery
./scripts/local-keycloak.sh bootstrap
./scripts/local-keycloak.sh start
```

Use your configured `SECURITY_STATE_DIR` in these paths if overridden. The override mounts the
existing bootstrap-secret file; the entrypoint supplies it through an environment variable without
putting its value in command arguments. `bootstrap` provisions the saved client credentials and
operator, removes the temporary master account and writes a new completion marker. Removing the
marker alone is insufficient because ordinary startup flags cannot recreate a bootstrap account
once `master` exists. For an already provisioned realm with lost credentials, use the recovery
administrator procedure below instead.

For a consistent local database backup, stop Keycloak first and keep the dump private:

```bash
./scripts/local-keycloak.sh stop
docker compose up --detach --wait rentflow-keycloak-postgres
umask 077
mkdir -p .secrets/security/backups
docker compose exec --no-TTY rentflow-keycloak-postgres \
  pg_dump --username rentflow_identity_admin --format custom rentflow_keycloak \
  > .secrets/security/backups/keycloak.dump
./scripts/local-keycloak.sh start
```

For a binary upgrade, review the target Keycloak upgrade guide, preserve the old image reference
and private files, rehearse the upgrade/restore in a separate project, update the image version and
digest, then run the combined suite. Restore the compatible image and database backup together
on rollback; never downgrade a migrated database in place. Full operational restore/HA requirements
remain Phase 7.

If administrative access is lost, restore missing local credential files from a private backup
first. For a temporary recovery administrator, stop every Keycloak server using this database:

```bash
docker compose stop rentflow-keycloak
docker compose up --detach --wait rentflow-keycloak-postgres
docker compose run --rm --no-deps rentflow-keycloak \
  bootstrap-admin user --username recovery-admin
docker compose up --detach --wait rentflow-keycloak
```

The recovery command prompts privately for a password. Log in through the master administration
console, repair the RentFlow operator/maintenance access deliberately, verify it, then delete the
recovery administrator. Keep client-secret files consistent with any deliberate credential changes.
This uses Keycloak's supported offline
[bootstrap/recovery procedure](https://www.keycloak.org/server/bootstrap-admin-recovery); ordinary
startup flags cannot recreate the initial account once `master` exists.

## Acceptance checks

`scripts/security-smoke-test.sh` exercises an isolated fresh installation, repeated provisioning,
browser and machine grants, database/network isolation, startup import skip, a policy update
that removes an obsolete audience/excess grant, no-op reapply, preservation
of unrelated objects/users/keys/credentials, both secret rotations and server/database recreation.
It simulates an existing pre-Inventory realm with empty business composites and no list/manage/history
roles, then applies the current policy and checks preservation. It also changes declarations for a new permission, service/fixture assignments, scope mappings,
role composites, realm defaults, profile settings and realm metadata, then restores the baseline.
It also upgrades an existing Inventory-enabled realm to Pricing's new roles/composites/scope
mapping and verifies no-op reapplication and preserved subjects, client IDs, keys and credentials.
It also upgrades an existing Pricing-enabled realm with Reservation's eight roles, composites
and scope mapping, preserving identities, keys and credentials. Its temporary credentials and
volumes are deleted on exit. The full combined smoke invokes the same checks before existing business/observability flows and final stack recreation. Automated
host checks use explicit loopback resolution for the canonical hostname; container checks use its
Compose DNS alias, and Chromium uses its own hosts mapping. No system hosts file is modified.

Run `./scripts/security-smoke-test.sh --inventory` for Inventory's C1 handoff. This runs the
foundation checks and builds the real Inventory image with isolated application PostgreSQL/Kafka.
A private temporary Compose override supplies only its issuer/JWKS settings; base `compose.yaml`
is unchanged. Browser Code + PKCE tokens from both approved human clients perform staff setup,
customer reads/lists and the direct API matrix. Real Client Credentials tokens prove Pricing
lookup-only and Reservation all-RESERVED restrictions. Denied writes preserve item/history state;
same-key replay is isolated across identities, stable across approved human clients and renewed
tokens, and denied after a fresh token loses permission. Fixture grants are restored afterward,
and subjects, signing keys and credential files are checked for preservation.

Public metadata checks use request-derived root/path URLs, the exact issuer, header bearer support
and no certificate binding. There is no Inventory public-origin variable. The test deletes only
its own project/volumes and temporary credentials, even on failure; it never applies policy to the
developer realm. Inventory's V5 ledger migration is exercised on fresh smoke storage; existing-data
migration and rollback evidence belongs to Inventory's implementation acceptance record.

Run `./scripts/security-smoke-test.sh --pricing` for Pricing's C1 handoff. It includes the
Inventory checks, actual Pricing/Inventory images and Redis. A temporary test override mounts
only Pricing's recipient copy, with UID 10001 ownership and mode 0400; the original host secret
is preserved. A test-only observer forwards requests to the secured Inventory image, verifies
the real provider signature and records only token digests, expiry and contract booleans in
memory. It stores no bearer values, request bodies or URLs. Its cached public keys let it keep
observing during a real Keycloak stop without adding an artificial provider dependency.

The runner checks both approved Pricing browser clients, both customers' four reads/write denial,
staff CRUD and cache authorization, and the actual outbound machine identity with no user-token
relay. It temporarily elevates only a managed smoke customer to prove token reuse across human
subjects. An isolated per-client 45-second token lifetime makes the application's 30-second
renewal boundary observable; normal policy remains 300 seconds. Wrong audience and missing
Inventory permission exercise real 401/403 responses, eviction versus retention, and no immediate
retry. A wrong recipient copy exercises rejected credentials without changing the provider's
secret. Stopping the isolated Keycloak proves cached-token operation, failed renewal, continued
local reads/readiness and recovery. PostgreSQL rows and Redis values are compared around denied
writes; natural TTL countdown is excluded from value comparisons.

A temporary loopback control server accepts only a private random capability held by the test
runner. Its fixed actions restart only smoke Pricing, stop/start only smoke Keycloak and read
smoke data state. It accepts no shell commands or credential values. All fixture grants and
client attributes are restored, and snapshots verify subjects/keys/original secrets unchanged.
The helper server, observer, temporary recipient/capability files and project resources are
removed on exit. The developer realm and stack are untouched.

Run `./scripts/container-smoke-test.sh` for Reservation C1 and the full combined gate. The runner
keeps real browser sessions in a single-threaded, capability-protected loopback proxy. It accepts
only configured smoke application origins, selects the correct API audience and fixture, and
keeps tokens in memory. Ordinary curl business calls and observability probes go through it.
The helper is restricted to `rentflow-common-smoke`/`rentflow-smoke`, survives the retained-volume
stack recreation and is explicitly removed on exit. It is not part of normal application startup.

Reservation checks verify token-derived owners, customer page totals and foreign 404s, staff
self-owned creation/any-cancel/immutable replacement, same-key cross-subject isolation and replay
across approved clients. Reduced create permission and deleted rows deny replay. The actual
Inventory observer verifies derived keys, reserve-only machine identity, token reuse/renewal,
401 eviction and 403 retention. Invalid recipient files and a real Keycloak stop exercise safe
502/503 handling with unchanged business/replay/outbox state and subsequent recovery. The runner
restores provider policy, source credentials and direct Inventory wiring afterward. Existing
cancellation/Kafka release, expiry, Pricing cache, outages, observability and persistence checks
continue against real secured applications.

The full gate must print `Combined container smoke verification passed`; individual service
messages do not substitute for it. The isolated Reservation run uses fresh storage and exercises
the consolidated V6. Existing databases need the separately managed cutover described in
[README](README.md): stop/drain writers and workers, back up and reconcile outstanding Inventory
claims before the destructive development migration. Common never repairs Flyway history or
resets developer storage. No identity mapping table is created.

Real-provider signing-key rotation and expanded operational acceptance remain Phase 5.
Production exposure remains gated on TLS, infrastructure security and Phase 7 hardening.
