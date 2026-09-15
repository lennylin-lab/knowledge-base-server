# Identity and Tenant Runbook

Operational guide for user authentication (OIDC/Keycloak-compatible) and
tenant RBAC in `knowledge-base-server`. Companion to
`docs/gateway-integration.md` (model access) — this document covers business
identity and authorization only. It contains no secrets; every placeholder
below must be replaced from your own secret store.

## 1. Model

```text
IdP (Keycloak-compatible OIDC)
    authenticates users, signs access tokens, publishes JWKS
        |
        v
knowledge-base-server
    verifies token (iss/aud/exp/signature via JWKS)
    -> resolves membership SERVER-SIDE (users.subject -> tenant_memberships)
    -> enforces the tenant RBAC matrix
        |
        v
documents / sessions / operations (all rows carry tenant_id)
```

- The IdP is authoritative for **authentication only**. Tenant membership and
  roles are recorded in this server's database; a token claim can never grant
  membership the server did not record.
- The Gateway is authoritative for model access (`KB_CHAT_*` service key).
  Resource roles here never imply model permissions, and vice versa.
- Identity key: the immutable OIDC `sub` (`users.subject`). Email and
  `preferred_username` are display data only.

## 2. Configuration (all `KB_*` environment variables)

| Variable | Meaning | Default |
| --- | --- | --- |
| `KB_OIDC_ISSUER` | Expected token issuer. **Empty = OIDC disabled** (compatibility mode, see §5). | empty |
| `KB_OIDC_AUDIENCE` | Expected `aud`. Empty disables audience checking (not recommended in production). | empty |
| `KB_OIDC_JWKS_URL` | JWKS endpoint. Empty = discovered once from `{issuer}/.well-known/openid-configuration`. | empty |
| `KB_OIDC_ALGORITHMS` | Allowed signing algorithms. | `["RS256"]` |
| `KB_OIDC_LEEWAY_SECONDS` | Clock skew tolerated on `exp`/`nbf`. | 30 |
| `KB_OIDC_JWKS_CACHE_TTL_S` | Bounded JWKS key-cache refresh window. | 300 |
| `KB_OIDC_HTTP_TIMEOUT_S` | Timeout for discovery/JWKS fetches. | 5.0 |
| `KB_TENANT_DEFAULT_SLUG` | The default tenant slug resolved for every request (single-tenant MVP). | `default` |
| `KB_SERVICE_ACCOUNT_KEY` | Bearer key for internal service-account routes. Empty = path rejects everything. | empty |
| `KB_SERVICE_ACCOUNT_SUBJECT` | Audit subject recorded on service-account principals. | `service-account` |

Claim mapping: the server reads **`sub` only** for identity (plus the
standard `iss`/`aud`/`exp`/`nbf`/`typ` validation). Token `scope` is carried
coarsely on the principal but no route consults it yet. Tenant and role are
never taken from claims or headers (`X-User-ID` / `X-Tenant-ID` / role
headers are ignored by design).

## 3. RBAC matrix

Roles live in `tenant_memberships.role`; the matrix is enforced once, in
`src/app/auth/rbac.py`, for every route scope. 403 = authenticated but not
allowed; cross-tenant resource ids answer a non-leaky **404** (identical to a
missing id).

| Role | Read docs/sessions/ops, search | Create/update/delete docs, delete sessions | Chat / writing | Create/resume operations | Apply operations | Membership admin |
| --- | --- | --- | --- | --- | --- | --- |
| `tenant_admin` | yes | yes | yes | yes | yes | yes (no routes yet) |
| `editor` | yes | yes | yes | yes | yes | no |
| `member` | yes | no | yes | yes | no | no |
| `viewer` | yes | no | no | no | no | no |
| `service_account` | no user-resource routes at all — internal routes only | | | | | |

Deliberate choices pinned by tests (`tests/test_rbac.py`): viewer chat
creation is disabled (design.md "read/create if enabled" — off by default);
service-account principals pass no user-resource scope dependency.

## 4. Enabling OIDC (Keycloak-compatible)

### Local Compose stack

`docker compose up -d` starts Keycloak on host port **8180** with a pre-imported
realm `kb` (client `kb-web`, audience `kb-api`). Before the first start, run
`./docker/keycloak/ensure-realm-import.sh` to copy `kb-realm.json.example` into
the gitignored import path. Dev user credentials belong in `.env` (see
`.env.example`); `./docker/keycloak/bootstrap-dev-user.sh` creates the Keycloak
user via the Admin API and inserts the user's `sub` into `users` /
`tenant_memberships`. After migrations, set
`KB_OIDC_ISSUER=http://localhost:8180/realms/kb` and
`KB_OIDC_AUDIENCE=kb-api` in `.env`.

