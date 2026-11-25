# Common security design: Phases 0 and 1

Design date: 2026-10-30. Common implementation authorized: 2026-10-31.
Status: Common Phases 0 and 1 implemented and verified on 2026-10-31.

This document defines Common's shared security contract and the local
Keycloak foundation. The user authorized Common implementation on 2026-10-31. Sibling services
retain their own implementation phases. Inventory has now implemented Phase 2; its provider
handoff is recorded below. Pricing has also implemented Phase 3; Common's Pricing handoff is
described below. Reservation has implemented Phase 4 with direct immutable issuer/subject
ownership; Common now wires all three secured services and their real-provider acceptance.

The user selected `auth.rentflow.test` for host/container networking and Swagger UI plus a scripted
browser-based PKCE verification flow. The implementation follows the defaults below, including
dedicated identity PostgreSQL. See [the operating guide](SECURITY_WALKTHROUGH.md) for actual commands.

## 1. Scope and current baseline

Common owns identity infrastructure, realm policy, local fixtures, secret delivery conventions,
documentation and real-provider interoperability tests. There is no shared Java security module.
Service repositories own HTTP authorization, ownership, authority conversion and OAuth client code.

The existing stack has PostgreSQL 18.4, Kafka, Redis, three applications and optional observability.
Applications use separate accounts/schemas within the `rentflow` database. Management listeners
are private to Docker. Inventory now enforces bearer authentication and its approved permission
matrix. Pricing now enforces its own HTTP policy and acquires Client Credentials for Inventory.
Reservation enforces its own/any permission matrix and authenticates Inventory claims.

Phase 1 adds identity infrastructure without application dependencies, security filters, Maven
Enforcer changes, outbound token forwarding or customer mapping. Swagger client registrations are
prepared here; each service wires its Swagger UI when it adopts security.

## 2. Phase 0: environment and issuer contract

| Environment | Realm | Exact issuer | Persistence and credentials |
| --- | --- | --- | --- |
| Local development | `rentflow-local` | `http://auth.rentflow.test:8180/realms/rentflow-local` | Developer-owned identity volume and generated local secrets |
| Isolated combined smoke | `rentflow-smoke` | `http://auth.rentflow.test:18180/realms/rentflow-smoke` | Smoke-project volume and temporary secrets; removed on exit |
| Future deployed environments | `rentflow-<environment>` | Explicit environment-specific HTTPS URL | Separate keys, users, credentials and storage; deployment design required |

On the host, add `127.0.0.1 auth.rentflow.test` to the hosts file during implementation setup.
Inside each Compose project, assign that name as a network alias to its own Keycloak container.
Keycloak listens on the same HTTP port inside and outside Docker: `8180` for development and
`18180` for smoke. A configurable port changes both sides and the full issuer URL together.
This avoids advertising a host port that containers cannot use.

