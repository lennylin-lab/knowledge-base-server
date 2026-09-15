# Gateway identity and tenant integration

## Goal

Move the knowledge-base system from its current single-user, provider-key
setup to a controlled multi-tenant platform. The work follows the required
dependency order: service-account Gateway integration, provider-secret
ownership, OIDC verification, tenant identity data, tenant resource isolation,
then Keycloak-compatible login and tenant RBAC.

## Background and Confirmed Facts

- `knowledge-base-server` is a Python 3.12/FastAPI service. Its current
  `KB_CHAT_BASE_URL`, `KB_CHAT_API_KEY`, and `KB_CHAT_MODEL` settings construct
  an OpenAI-compatible client in `src/app/llm/models.py`.
- The current `KB_CHAT_API_KEY` is an upstream provider key, not a user
  identity. The server has no request authentication dependency.
- `documents.owner_id` and `chat_sessions.owner_id` are nullable placeholders;
  repositories currently do not filter by owner or tenant. `AgentOperation`
  and `DocumentRevision` also have no tenant boundary.
- The Go Gateway owns model API-key authentication, provider secrets, model
  allowlists, model quotas/limits, routing and LLM audit metadata. It does not
  own knowledge-base document authorization.
- The previous Gateway integration task verified the ordinary completion path
  and recorded that the current Gateway does not functionally forward
  tools/MCP; this is a known capability boundary, not a tenant/auth bug.
- The Gateway repository is public (`lenny-lab/knowledge-base-gateway`). Any
  issue filed there must be minimal and fully sanitized.

## Requirements

### R1. Service-account Gateway integration

Configure the server to call the running Gateway using a Gateway-issued
service API key and public model name through the existing `KB_CHAT_*` settings.
The first integration must be reproducible with a local fake provider and must
keep the server's embedding provider independent.

### R2. Provider-secret ownership migration

Move chat-provider credentials out of the server runtime and into Gateway
runtime/secret injection. The server must only hold the Gateway service key for
chat. Embedding credentials remain server-side until a separate embeddings
Gateway contract exists. Configuration examples and diagnostics must make the
distinction explicit without exposing values.

### R3. OIDC verification entry point

Add a provider-neutral OIDC/JWT verification boundary to the server that can
validate issuer, audience, signature via JWKS, expiry and token type before
constructing a request principal. It must support a Keycloak-compatible issuer
without hard-coding a Keycloak SDK or coupling business services to JWT
parsing. Browser login UI and refresh-token handling are out of scope for this
backend task.

### R4. Tenant identity data

Add `tenants`, `users`, and `tenant_memberships` with migrations, ORM models,
repositories and seed/backfill behavior for the existing single-user corpus.
Membership roles must be explicit and constrained. IDs use immutable provider
subject identifiers for users and stable tenant IDs; email is not a primary
identity key.

### R5. Tenant resource isolation

Add non-null `tenant_id` ownership to documents, chat sessions and agent
operations, and preserve the tenant boundary through document revisions,
messages, indexing/search hydration and agent services. Every read, write,
update, delete, search and operation transition must require the current
principal's tenant scope. Cross-tenant IDs must resolve as not found or
forbidden according to the documented API policy, never leak data.

### R6. Keycloak-compatible login and tenant RBAC

Document and wire Keycloak-compatible OIDC configuration for the server,
including issuer/audience/JWKS settings and claim mapping. Enforce initial
tenant RBAC (`tenant_admin`, `editor`, `member`/`viewer`, `service_account`)
at the API boundary and service boundary. Keycloak deployment may be an
external/infra service; this repository must not implement password storage,
login pages, MFA, token issuance or IdP administration.

### R7. Compatibility and migration safety

Keep the service-account path working while user OIDC is introduced. Existing
single-tenant data must be migrated deterministically into a local/default
tenant. Migrations require real downgrade paths, and the default test suite
must remain offline and deterministic.

## Constraints and Out of Scope

- Do not build a custom IdP or authentication server.
- Do not duplicate document ACLs in the Gateway; Gateway authorization is for
  model access, quotas and rate/concurrency limits.
- Do not trust client-supplied `X-User-ID`, `X-Tenant-ID`, role headers or
  unsigned actor context.
- Do not move embeddings to Gateway in this task.
- Do not add a browser frontend or Keycloak container to this backend repo
  unless a later infra task explicitly owns it.
- Do not modify Gateway source as part of this task. A confirmed Gateway
  contract defect is reported through one sanitized public issue instead.
- Do not commit secrets, real tokens, DSNs, provider URLs with credentials,
  prompts, completions or private logs.

## Acceptance Criteria

- [ ] The server can call the local Gateway with a service API key and public
      model using documented, ignored runtime configuration.
- [ ] Chat-provider secrets are absent from the server runtime configuration
      used for Gateway mode; embedding configuration remains independent.
- [ ] Invalid, expired, wrong-issuer, wrong-audience and bad-signature OIDC
      tokens are rejected before business services; valid Keycloak-shaped
      claims produce a typed principal.
- [ ] `tenants`, `users`, and `tenant_memberships` migrations/models/repositories
      exist with constraints, indexes, seed/backfill and tested downgrade.
- [ ] Documents, sessions and agent operations cannot be read or mutated
      across tenant boundaries; associated revisions, messages, search hits and
      indexing jobs preserve the same tenant scope.
- [ ] RBAC denies unauthorized actions and allows the documented role matrix;
      service-account Gateway calls remain functional.
- [ ] Existing single-user tests remain green or are updated with explicit
      default-tenant context; no test silently bypasses authorization.
- [ ] Every changed layer has focused tests and the server quality gates pass.
- [ ] Any Gateway defect is reproduced independently, classified against its
      public contract, and represented by at most one sanitized issue URL.

## Dependencies and Phase Gates

The phases are ordered and cannot be accepted out of order:

1. R1 must pass before R2 is considered complete.
2. R2 must pass before OIDC/RBAC work is allowed to depend on provider calls.
3. R3 must pass before tenant membership is used as an authorization source.
4. R4 must pass before R5 data filters are made mandatory.
5. R5 must pass before R6 exposes tenant RBAC to external users.

No blocking product questions remain for the first implementation slice: use a
provider-neutral OIDC verifier with Keycloak-compatible claims, a single
default tenant for backfill, and a Gateway service account for server-to-server
LLM calls. Concrete Keycloak deployment belongs to the final phase/infra
configuration, not a new authentication implementation.