### External or production IdP

1. Run migrations so `tenants` / `users` / `tenant_memberships` exist
   (`uv run alembic upgrade head`; migration 0010 also creates the default
   tenant deterministically).
2. Provision the user and membership (values are placeholders — use your
   admin channel, never commit real subjects):

   ```sql
   INSERT INTO users (subject, email, display_name)
   VALUES ('<oidc-sub>', '<email>', '<display name>');
   INSERT INTO tenant_memberships (tenant_id, user_id, role)
   SELECT t.id, u.id, 'editor'
   FROM tenants t, users u
   WHERE t.slug = 'default' AND u.subject = '<oidc-sub>';
   ```

3. Configure the client in your Keycloak realm so its access tokens carry
   `aud = <KB_OIDC_AUDIENCE>` and are signed with an algorithm in
   `KB_OIDC_ALGORITHMS`. Keycloak realm/realm-import configuration is an
   infra concern: keep realm files (they reference secrets and real user
   data) out of this repository.
4. Set `KB_OIDC_ISSUER` (and `KB_OIDC_AUDIENCE`) and restart. Every business
   route then requires `Authorization: Bearer <access-token>`.

## 5. Compatibility mode (default)

With `KB_OIDC_ISSUER` empty, all requests are scoped to the default tenant
resolved from the database (503 `tenant_unavailable` if the migrations have
not run) and no RBAC is applied — the single-user MVP behavior. This mode is
kept deliberately for the MVP rollout; enabling OIDC is a configuration
change, not a code change. The service-account path (health/internal routes)
is independent of this switch.

## 6. Non-destructive local verification (no production IdP needed)

The default test suite already proves the full authorization matrix offline
with locally signed tokens (`tests/test_rbac.py`, `tests/test_auth_oidc.py`):
a local RSA keypair stands in for the IdP and no network is used. For a
manual end-to-end check without touching real identities:

1. Start the server in compatibility mode with a disposable database, run
   migrations, then set `KB_OIDC_ISSUER`/`KB_OIDC_AUDIENCE` pointing at a
   **disposable** local issuer (for example a short-lived local OIDC provider
   container or any JWKS endpoint you control). Never point a test issuer at
   production data.
2. Mint an access token signed by that issuer's key with `sub` equal to a
   subject you provisioned (§4), `aud` equal to `KB_OIDC_AUDIENCE`, and
   `typ: Bearer`.
3. Probe one route per matrix cell, expecting 200/403 per §3:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/documents \
     -H "Authorization: Bearer $TEST_TOKEN"          # read: role-dependent
   curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/v1/documents \
     -H "Authorization: Bearer $TEST_TOKEN" \
     -H 'Content-Type: application/json' -d '{"content":"# probe"}'
   ```

4. Negative checks: no header → 401; token with an unprovisioned `sub` →
   403; a resource id from another tenant → 404 (never 403). Use synthetic
   content only; the issuer/JWKS endpoints must be your throwaway ones.

All timeouts on live OIDC/JWKS fetches are bounded by
`KB_OIDC_HTTP_TIMEOUT_S`; verification failures never echo the token or the
rejection reason (one generic 401 envelope).

## 7. Troubleshooting

| Symptom | Meaning / first check |
| --- | --- |
| 401 `unauthorized` on every route | OIDC configured: missing/expired/wrong-issuer/wrong-audience/bad-signature token. The envelope is generic by design — check server config and the IdP, not the response body. |
| 403 `forbidden` with a valid token | `sub` not provisioned, membership disabled, or the role lacks the route's permission (§3). |
| 404 on a resource you can see elsewhere | Cross-tenant id: non-leaky by policy. Check the row's `tenant_id` and the caller's membership. |
| 503 `tenant_unavailable` | Database migrations not run (default tenant missing). `uv run alembic upgrade head`. |
| Service account 401 | `KB_SERVICE_ACCOUNT_KEY` unset/mismatched. It never authenticates user-resource routes. |

## 8. Boundaries and non-goals

- No password storage, login pages, MFA, token issuance, or IdP administration
  lives in this repository.
- `owner_id` columns are legacy placeholders; tenant isolation is `tenant_id`.
- Do not add client-supplied identity headers to the trust chain.
