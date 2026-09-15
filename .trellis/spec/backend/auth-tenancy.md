# Auth & Tenant Isolation Guidelines

> Executable contracts for the OIDC/service-account auth boundary, tenant
> identity schema, resource isolation, and RBAC. Established in task
> 09-15-gateway-integration-v2 (2026-09-15); verified by the final
> full-scope check.

---

## Scenario: Identity boundary — OIDC + service account

### 1. Scope / Trigger

- Trigger: any endpoint accepting user requests, and any change to
  `src/app/auth/`, `src/app/api/deps.py`, or identity Settings.

### 2. Signatures

```python
# src/app/auth/principal.py
Principal(subject_id: str, auth_method: AuthMethod, tenant_id: UUID | None,
          membership_role: str | None, scopes: tuple[str, ...],
          claims: Mapping[str, object])

# src/app/auth/verifier.py
TokenVerifier.verify(token: str) -> Principal   # sole raise: AuthenticationError

# src/app/auth/dependencies.py
get_current_principal(...) -> Principal          # OIDC bearer
get_service_account_principal(token, settings) -> Principal  # constant-time compare
require_bearer / get_token_verifier() -> TokenVerifier | None  # None when OIDC unconfigured
```

### 3. Contracts

- Settings (all `KB_`-prefixed, in `core/config.py`, documented in
  `.env.example`): `OIDC_ISSUER/AUDIENCE/JWKS_URL/ALGORITHMS/LEEWAY_SECONDS/
  JWKS_CACHE_TTL_S/HTTP_TIMEOUT_S`, `SERVICE_ACCOUNT_KEY/SUBJECT`,
  `TENANT_DEFAULT_SLUG/NAME`.
- Validation: issuer, audience, signature via cached JWKS (bounded TTL +
  HTTP timeout), expiry with clock-skew leeway, token type (`typ`/`token_use`),
  required `sub`. Unknown kid and unreachable JWKS also fail closed.
- Compatibility mode: empty `KB_OIDC_ISSUER` ⇒ no verifier constructed;
  default-tenant scope, no RBAC (pinned by tests).
- Claim mapping: only `sub` is trusted for identity; tenant and role are
  resolved server-side from `tenant_memberships` — never from claims or headers.

### 4. Validation & Error Matrix

- Any authn failure (bad sig, wrong iss/aud, expired beyond leeway, wrong
  typ, missing sub, unknown kid, JWKS unreachable) → one generic 401
  `unauthorized` envelope (`AuthenticationError`); failure class only in logs.
- Authenticated but unauthorized (RBAC) → 403 `forbidden`.
- Cross-tenant resource access → 404 (indistinguishable from missing).
- Identity schema configured but tenant row missing → 503 `tenant_unavailable`.

### 5. Good/Base/Bad Cases

- Good: `sub` resolves to a membership; Principal carries tenant + role.
- Base: no OIDC configured; default-tenant compatibility scope.
- Bad: tenant/role read from JWT claims or `X-Tenant-*` headers.

### 6. Tests Required

- `tests/test_auth_oidc.py`: offline, local RSA keypair + MockTransport;
  each validation rule with a positive and negative case.
- `tests/test_rbac.py`: full role × permission matrix, service-account
  zero-permissions, db-marked API proofs with signed tokens.

### 7. Wrong vs Correct

#### Wrong

```python
role = claims.get("roles", ["viewer"])[0]   # trusting claims for authz
tenant_id = request.headers["X-Tenant-Id"]
```

#### Correct

```python
scope = await _resolve_membership_scope(subject)  # server-side DB lookup
```

---

## Convention: Tenant isolation

**What**: Every user-reachable read/write path is tenant-scoped. Required
`tenant_id` on `documents`, `document_revisions`, `chat_sessions`,
`agent_operations` (migration `0011`); repo methods take a required
`tenant_id`; ES queries carry a structural `term` filter on the `tenant_id`
keyword field; search cache keys include the tenant; index-queue payloads
carry `(doc_id, tenant_id, version)` and tenant-less legacy payloads are
skipped, never run unscoped.

**Why**: Cross-tenant leakage is the worst-case defect; the structural ES
filter and payload-carried tenant make leakage unrepresentable rather than
merely untested.

**The one exception**: `DocumentRepository.list_by_index_status` is an
ops-only CLI sweep (bounded, not request-reachable) that threads each row's
own tenant into its job. Do not add a second unscoped method; do not expose
any all-tenant fallback to requests.

**Tests**: `tests/test_tenant_isolation.py` — cross-tenant read/write/
search/delete/apply per resource, two-tenant shared-index ES no-leak e2e.

---

## Convention: RBAC enforcement point

**What**: One permission matrix in `src/app/auth/rbac.py`
(`Permission` enum + `ensure_allowed`, raises 403 `forbidden`). Routes take
typed scope dependencies (`tenant_scope_requiring(DocumentWriteScope)` etc.);
service guards repeat the check for non-HTTP entry points. Roles:
`tenant_admin` ⊃ `editor` ⊃ `member` ⊃ `viewer`; `service_account` has zero
user-resource permissions (internal routes only). Gateway model policy is
enforced at the gateway, deliberately outside this matrix.

**Why**: A single matrix keeps the authorization answer in one auditable
place; 401 vs 403 vs 404 semantics stay unambiguous.
