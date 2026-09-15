# Technical Design: Gateway Identity and Tenant Integration

## Design Choice

This is one ordered coordination task because each phase changes a shared
principal and tenant contract used by later phases. The first implementation
slice is the service-account Gateway path; the identity and schema phases are
not allowed to silently invent a second authorization model.

## Responsibility Boundaries

```text
IdP (Keycloak-compatible OIDC)
  user authentication, MFA, token issuance, JWKS
          |
          v
knowledge-base-server
  token verification, tenant membership, resource/RBAC authorization,
  documents, sessions, search, agents, embedding provider client
          |
          +--> knowledge-base-gateway
                 service API key, model policy, quota/limits, provider route,
                 provider secrets, LLM audit
                         |
                         v
                    LLM providers
```

The server is authoritative for business-resource access. The Gateway is
authoritative for model access and model-related cost/limit policy. The IdP is
authoritative for authentication, not tenant membership or document ACLs.

## Principal Model

Define a typed request principal at the server boundary with at least:

- `subject_id`: immutable OIDC `sub`, or an explicitly configured service
  account subject;
- `tenant_id`: selected only from verified claims/context plus a membership
  lookup, never from an unsigned header;
- `membership_role`: the role resolved from server persistence;
- `auth_method`: `oidc` or `service_account`;
- `scopes`: coarse verified token scopes, if present.

The service-account principal is used for internal calls to Gateway and does
not grant a caller access to arbitrary server resources. For a user token,
tenant selection must be explicit and authorized by membership; a token's
`preferred_username` or email is display data, not an identity key.

## OIDC Boundary

Keep JWT/OIDC parsing in an auth module and expose a verifier interface to FastAPI
dependencies. Configuration should include issuer, audience, JWKS URL (or a
strict issuer-derived discovery URL), clock skew and allowed algorithms. Cache
JWKS keys with bounded refresh behavior. Validate `iss`, `aud`, `exp`, `nbf`
when present, signature and token type before querying membership. Map
verification failures to one non-leaky 401 response.

The implementation must be Keycloak-compatible but provider-neutral: use
standard OIDC discovery/JWKS and claims, not a Keycloak-specific SDK. A later
infra change can run Keycloak and provide the issuer/configuration.

## Tenant Schema and Data Flow

```text
verified principal
  -> membership repository (tenant + role)
  -> service authorization guard
  -> repository query includes tenant_id
  -> ES query includes tenant_id filter
  -> background job payload carries tenant_id
```

Initial migration creates one deterministic default tenant and assigns existing
rows to it. New `tenant_id` columns become non-null after backfill. User-owned
rows can retain `owner_id` as the user subject/foreign key mapping, but
`owner_id` is no longer a substitute for tenant isolation.

Tables and constraints:

- `tenants`: stable ID, name, status, timestamps.
- `users`: immutable OIDC subject, display fields, status, timestamps.
- `tenant_memberships`: `(tenant_id, user_id)` unique, constrained role and
  status, timestamps; optional service-account membership is explicit.
- `documents`, `chat_sessions`, `agent_operations`: non-null `tenant_id`,
  indexed with their primary lookup keys.
- `document_revisions`: tenant scope must agree with its document.

Queries should accept a tenant scope object or required `tenant_id` argument;
there must be no default "all tenants" repository method reachable from a
request path. Search/ES hydration and indexing payloads must carry the same
tenant ID so a PostgreSQL filter cannot be bypassed by a stale index.

## RBAC Matrix (initial)

| Role | Documents | Sessions/chat | Agent operations | Membership/admin | Gateway model use |
| --- | --- | --- | --- | --- | --- |
| `tenant_admin` | read/write/delete | read/write/delete | read/write/apply | manage | according to Gateway policy |
| `editor` | read/write/delete | read/write/delete | create/resume/apply | none | according to Gateway policy |
| `member` | read/search | own or tenant chat per policy | read/create, no admin | none | according to Gateway policy |
| `viewer` | read/search | read/create if enabled | read only | none | according to Gateway policy |
| `service_account` | only explicitly granted internal routes | only explicitly granted | no user admin | none | Gateway service key only |

The first implementation may narrow session ownership to the current user if
that is the existing product policy, but the choice must be explicit in tests.
Gateway model permissions are separate grants and must not be inferred from
these resource roles.

## Compatibility and Rollout

1. Add Gateway settings and a service-account smoke without changing public
   resource semantics.
2. Remove chat provider secret use from server deployment documentation and
   run the same model through Gateway.
3. Ship OIDC verification in opt-in/configured mode; preserve service-account
   health and internal paths.
4. Migrate identity/tenant tables and backfill a default tenant.
5. Make tenant scope mandatory in repositories, services, search and jobs.
6. Enable RBAC/Keycloak configuration after cross-tenant tests pass.

Each stage should be deployable and reversible. Schema migrations use additive
steps followed by backfill and constraint tightening; never drop existing
rows or rely on `create_all` to alter a test schema.