Set `KC_HOSTNAME` to the full origin, such as `http://auth.rentflow.test:8180`, and keep
`KC_HOSTNAME_BACKCHANNEL_DYNAMIC=false`. Host and container clients use the same discovery,
authorization, token and JWKS URLs. No alternate accepted issuer or `localhost` substitution is
allowed. This is our proposed use of Keycloak's
[fixed hostname configuration](https://www.keycloak.org/server/hostname).

For a configured issuer `I`, the endpoint contract is:

| Purpose | URL |
| --- | --- |
| Discovery | `I/.well-known/openid-configuration` |
| Authorization | `I/protocol/openid-connect/auth` |
| Token acquisition | `I/protocol/openid-connect/token` |
| Public signing keys | `I/protocol/openid-connect/certs` |

Realm names distinguish environments, but isolation also requires distinct signing keys. Never
copy a developer database, private signing keys or credentials into smoke or production. The
`master` realm is administrative and is never an application issuer.

## 3. Phase 0: token and authority contract

These are target settings that Phase 1 must verify against real issued tokens before Inventory
depends on them in Phase 2. Services implement the corresponding validators in Phases 2–4.

| Item | Contract |
| --- | --- |
| Credential | Signed JWT **access token**, sent as `Authorization: Bearer ...` |
| Signing | `RS256` only initially; trusted issuer JWKS; reject unsigned tokens and other algorithms |
| Token profile | Keycloak emits JOSE `typ=JWT` and payload `typ=Bearer`; Inventory accepts the required JOSE type ignoring case, while payload type remains exact |
| Required claims | Nonempty `iss`, `sub`, `azp`; `aud`; numeric `iat` and `exp`; payload `typ=Bearer` |
| Issuer | Exact configured environment issuer |
| Audience | The receiving API's own client ID must be a member of `aud`; normalize string/array forms |
| Time validation | Expiry and `nbf` when present; proposed clock skew `30s`; synchronized host clocks |
| Access-token lifetime | `300s` for human and machine clients |
| Calling client | Verified `azp`; it does not replace audience or permission validation |
| Human identity | Verified `(iss, sub)`; never email, username or request `customerId` |
| API permissions | Only approved roles in `resource_access[own-api-client-id].roles` |
| Spring authority name | `PERM_` plus the exact permission, e.g. `PERM_inventory:reserve` |
| OAuth scopes | Remain `SCOPE_...`; requesting a scope never substitutes for a permission |

ID tokens have the interactive client as audience and must not pass the API audience/type
contract. Refresh tokens must also fail API validation. Do not switch to an RFC 9068 token profile
or accept additional token types implicitly; that would require a contract update and tests.

`CUSTOMER` and `STAFF` are realm-level business roles. They do not directly become Spring
authorities. Approved service client roles will be composed into these business roles as each
service design is agreed. Inventory and Pricing's implemented specifications define these composites:

| Business role | `inventory-api` client roles | `pricing-api` client roles |
| --- | --- | --- |
| `CUSTOMER` | `inventory:read`, `inventory:list` | `pricing:read`, `pricing:list` |
| `STAFF` | `inventory:read`, `inventory:list`, `inventory:manage`, `inventory:history` | `pricing:read`, `pricing:list`, `pricing:manage` |

Staff receives its own explicit set without inheriting CUSTOMER. Neither role contains
`inventory:reserve`. Reservation adds exactly these independent client roles:

| Business role | `reservation-api` client roles |
| --- | --- |
| `CUSTOMER` | `reservation:create`, `reservation:read`, `reservation:list`, `reservation:cancel` |
| `STAFF` | `reservation:create`, `reservation:read:any`, `reservation:list:any`, `reservation:cancel:any`, `reservation:manage` |

Both create for themselves with `reservation:create`; there is no `reservation:create:any` grant.
STAFF does not inherit CUSTOMER, and manage grants only PUT/DELETE. `reservation-access` allows
exactly those eight roles for `reservation-swagger` and `rentflow-pkce-check`, filtered by held roles.
Reservation uses verified `(iss, sub)` directly: response customerId is the opaque subject, with
issuer stored internally on each row. POST/PUT owner fields are rejected; there is no mapping
table, registration step, trusted customerId token claim or provider lookup. Staff PUT preserves
the original owner/time/deadline, and DELETE neither releases Inventory nor emits cancellation.
The `inventory-access` scope allows exactly the four human Inventory roles and maps only roles
the authenticated identity holds. Both `inventory-swagger` and `rentflow-pkce-check` use that
scope; no realm-role mapper or broad default scope is added.
The `pricing-access` scope similarly allows exactly Pricing read/list/manage for `pricing-swagger`
and `rentflow-pkce-check`. Pricing authorizes only these two human clients, including read-only
batch POST with `pricing:read`; no machine caller receives inbound Pricing privileges. Pricing's
Nimbus decoder accepts numeric subjects after conversion to strings, as explicitly approved in
its specification; Common continues emitting nonblank string subjects and unchanged token types.

Services map an explicit allowlist of their own client roles. Unknown roles, roles belonging to
other APIs and `realm-management` roles produce no local API permissions. Machine-only operations
also require an approved caller identity (`azp`), so an accidental grant to an interactive client
cannot turn it into the Reservation caller.

Logout, disabling a user or removing a role does not immediately revoke a locally validated JWT.
The proposed exposure window is its remaining five-minute lifetime plus up to 30 seconds of clock
skew. Immediate revocation and introspection remain later requirements. Keycloak outages must not
become blanket application liveness/readiness failures; cached-key and startup behavior must be
tested when the services add Spring Security.

## 4. Phase 0: clients, permissions and dependency map

Create three enabled resource registrations: `inventory-api`, `pricing-api` and `reservation-api`.
They identify audiences and own client roles. Disable all token-issuing flows on them; resource
servers need no secret to validate tokens. Do not pre-create Order registrations.

| Caller registration | Grant | Allowed API audience | Initial API permission | Intended operation |
| --- | --- | --- | --- | --- |
| `pricing-service` | Client Credentials | `inventory-api` only | `inventory:read` | `GET /api/v1/inventory/{serialNumber}` used for existence lookup |
| `reservation-service` | Client Credentials | `inventory-api` only | `inventory:reserve` | Batch claim through `PATCH /api/v1/inventory/status`, targeting `RESERVED` only |

These two permission names are the Common machine contract agreed for this implementation.
Inventory implements these operation rules in Phase 2. `inventory:read` authorizes the single-item
lookup only; listing requires `inventory:list` and history requires `inventory:history`. Catalogue
writes and human status transitions require `inventory:manage`. Inventory also restricts verified
`azp`: only `inventory-swagger` and `rentflow-pkce-check` are approved human clients, Pricing can
only look up an item, and Reservation can only submit all-RESERVED status batches.

`inventory:reserve` must allow only the existing `AVAILABLE -> RESERVED` business transition,
with current atomic-batch and idempotency semantics preserved. It must not allow release to
`AVAILABLE`, rental, retirement, catalogue changes or history access. Inventory restricts the
existing endpoint at both HTTP and service boundaries. Authorization precedes idempotent replay;
V5 scopes saved outcomes and execution locks by verified `(iss, sub)`, preserving identity across
token renewal and approved human-client changes. Legacy unauthenticated outcomes cannot be
replayed by authenticated identities. A URL-level grant alone is insufficient for this status API.

Both confidential callers have separate secrets and service accounts. Standard flow, implicit
flow, direct-access/password grants, device flow and token exchange are disabled. Neither has
`CUSTOMER`, `STAFF`, account-management or realm-management privileges. Inventory has no outbound
HTTP client credential because it currently has no such dependency.

Reservation cancellation and expiry release equipment through Kafka. This creates no additional
HTTP grant. The existing Kafka publisher/consumer trust boundary is unchanged until broker
hardening; HTTP tokens do not secure those messages.

### Audience and role provisioning

Disable Full Scope Allowed on every RentFlow token-issuing client. Use explicitly attached client
scopes with hardcoded audience mappers that add API audiences to **access tokens only**. Add a
client-role mapper constrained by explicit role scope mappings. Remove broad default role/audience
mappers from these clients, and verify effective grants, including composite roles.

Each machine client gets a dedicated default scope for `inventory-api` and only its own permission.
It cannot request scopes for other APIs or the other caller's role. Human client scopes never
include `inventory:reserve`. A scope exposes only permissions the user actually holds; it does not
grant those permissions. Our explicit mapper choice uses Keycloak's documented
[client scopes, role mappings and audience mechanisms](https://www.keycloak.org/docs/latest/server_admin/index.html#_audience).

The expected machine audience set is exactly `{inventory-api}`: no `account`, caller-client or
other RentFlow audience. Account-console defaults must not leak roles into these tokens. API
consumers still validate membership in their own audience, rather than requiring all tokens to
have a single audience.

### Service handoffs

| Owner | Agreed handoff and current status |
| --- | --- |
| Inventory | Implemented read/list/manage/history, verified caller restrictions, Pricing read-only and Reservation all-RESERVED grants, scoped replay, metadata and private management |
| Pricing | Implemented read/list/manage policy, human grants, application-scoped Inventory tokens and safe 502/503 failures; retain cache authorization and recipient restart on rotation |
| Reservation | Implemented exact issuer/subject ownership, scoped creation/replay, eight own/any/manage permissions, reserve-only Inventory calls and no-human-context cancellation/expiry workers |
| Common | Supplies all three service-role composites, isolated test identities, issuer/recipient wiring and real-provider combined acceptance; maintains this contract |
| Future Order | Define ownership and orchestration privileges before introducing its audience or service credentials |

Inventory enforces the machine contract in Phase 2. Pricing implements inbound authorization and
outbound Client Credentials together in Phase 3; its scoped Common checks exercise real Inventory
lookups. Reservation implements the same secured cutover, including ownership, in Phase 4.
Callers never forward the customer's bearer token or trust `X-User-*`
headers. Their token caches must use stable service principals and work without a human request.

Phase 0 establishes this provider contract and dependency map. Inventory's service-side design
must enforce the narrow machine operations before its secured rollout. Full human endpoint
matrices remain assigned above and are not implied by creating realm roles.

## 5. Phase 1: interactive clients and local users

| Public client | Exact development redirect URI | Allowed web origin | API audience |
| --- | --- | --- | --- |
| `inventory-swagger` | `http://localhost:8080/swagger-ui/oauth2-redirect.html` | `http://localhost:8080` | `inventory-api` |
| `pricing-swagger` | `http://localhost:8081/swagger-ui/oauth2-redirect.html` | `http://localhost:8081` | `pricing-api` |
| `reservation-swagger` | `http://localhost:8082/swagger-ui/oauth2-redirect.html` | `http://localhost:8082` | `reservation-api` |
| `rentflow-pkce-check` | `http://127.0.0.1:8765/callback` | None required: the script exchanges the code | One explicitly requested API scope |

All four clients use Authorization Code with required PKCE `S256`, no secret, and no other login
or machine grant. No wildcard redirects/origins, password grant, implicit flow, self-registration,
Remember Me or offline access. Proposed browser SSO idle/max limits are `30m`/`8h`. Keep Swagger
authorization in memory with persistence disabled. Service-side Swagger wiring and exposure policy
belong to Phases 2–4. All three local Swagger UIs are now wired. Phase 1 itself does not edit the service UIs.

Each Swagger client receives one default API audience scope, with only that service's eventual
human permissions in its role scope mapping. The verification client has three optional scopes:
`inventory-access`, `pricing-access`, `reservation-access`; each adds only the matching audience.
The verification script requests exactly one plus `openid`, validates state and the ID-token nonce,
and exchanges the code with its original verifier. Token contents stay in memory.

Changing service ports requires updating exact callback/origin registrations through the policy
update command. `localhost` and `127.0.0.1` are different origins; use the registered form.
Smoke uses its own exact callback registration and port, proposed `18765`, with a loopback-bound
callback listener. A containerized browser runner uses host networking on the current Linux host
so both the issuer's loopback publication and callback are reachable. Its generated hosts mapping
does not modify the host. Other host platforms need a tested equivalent before claiming support.

Create optional local fixtures only through an explicit `fixtures apply` operation:

| Fixture | Assigned business role | Purpose |
| --- | --- | --- |
| `customer-a` | `CUSTOMER` | First distinct human subject |
| `customer-b` | `CUSTOMER` | Second subject for cross-customer ownership-denial checks |
| `staff` | `STAFF` | Inventory/Pricing administration and explicit Reservation any-read/list/cancel/manage; creates only for itself |

Fixtures have generated, independent passwords; no real person, credential or user record goes in
the checked-in realm. Reapplying fixtures preserves subjects and passwords. Password changes
require a separate explicit operation. No default customer/staff role is assigned to new users.
Do not place a trusted `customerId` claim into tokens: Reservation stores the verified subject
directly as customerId alongside its exact issuer. No identity mapping/provisioning exists.
Phase 4 checks cross-customer reads, page totals, cancellation and replay against real tokens.

## 6. Phase 1: runtime and persistence

Pinned image: `quay.io/keycloak/keycloak:26.8.1`, the release shown by the official
[downloads page](https://www.keycloak.org/downloads) at design time, released
[13 October 2026](https://www.keycloak.org/2026/10/keycloak-2681-released). Recheck maintenance/security
status before future upgrades. Compose pins the resolved digest; never use `latest`.

Run one `rentflow-keycloak` container using local-only `start-dev --import-realm`, backed by a
dedicated `rentflow-keycloak-postgres` container using the existing PostgreSQL image version,
`postgres:18.4-alpine`. Keycloak's current
[database support table](https://www.keycloak.org/server/db) includes PostgreSQL 18.

Use database `rentflow_keycloak`, owned by login `rentflow_keycloak`, with a separate bootstrap
database administrator. The Keycloak runtime login has no superuser, database-creation or
role-creation privilege. The database administrator is used only for initialization/maintenance.
Create no Inventory/Pricing/Reservation logins in this PostgreSQL instance.

This dedicated instance costs one small local container but separates identity lifecycle from
application data. The existing `local-postgres.sh reset` deletes the application volume; it must
never remove Keycloak users or keys. A separate project-scoped Keycloak database volume preserves
identity state across server/database recreation. Do not persist a competing development H2 store.

The database has no host port and joins only a private identity-database network. Keycloak joins
that network and the application network; the application containers cannot reach the identity
database network. Test that application credentials cannot authenticate to the identity database.
Keycloak alone receives its runtime database password. Application database administrators remain
privileged within the application database; broader SQL hardening remains Phase 7.

Publish Keycloak HTTP only on `127.0.0.1`. Enable health and metrics, retaining the management
interface on unpublished port `9000`. Use a bounded Bash TCP HTTP readiness probe inside the
image, since it has no curl. Enabling metrics also enables the database-pool health check.
See [Keycloak health checks](https://www.keycloak.org/observability/health). Proposed probe timing:
`5s` interval, `3s` timeout, `120s` startup allowance and `12` retries. Test the returned HTTP
status; an open TCP port alone is insufficient.

Keycloak depends on a healthy identity database. Applications gain no Keycloak `depends_on` or
health dependency in Phase 1. The foundation starts with normal Compose startup; existing
observability remains optional. Do not opt Keycloak into Alloy log collection until its fields
and login-event content have been reviewed. Phase 1 does not add identity dashboards or scrape jobs.

This is a local development topology: HTTP, development mode and local administrative access
must not become a production profile. Production TLS, proxy trust, operational MFA, backups,
recovery, database hardening and supported deployment sizing remain Phase 7 requirements.

## 7. Phase 1: secret delivery and administration

Store generated development secrets under ignored `.secrets/security/`, directory mode `0700`,
files mode `0600`. Check in only a variable/file-name example with no usable values. Do not
overwrite existing `.env` or secret files during setup. Compose secrets mount selected files;
they are local file delivery, not an encrypted secret manager.

| Secret | Recipients |
| --- | --- |
| Identity PostgreSQL bootstrap-admin password | Identity database initialization/maintenance only |
| Keycloak runtime DB password | Identity database initialization and Keycloak only |
| Temporary bootstrap admin client secret | First initialization/recovery tooling and Keycloak bootstrap only |
| Realm maintenance client secret | Explicit policy/fixture maintenance tooling only |
| Local operator password | Human administrator only |
| Pricing client secret | Provisioning tooling and Pricing's protected recipient config-tree file only |
| Reservation client secret | Provisioning tooling and Reservation's protected recipient config-tree file only |
| Fixture passwords | Local fixture/browser verification tooling only |

A small Keycloak entrypoint reads mounted secret files into the supported configuration variables
and `exec`s Keycloak. Do not assume generic `KC_*_FILE` support. No secrets in image layers,
checked-in realm JSON, command-line arguments, Compose interpolation output, shell history,
trace attributes or ordinary logs. Tools read secret files directly and submit credentials in
request bodies; disable shell tracing and redact HTTP error bodies. Do not print token responses.

Use a temporary bootstrap **admin service account** in `master` and Client Credentials for initial
automation. Keycloak supports this
[bootstrap mechanism](https://www.keycloak.org/server/bootstrap-admin-recovery). It creates the local
operator and `rentflow-realm-maintainer` in the RentFlow realm, then removes the temporary account
and its mounted bootstrap credential from normal startup. The maintainer has only the realm's
administrative privileges, no master/global administration or RentFlow API permissions; it is not
one of the two business callers. Business staff receive no administrative privileges.

Routine policy/fixture updates use the maintenance client's Client Credentials token. Bootstrap
must not leave a permanent globally privileged automation credential. Document offline recovery
with all Keycloak nodes stopped; never silently recreate a deleted administrator during ordinary
startup. The human operator is distinct from the staff fixture.

Keep bootstrap mounts/options in a generated, temporary Compose override used only by the
explicit `bootstrap` operation. That operation starts an empty installation, establishes the
realm operator/maintainer and business credentials, removes the temporary admin, then recreates
Keycloak with normal configuration. Verify maintenance access before removing bootstrap access.
If interrupted, inspect the recorded initialization stage and resume explicitly; never replace
existing credentials or reset the realm to recover. Normal `start` reports incomplete setup.

Provision the two business client secrets from independent generated files during initial
credential setup. Later policy updates do not rotate them. An explicit rotation operation updates
one Keycloak client and its recipient file, restarts only the affected caller when applicable,
and verifies new acquisition plus failure with the old secret. For this local single-secret
design, schedule a brief acquisition interruption; already issued access tokens remain valid
until expiry. Production overlap/zero-downtime rotation is a later operational design.

Both callers import `/run/secrets/spring.security.oauth2.client.registration.inventory.client-secret`
through Spring config-tree delivery; its required property has no fallback secret. Only this
business credential reaches its intended caller, never the maintainer/other caller credentials. Common's
source secret remains host-owned mode 0600. Container delivery needs a separate recipient copy
with a UID-10001 read ACL or ownership; do not add group/ACL bits to the original file that Common's
owner-only checks protect. `bootstrap` delivers UID-10001 mode-0400 copies using a network-disabled
container that mounts only the selected copy. Existing setups use `recipients apply`; rotation
refreshes only its affected copy. Recreate only that caller to load the replacement file.
IDE runs can use each caller's optional local `.secrets/ide/` import with a user-owned mode-0600
property file or symlink to Common's source secret. No credential values enter environment
variables, command arguments or checked-in configuration.

## 8. Phase 1: initial import and repeatable updates

Keep a secret-free baseline in `security/keycloak/rentflow-realm.template.json`. Render only
non-secret environment values into an ignored runtime directory as `<realm-name>-realm.json`,
and mount it read-only into `/opt/keycloak/data/import`. The baseline contains realm settings,
resource/public/business-caller registrations, initial roles, mappers and scopes, with no people
or passwords in the rendered import. Native `users` entries declare service-account roles.
The source template's `_rentflow` extension declares user-profile settings, credential-file
references, the local operator and optional fake fixtures. The renderer strips that extension
before import. Initialize business credentials and local-user creation through explicit tooling.

Role definitions/composites, realm/client role scope mappings, service-account grants and realm
default scope lists are read from the template during both initial provisioning and updates.
Python reconciles those declarations rather than choosing service-specific permissions. Existing
managed local users receive their declared roles on policy apply; missing optional fixtures are
created only by `fixtures apply`. The separate verification code retains independent expectations
for the agreed token/permission contract.

Startup import initializes a missing realm and skips an existing one. Editing the template does
not update a developer's realm. Keycloak documents these semantics in its
[import/export guide](https://www.keycloak.org/server/importExport).

Use a Common-owned Admin REST API tool to apply the current desired policy directly:

1. Acquire a short-lived maintenance token without logging it. Verify the exact target origin,
   realm and expected environment before mutation.
2. Compare the owned realm/client/role/scope fields and present a redacted plan. Stable names
   locate objects; preserve their Keycloak IDs. The baseline defines current policy for fresh
   installs and the desired settings for existing realms.
3. Apply missing objects and deliberate changes, including explicit removal of obsolete owned
   grants/mappers. Preserve users, subjects, passwords, signing keys, sessions, client secrets and
   unrelated objects. Credential rotation and fixture removal are separate operations.
4. Read back effective policy. A second apply must produce no changes. Token/login checks run
   only through explicit `verify`/`verify-browser` commands or the smoke suite.
5. On partial failure, return nonzero with a bounded diagnostic. Operations can be rerun to
   converge; the Admin API is not a multi-object transaction. Serialize updates per environment.

There are no policy migration files, version markers or upgrade-path checks. The user chose this
simpler workflow on 2026-11-01. Existing realm version attributes, if present, are ignored.

Treat manual changes to Common-owned fields as drift: `plan` shows them and explicit `apply`
reconciles them. Unknown objects are preserved. Never use realm deletion, import overwrite or
database-volume deletion as a routine update. Before a change that cannot be safely reversed,
take a database backup and rehearse restore. Realm exports do not replace database backups.

Test an actual policy change on an existing realm, not just a second no-op import: retain a user
and signing key, change an owned mapper/grant, apply the current desired policy, and verify new
tokens plus preserved identity state.

A Keycloak binary upgrade is separate from a realm policy update. Read the target release's
upgrade guide, stop identity writers, take a restorable database backup, rehearse against a copy,
then update the image pin and rerun foundation/combined smoke checks. Rollback restores the
compatible image and database backup together; do not downgrade an already migrated database
in place. Production backup/restore operations remain a Phase 7 workstream.

## 9. Phase 1: files and local commands

These implementation deliverables are available in this repository.

| Artifact | Responsibility |
| --- | --- |
| `compose.yaml` | Keycloak, dedicated identity PostgreSQL, private network, secret mounts and persistent volume |
| `security/keycloak/` | Secret-free realm template, environment rendering and entrypoint |
| `security/postgres/` | Dedicated identity database/user initialization |
| `.env.security.example`, `.gitignore` | Non-secret settings and exclusions for generated secrets/runtime data |
| `scripts/local-keycloak.sh` | Consistent lifecycle wrapper following existing local infrastructure scripts |
| `scripts/security/` | Admin policy tool, explicit fixtures/rotation and browser PKCE/token checks |
| `scripts/security-smoke-test.sh` | Isolated foundation checks; `--inventory` adds Inventory interoperability; `--pricing` adds actual Pricing-to-Inventory acceptance |
| `scripts/security/inventory_smoke.py`, `inventory_check.py` | Isolated Inventory issuer/JWKS injection and real-token HTTP acceptance |
| `scripts/security/pricing_smoke.py`, `pricing_check.py`, `pricing_observer.py` | Protected recipient delivery, actual Pricing/Inventory image checks, in-memory machine-token observations and scoped failure/renewal/outage control |
| `scripts/security/combined.py`, `smoke_proxy.py` | Capability-protected loopback proxy with real PKCE sessions retained in memory for combined HTTP checks |
| `scripts/security/reservation_smoke.py`, `reservation_check.py` | Actual Reservation ownership/replay, scoped machine-key and dependency failure acceptance, with fixed isolated control actions |
| `scripts/container-smoke-test.sh` | Supply smoke realm/ports/temporary files, run foundation/Reservation checks and existing business/observability checks |
| `README.md`, security walkthrough | Setup, issuer/claims learning exercise, updates, recovery and phase limitations |

Wrapper operations are `init`, `bootstrap`, `start`, `stop`, `status`, `logs`, `policy plan`,
`policy apply`, `fixtures apply`, `recipients apply`, `verify`, `verify-browser`, and `rotate-client <client-id>`. `init` creates missing
local secrets and renders configuration; `bootstrap` performs explicit first-time provisioning;
`start` waits for health and detects policy drift; `stop` preserves identity data. Repeated starts
preserve user subjects, credentials and keys. No implicit fixture creation, password change or
policy mutation on ordinary restart. Initial local setup is `init`, `bootstrap`, optional
`fixtures apply`, then `verify`; later starts use `start`.

Do not add a reset operation in this increment. Document destructive cleanup separately with the
exact project/volume target. The smoke teardown removes only its own resources, including its
temporary administrative credentials and fixture files.

## 10. Phase 1: acceptance evidence

Extend the isolated `rentflow-common-smoke` project; never use the developer's running realm or
credentials. Use its own issuer port, realm, secrets, keys and identity volume. Pin browser/JWT
test dependencies during implementation, and keep them in the test runner. Existing host
requirements for the combined smoke should remain Docker Compose, curl and Python 3.

| Check | Required evidence |
| --- | --- |
| Networking | Host and application-network container resolve the configured hostname; discovery and tokens report the same exact issuer; advertised token/JWKS URLs work in both locations |
| Fresh setup | Empty identity volume becomes healthy; baseline policy, credentials and explicit fixtures provision successfully |
| Database isolation | Runtime DB login is unprivileged; application credentials cannot connect to the identity DB; identity DB has no host port/application-network attachment |
| PKCE success | Real browser login for both customers and staff; Code + `S256`; callback state and nonce checks; real token signature/issuer/audience/type/time validation |
| PKCE denials | Unregistered redirect, missing PKCE, plain challenge, wrong verifier and code reuse fail; local callback rejects wrong state |
| Human clients | Every Swagger registration has its own audience and exact callback/origin; all approved tools expose only the requested API's held CUSTOMER/STAFF permissions; no human inventory:reserve or machine/admin roles |
| Machine tokens | Both callers get exactly `inventory-api`; Pricing has only `inventory:read`, Reservation only `inventory:reserve`; distinct `sub`/`azp`; no business/admin roles or refresh tokens |
| Restricted grants | Wrong secret fails; caller cannot obtain another API audience/permission by requesting scopes; prohibited grants fail; resource registrations cannot issue tokens |
| JWT checks | Test verifier rejects ID/refresh tokens, tampering and wrong audience/issuer; decoded claims alone never count as verification |
| Persistence | Restart/recreate Keycloak and its database with retained volume; users, subjects, keys and credentials persist; fresh authentication still works |
| Existing-realm update | Startup import leaves existing policy unchanged; explicit policy apply changes intended settings, preserves identities/keys/secrets, and a second apply is a no-op |
| Credential rotation | Rotate each business client in isolation; new secret works, old acquisition fails, other caller is unaffected; no credential output |
| Regression | All current cancellation, expiry, Pricing cache and observability smoke checks still pass |

The browser runner must disable screenshots, videos, traces, HAR capture and credential-bearing
diagnostics. Keep tokens/verifiers in memory, suppress token endpoint bodies on failure, and
report assertion names plus approved non-secret claim summaries. Do not log callback queries
containing authorization codes. Use browser form login rather than enabling password grant for
test convenience. Include a manual walkthrough for Swagger wiring once the service phases land.

All three API audiences now carry their approved CUSTOMER/STAFF permission sets. The foundation suite proves provider
configuration and login; `--inventory` additionally proves real-token Inventory HTTP denials,
machine restrictions and authorization before replay. `--pricing` adds actual Pricing-to-Inventory
authentication, reuse/renewal, protected file delivery, failure mapping and denied-write state
preservation. The complete combined suite adds Reservation ownership/replay, real machine claims,
credential/permission/outage failures and existing cancellation/expiry/observability regressions.
Broader real-provider signing-key rotation and operational acceptance remain Phase 5 gates.

Whenever implementation changes `compose.yaml`, run `./scripts/container-smoke-test.sh` from
Common's root and require **`Combined container smoke verification passed`**. A standalone
Keycloak test does not satisfy Common's repository invariant. The isolated security suite is
invoked by the combined suite; actual results must include both provider and regression checks.

## 11. Integrated rollout and remaining work

Inventory, Pricing and Reservation have implemented their service security phases. Common now
supplies the canonical issuer/JWKS/token URLs, each caller's own UID-readable config-tree file,
all approved human composites and the unchanged narrow machine grants. No Keycloak health or
startup dependency is added. All three normal local Swagger clients use PKCE.

Common's combined runner uses real PKCE sessions through a loopback, capability-protected HTTP
proxy. It injects only the selected fixture's correct audience token into isolated application
requests, retaining access/refresh tokens in memory. Existing curl requests and observability
API requests use this proxy; health/metadata retain their public rules. Staff setup uses explicit
staff grants, and Reservation creates its own token-derived owner rather than accepting a
customer ID. The proxy is test-only and accepts only the three configured loopback app origins.

Reservation C1 checks real subjects, owner-scoped totals/get/cancel, immutable staff replacement,
self-owned staff creation, same-key subject isolation, approved-client replay and permission/deleted
row denials. It verifies deterministic Inventory keys, actual reserve-only credentials and token
reuse/renewal, invalid recipients, 401 eviction, 403 retention and provider outage/recovery. The
existing cancellation/outbox/expiration, cache, persistence and full observability checks remain
required. Phase-specific messages never replace `Combined container smoke verification passed`.

Reservation's consolidated V6 clears development reservations, cancellation outbox and replay
rows; it creates no mapping table. Stop/drain old writers/workers, back up, and reconcile outstanding
Inventory claims before a separately managed cutover. Deleting rows does not release equipment.
Databases containing superseded V6/V7 history need separately authorized recreation, not Flyway
repair or validation bypass. Common acceptance runs only on fresh disposable databases and
never resets a developer database or mutates its realm.

Phase 5 expands integrated signing-key rotation/operational evidence; Phase 6 covers future Order;
Phase 7 remains required before production exposure. Local HTTP/dev-mode security and passing
application tests do not supply production hardening or authenticate Kafka publishers.

## 12. Verification evidence — 2026-10-31

- `./scripts/container-smoke-test.sh` completed successfully and reported
  **`Combined container smoke verification passed`**. This included the isolated Keycloak suite
  and the existing application, cancellation/expiry, cache, outage/recovery and observability flows.
- `KEYCLOAK_SMOKE_PORT=28180 KEYCLOAK_SMOKE_PKCE_PORT=28765 ./scripts/security-smoke-test.sh`
  completed successfully and reported **`Keycloak foundation smoke verification passed`** with
  the final policy read-back and credential-rotation recovery implementation.
- Real Keycloak tokens verified the expected RS256 issuer/audience/type/identity/permission
  contract. Real Chromium browser logins covered three fixture subjects and three API audiences,
  plus PKCE/state/nonce and token-type denial checks. No API enforcement gate from a later phase
  is claimed by these provider tests.
- Fresh and existing-realm setup, interruption-safe policy reconciliation, no-op reapply,
  preservation of unrelated objects/subjects/keys/credentials, both caller secret rotations,
  database/network privileges and retained-volume recreation passed in isolated projects.
- Shell/Python/JSON syntax, local documentation links and whitespace checks passed. Test-owned
  containers, volumes and temporary credential files were cleaned up; regular developer state
  and system hostname configuration were not modified.

## 13. Policy simplification — 2026-11-01

Removed migration descriptors, policy version markers, upgrade-path checks and artificial
migration-interruption testing at the user's request. `policy plan` compares current settings
with the desired policy; `policy apply` updates them and reads them back. Full token/login checks
are explicit commands and remain part of the smoke suite, rather than running on every apply.

The isolated security suite completed successfully and reported
**`Keycloak foundation smoke verification passed`**. It verified fresh setup, browser/machine
tokens, existing-realm updates, no-op reapply, preservation of identities/keys/credentials,
secret rotation and persistence. Python/JSON syntax, local documentation links and whitespace
checks also passed. Compose configuration was unchanged by this simplification.

## 14. Declarative policy — 2026-11-03

Moved service-account grants, maintenance scope mappings, role composite settings, user-profile
settings and local-user declarations into the realm template. Native Keycloak fields are used
for service accounts and role mappings. The `_rentflow` extension holds local-tool declarations
and is stripped from the rendered startup import. Python reconciles the declared policy;
independent acceptance expectations remain in the verification code.

The isolated security suite reported **`Keycloak foundation smoke verification passed`**.
It verified fresh import, optional fixtures remaining absent before explicit creation, browser
and machine tokens, policy updates and no-op reapply, credential rotation and persistent storage.
An additional integration scenario changed template declarations and verified service-account
grants, realm/client role scope mappings, composites, nonempty realm defaults, profile settings
and existing fixture-user roles. User IDs, passwords and client credentials were preserved.
Compose configuration and the regular developer realm were unchanged by this refactor.

## 15. Inventory implementation handoff (C1) — 2026-11-12

Reviewed Inventory's [design](../rent-flow-inventory/.specs/security/design.md),
[Common task C1](../rent-flow-inventory/.specs/security/tasks.md#c1--common-policy-and-real-provider-interoperability-handoff)
and [regression test entry points](../rent-flow-inventory/.specs/security/tasks.md#regression-test-entry-points),
including the final native-metadata, typed authorization and annotation-owned OpenAPI amendments.
Added list/manage/history roles, approved CUSTOMER/STAFF composites and the human Inventory scope
mapping. Exact Pricing read-only and Reservation reserve-only audience/grant contracts are retained.
No extra public-origin configuration or changes to Inventory's runtime policy are needed.

Verification executed:

- `KEYCLOAK_SMOKE_PORT=28180 KEYCLOAK_SMOKE_PKCE_PORT=28765 ./scripts/security-smoke-test.sh --inventory`
  completed with **`Keycloak foundation smoke verification passed`** and
  **`Inventory security interoperability verification passed`**.
- The foundation suite covered fresh provisioning, real Code + PKCE through both approved human
  clients, exact human/machine grants, update from the old empty-composite policy, no-op reapply,
  identity/credential preservation, rotation and persistence. Pricing/Reservation human audience
  tokens still contain no API roles.
- The actual Inventory image accepted browser-authenticated staff setup/management, both customer
  reads/lists, Pricing lookup and Reservation claims. It rejected missing/malformed/wrong-audience
  and ID/refresh credentials, customer administration/history, prohibited machine operations and
  Reservation non-RESERVED/mixed targets. Denied writes left items/history unchanged.
- Replay checks verified authorization before saved results, equal-key isolation between staff and
  Reservation, stable replay across approved human clients and token renewal, and denial of a
  freshly reduced-permission staff token. The test restored fixture grants and verified preserved
  subjects, owned client IDs, signing keys and credential files afterward.
- Public root/trailing/path-suffixed resource metadata matched the effective request URL and exact
  issuer/header/certificate fields. Probes remained public and application-listener management
  access remained denied. All provider tokens stayed in runner memory.

The suite injects Inventory issuer/JWKS through a private temporary test override. Base
`compose.yaml`, the running developer realm and sibling repositories were not changed. This is
the C1 real-provider gate, not final combined acceptance: Pricing and Reservation application
workflows remain pending Phases 3/4, real-provider operational expansion remains Phase 5 and
production hardening remains Phase 7. Inventory's own historical acceptance record remains in
its repository; this section records the separately requested Common evidence.

## 16. Pricing implementation handoff (C1) — 2026-11-17

Reviewed Pricing's [requirements](../rent-flow-pricing/.specs/security/requirements.md),
[design](../rent-flow-pricing/.specs/security/design.md),
[C1 task](../rent-flow-pricing/.specs/security/tasks.md#c1--apply-common-policy-and-verify-actual-pricing-interoperability)
and [verification evidence](../rent-flow-pricing/.specs/security/tasks.md#verification-evidence),
including the final subject normalization, audience-entry fix, Boot client registration/storage,
transport properties, request-attribute principal and optional IDE config-tree amendments.
Added pricing:read/list/manage, explicit CUSTOMER read/list and STAFF read/list/manage composites,
and constrained pricing-access mappings. Inventory composites and both narrow machine grants
are retained. These changes require only Common policy/tooling; no sibling source was edited.

Verification executed:

- `KEYCLOAK_SMOKE_PORT=28180 KEYCLOAK_SMOKE_PKCE_PORT=28765 ./scripts/security-smoke-test.sh --pricing`
  exited successfully with **`Keycloak foundation smoke verification passed`** and
  **`Pricing security interoperability verification passed`**.
- Real PKCE checks covered rentflow-pkce-check, inventory-swagger and pricing-swagger, with exact
  API-specific held roles and no Reservation grants. The foundation suite upgraded an existing
  Inventory-enabled realm to Pricing's new policy, verified read-back/no-op reapply and preserved
  subjects, client IDs, signing keys and original credentials.
- The actual Pricing and Inventory images accepted both customers' four reads, staff CRUD and
  cached single/batch paths. Signed real-provider tokens were rejected for missing/invalid/wrong
  audience and ID/refresh credentials; customer writes and reduced-permission cached reads were
  denied without changing checked PostgreSQL rows or Redis values.
- A transparent test observer verified Pricing's actual outbound signed identity and exact
  inventory-api/inventory:read grant, with no human token or X-User header relay. Token digests
  proved reuse across human subjects and renewal at the configured 30-second skew. A temporary
  45-second machine lifetime accelerated this isolated test and was restored afterward.
- Real Inventory 401 from a wrong audience failed once, evicted the token and recovered on the
  next create after policy restoration. Real Inventory 403 from a missing permission retained
  the token even after the grant was restored; restarting Pricing obtained the corrected token.
  A bad recipient copy returned safe 502 without an Inventory call or write. Stopping only the
  smoke Keycloak allowed cached-token operations, then produced safe 503 when renewal was needed;
  local reads/readiness continued, and creation recovered after provider restart.
- UID 10001 read its single read-only config-tree recipient mount. Source secrets remained
  host-owned and unchanged; no maintainer or other caller credential was mounted into Pricing.
  Tokens remained in runner/proxy memory, with only digests/contract booleans retained by the
  observer. The capability-protected control helper accepted only fixed smoke actions.
- Cleanup read-back found no smoke containers, volumes, networks or temporary credential
  directories. The five original development infrastructure containers remained healthy.

Base `compose.yaml` and the running developer realm were unchanged. This is Pricing-scoped
Phase 3 evidence, including the prior Inventory HTTP checks. Reservation Phase 4, the complete
combined-smoke gate and Phase 5/7 integrated operational/production gates remain pending. This
run does not claim **`Combined container smoke verification passed`**. Refresh protected recipient
copies and restart Pricing after explicit credential rotation; no hot reload is promised.


## 17. Reservation implementation handoff (C1) — 2026-11-24

Reviewed Reservation's [requirements](../rent-flow-reservation/.specs/security/requirements.md),
[design](../rent-flow-reservation/.specs/security/design.md),
[C1 task](../rent-flow-reservation/.specs/security/tasks.md#c1--provision-common-policy-and-restore-complete-authenticated-workflows)
and [verification evidence](../rent-flow-reservation/.specs/security/design.md#115-verification-evidence-and-ac-coverage),
including the latest direct issuer/subject ownership revision and consolidated V6 migration.
Common declares exactly the eight Reservation roles and approved CUSTOMER/STAFF composites.
Both use reservation:create for self-owned creation. Inventory/Pricing human policy and both
narrow machine grants are preserved. Base Compose now supplies all three APIs' canonical URLs
and each caller's own read-only config-tree recipient. Bootstrap/explicit delivery and rotation
prepare UID 10001-readable copies while keeping original credentials host-owned and private.

Verification executed from Common's root:

```bash
INVENTORY_PORT=38080 PRICING_PORT=38081 RESERVATION_PORT=38082 \
POSTGRES_PORT=0 REDIS_PORT=0 KAFKA_PORT=49092 \
KEYCLOAK_SMOKE_PORT=38180 KEYCLOAK_SMOKE_PKCE_PORT=38765 \
GRAFANA_PORT=43000 OTLP_HTTP_PORT=44318 ./scripts/container-smoke-test.sh
```

The command exited 0 and reported **`Keycloak foundation smoke verification passed`**,
**`Reservation security interoperability verification passed`** and
**`Combined container smoke verification passed`**.

- Real Code + PKCE covered all three fixture users and API audiences through the verification
  client and all three Swagger registrations. Refresh verified preserved subject/client/audience
  and exact held roles. Existing Pricing-enabled policy upgraded with read-back/no-op reapply,
  preserving identities, keys and credentials. Foundation rotation and persistence gates passed.
- Actual Reservation/Inventory images verified token-derived owners without mapping/provisioning,
  owner-scoped page totals and foreign 404s, staff self-owned creation and any-cancel, immutable
  ownership/creation time/deadline during replacement, and no release from administrative PUT/DELETE.
  Equal external keys remained isolated by issuer/subject; approved-client replay, cancelled-row
  snapshot replay, reduced-create-permission denial and deleted-row denial passed.
- The observer verified actual signed reserve-only machine credentials, deterministic downstream
  UUIDs, reuse across human subjects, renewal and absence of forwarded human/X-User credentials.
  Real wrong-audience 401 evicted without immediate retry; real missing-permission 403 retained
  the cached token. Invalid recipient credentials and provider outage/renewal produced safe 502/503,
  preserved checked business/replay/logical outbox state, and recovered on explicit subsequent calls.
  Both callers proved UID 10001 readability and a single appropriate read-only credential mount.
- Authenticated cancellation/outbox/Kafka release and duplicate suppression, HELD expiry/CONFIRMED
  exclusion, Pricing cache, telemetry shutdown, application/database/Redis recovery and retained-volume
  recreation passed. Metrics, ECS logs, HTTP/JDBC traces, cross-service trace continuity, durable
  outbox origin links, exemplars, dashboards and alerts passed before and after recreation.
- Browser tokens remained in runner memory behind the capability-protected loopback proxy. The
  code-reuse denial check uses a separate login: Keycloak intentionally detaches that client session
  on reuse, so it cannot supply later refreshes. See its
  [authorization-code implementation](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/protocol/oidc/grants/AuthorizationCodeGrantType.java#L81).
  Policy read-back enforces 300-second lifetimes; token checks allow one clock tick between Keycloak's
  separate issuance/expiration timestamp reads in
  [TokenManager](https://github.com/keycloak/keycloak/blob/26.8.1/services/src/main/java/org/keycloak/protocol/oidc/TokenManager.java#L1001).
- Cleanup read-back found no smoke containers, proxy container, volumes, networks or private
  credential directories. Python/JSON/shell syntax, local documentation links and whitespace
  checks passed. Developer state and sibling source files were untouched.

This completes Common's requested Reservation C1 gate on fresh disposable storage. Existing-data
cutover for the destructive V6 remains separately managed as documented in section 11. The older
optional observability demo does not acquire bearer tokens; its limitation is documented, while
combined observability acceptance is authenticated. Broader Phase 5 operational/signing-key rotation
and Phase 7 production hardening remain separate gates.
