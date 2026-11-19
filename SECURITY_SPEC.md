# Common security design: Phases 0 and 1

Design date: 2026-10-30. Common implementation authorized: 2026-10-31.
Status: Common Phases 0 and 1 implemented and verified on 2026-10-31.

This is Common's detailed design for Phases 0 and 1 of the
[RentFlow security plan](../SECURITY_PLAN.md). It proposes the shared contract and the local
Keycloak foundation. The user authorized Common implementation on 2026-10-31. Sibling services
retain their own implementation phases. Inventory has now implemented Phase 2; its provider
handoff is recorded below. Pricing has also implemented Phase 3; Common's Pricing handoff is
described below. Reservation application security remains pending.

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
Reservation still needs Phase 4 security and outbound authentication.

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
`inventory:reserve`. Reservation human API permissions remain undefined and ungranted.
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

| Owner | Decisions required before its enforcement phase |
| --- | --- |
| Inventory | Confirm machine permissions/transitions, endpoint matrix, human grants, machine identity checks, idempotency scope, docs/probe/management rules |
| Pricing | Implemented read/list/manage policy, human grants, application-scoped Inventory tokens and safe 502/503 failures; retain cache authorization and recipient restart on rotation |
| Reservation | Ownership and `(iss, sub)` mapping/provisioning, legacy rows, staff-on-behalf policy, order references, replay scope, Inventory auth failures, worker behavior |
| Common | Apply agreed service-role composites and test identities as service specifications become available; maintain this interoperability contract |
| Future Order | Define ownership and orchestration privileges before introducing its audience or service credentials |

Inventory enforces the machine contract in Phase 2. Pricing implements inbound authorization and
outbound Client Credentials together in Phase 3; its scoped Common checks exercise real Inventory
lookups. Reservation does the same, including ownership, in Phase 4; its calls remain temporarily
blocked until that phase completes. Callers never forward the customer's bearer token or trust `X-User-*`
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
belong to Phases 2–4. Inventory and Pricing local Swagger are now wired; Reservation wiring
remains pending. Phase 1 itself does not edit the service UIs.

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
| `customer-b` | `CUSTOMER` | Second subject for future ownership-denial checks |
| `staff` | `STAFF` | Inventory administration/history and Pricing administration; Reservation powers remain pending |

Fixtures have generated, independent passwords; no real person, credential or user record goes in
the checked-in realm. Reapplying fixtures preserves subjects and passwords. Password changes
require a separate explicit operation. No default customer/staff role is assigned to new users.
Do not place a trusted `customerId` claim into tokens: Reservation owns the mapping to its domain
IDs. Phase 1 proves distinct identities; cross-customer API denial is a Phase 4 gate.

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
| Reservation client secret | Provisioning tooling; Reservation only when Phase 4 wires it |
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

Pricing imports `/run/secrets/spring.security.oauth2.client.registration.inventory.client-secret`
through Spring config-tree delivery; its required property has no fallback secret. Only this
business credential reaches Pricing, never the maintainer/other caller credentials. Common's
source secret remains host-owned mode 0600. Container delivery needs a separate recipient copy
with a UID-10001 read ACL or ownership; do not add group/ACL bits to the original file that Common's
owner-only checks protect. Refresh the recipient and restart Pricing after explicit rotation.
IDE runs can use Pricing's optional local `.secrets/ide/` import with a user-owned mode-0600
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
| `scripts/security/inventory_smoke.py`, `inventory_check.py` | Temporary Inventory issuer/JWKS injection and real-token HTTP acceptance, without base Compose changes |
| `scripts/security/pricing_smoke.py`, `pricing_check.py`, `pricing_observer.py` | Protected recipient delivery, actual Pricing/Inventory image checks, in-memory machine-token observations and scoped failure/renewal/outage control |
| `scripts/container-smoke-test.sh` | Supply smoke realm/ports/temporary files, run foundation checks and existing business/observability checks |
| `README.md`, security walkthrough | Setup, issuer/claims learning exercise, updates, recovery and phase limitations |

Wrapper operations are `init`, `bootstrap`, `start`, `stop`, `status`, `logs`, `policy plan`,
`policy apply`, `fixtures apply`, `verify`, `verify-browser`, and `rotate-client <client-id>`. `init` creates missing
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
| Human clients | Each Swagger registration has exactly its own audience mapping and exact callbacks/origins; both approved Inventory human clients expose the exact CUSTOMER/STAFF matrix and no reserve grant; other API tokens remain without permissions |
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

Inventory and Pricing human tokens now carry their approved CUSTOMER/STAFF permission sets.
Reservation audience tests still yield no API permissions. The foundation suite proves provider
configuration and login; `--inventory` additionally proves real-token Inventory HTTP denials,
machine restrictions and authorization before replay. `--pricing` adds actual Pricing-to-Inventory
authentication, reuse/renewal, protected file delivery, failure mapping and denied-write state
preservation. Reservation ownership/application integration remains later work. Full integrated
real-provider key rotation and operational outage coverage remain Phase 5 gates.

Whenever implementation changes `compose.yaml`, run `./scripts/container-smoke-test.sh` from
Common's root and require **`Combined container smoke verification passed`**. A standalone
Keycloak test does not satisfy Common's repository invariant. The isolated security suite is
invoked by the combined suite; actual results must include both provider and regression checks.

## 11. Remaining service work

Common's implemented defaults are the permission names/authority prefix, dedicated identity
database, approved Inventory human composites, 300-second token lifetime, administrative model
and direct policy update procedure described here. Inventory has implemented the machine-operation
contract in Phase 2. Pricing has implemented inbound security and outbound authentication together
in Phase 3. Reservation must do so in Phase 4 to restore its Inventory workflows.

The implementation includes isolated runtime/persistence and secret setup, fresh realm and
administrative provisioning, restricted grants and fixtures, direct policy updates, browser/token
checks, combined smoke integration and [learning documentation](SECURITY_WALKTHROUGH.md).

Common's Phase 2 handoff updates Inventory setup/read checks to use real browser-authenticated
test identities and verifies provisioned machine grants against the secured service, without
changing the caller applications. Pricing's Phase 3 handoff now additionally verifies the actual
caller image, dedicated secret delivery, four customer reads, staff CRUD and Inventory token
reuse/renewal and failures. The next application work is Reservation Phase 4. Track workflows blocked
by unfinished callers explicitly; keep Inventory enforcement intact and do not claim full combined
acceptance during that interval. Pricing integration is covered by Phase 3 checks, and Reservation
integration and the full combined smoke suite in Phase 4. Phase 5 completes integrated acceptance
and operational checks; Phase 6 covers future Order, and Phase 7 infrastructure hardening.

The repository's full combined-smoke requirement for Compose changes remains in force. If that
gate cannot pass during the intermediate rollout, defer affected Common Compose changes to
Phase 4 and use existing environment/secret injection mechanisms for earlier checks. Phase-specific
checks do not replace repository-required acceptance. For deployed environments requiring
uninterrupted workflows, prepare callers before Inventory enforcement or coordinate compatible
service deployments, as the plan requires. The partially secured rollout remains temporary and
must never be described as production-ready.

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
and [implementation acceptance](../rent-flow-inventory/.specs/security/implementation-acceptance.md),
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
and [implementation acceptance](../rent-flow-pricing/.specs/security/implementation-acceptance.md),
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
