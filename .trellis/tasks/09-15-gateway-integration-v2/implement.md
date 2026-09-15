# Execution Plan: Gateway Identity and Tenant Integration

## Stage 0: Baseline and safeguards

- [x] Re-read the archived Gateway integration evidence and current Gateway
      client contract; inspect both repositories' main/default branches and
      preserve all dirty user changes.
- [x] Run the server offline quality baseline before code changes; record
      failures separately from integration failures.
- [x] Enumerate runtime variable names and verify ignored-secret handling
      without printing values.
- [x] Confirm the running Gateway endpoint via bounded health/readiness probes;
      do not mutate or tear down its Compose volumes.

## Stage 1: Service-account Gateway integration

- [x] Verify/adjust server documentation and runtime configuration so
      `KB_CHAT_BASE_URL`, `KB_CHAT_API_KEY`, and `KB_CHAT_MODEL` point to the
      Gateway public API and public model.
- [x] Add a focused opt-in integration probe or fixture for non-streaming and
      streaming Gateway calls; keep default tests offline.
- [x] Verify the server `/api/v1/chat` SSE path reaches the Gateway and keeps
      server/Gateway SSE formats distinct.

## Stage 2: Provider-secret migration

- [x] Remove chat-provider credentials from the server deployment path and
      examples; retain only embedding credentials where required.
- [x] Verify Gateway provider secret injection and public model routing from
      its local Compose/runtime configuration without copying secret values.
- [x] Re-run direct Gateway and server-through-Gateway checks; record safe
      rollback by restoring only ignored environment state.

## Stage 3: OIDC verification boundary

- [x] Add typed principal/auth interfaces and FastAPI dependencies at the API
      boundary; keep JWT/OIDC logic out of services and repositories.
- [x] Implement issuer, audience, signature/JWKS, expiry, clock-skew and token
      type validation with bounded JWKS caching and generic 401 errors.
- [x] Add offline tests for valid/invalid claims and Keycloak-shaped fixtures;
      no real IdP is required by the default suite.
- [x] Preserve an explicit service-account authentication path for internal
      calls and health/operations that need it.

## Stage 4: Tenant identity schema

- [x] Add ORM models and Alembic migration for `tenants`, `users`, and
      `tenant_memberships`, including role/status constraints and indexes.
- [x] Add deterministic default-tenant backfill and downgrade tests; verify
      stale test databases are recreated for schema changes.
- [x] Add repositories for subject lookup and membership resolution with no
      request-reachable all-tenant fallback.

## Stage 5: Resource tenant isolation

- [x] Add non-null `tenant_id` to documents, chat sessions and operations, plus
      document revisions and required indexes/foreign keys.
- [x] Thread tenant scope through API dependencies, services, repositories,
      chat history, agent operations, index queue payloads and ES filters.
- [x] Add cross-tenant read/write/search/delete/apply regression tests for
      every affected resource and background path.
- [x] Verify cross-tenant IDs return the chosen non-leaky 404/403 behavior.

## Stage 6: Keycloak-compatible login and RBAC

- [x] Add documented Keycloak/OIDC environment configuration and claim mapping;
      keep deployment-specific Keycloak resources in an infra concern.
- [x] Enforce the initial tenant role matrix at both API and service guards,
      including service-account restrictions.
- [x] Add authorization-matrix tests and a non-destructive local login/claim
      verification procedure using a disposable IdP or signed test tokens.
- [x] Re-run Gateway model policy checks independently from resource RBAC.

## Stage 7: Quality and handoff

- [x] Run focused tests after each stage and the complete server gates:
      `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`,
      `uv run pytest`.
- [x] Update `docs/gateway-integration.md` and add an identity/tenant runbook
      without secrets or raw user content.
- [x] If a Gateway contract defect is confirmed, reproduce it with placeholders
      and submit one sanitized issue using `gh issue create --repo
      lenny-lab/knowledge-base-gateway`; otherwise record that no issue was
      warranted.
- [ ] Run the Trellis quality check, update specs for durable auth/tenant
      conventions, and archive only after all required phases are accepted.

## Validation and Rollback

All live probes use bounded timeouts and synthetic messages. Never run
`docker compose down -v`, destructive database rollback, or commands that echo
credentials. Configuration-only rollback restores the previous ignored
`KB_CHAT_*` values. Code/schema rollback uses the migration down path and a
revertible commit only after data/backfill safety is confirmed.

## Evidence (2026-09-15, Stages 0–2, sanitized — no credentials, prompts or raw logs)

### Stage 0: Baseline and safeguards — PASS

- Server repo: branch `main`, HEAD `5f315aa`, dirty: `.claude/settings.json`
  (user-owned, untouched) + this task directory. Gateway repo: branch `v1.2`,
  HEAD `cd94bbe`, dirty: untracked `.trellis` task dir (untouched).
- Offline baseline (before any change): `ruff check` clean, `ruff format
  --check` 156 files clean, `mypy src` clean (72 files), `pytest` 534 passed /
  8 deselected. Zero pre-existing failures.
- Config var names enumerated from `core/config.py` Settings + `.env.example`;
  server `.env` is git-ignored (verified via `git check-ignore`); values never
  printed. Server `.env` chat vars still point at the user's remote provider
  (detected by shape only, left untouched as user-owned ignored state).
- Gateway probes bounded (`--max-time 5`): `127.0.0.1:8091/healthz` → 200
  `ok`, `/readyz` → 200 `ready`. No Compose/volume commands issued.

### Stage 1: Service-account Gateway integration — PASS

- `.env.example` chat block now points `KB_CHAT_BASE_URL` at the Gateway
  public API (`http://127.0.0.1:8091/v1`), `KB_CHAT_MODEL` at the public model
  `gateway-echo`, with an explicit comment that `KB_CHAT_API_KEY` is a
  Gateway service key and provider secrets never belong on the server.
  `docs/gateway-integration.md` already matched; no doc change needed.
- New opt-in probe suite `tests/test_gateway_live.py` (3 tests, httpx only,
  10s timeout): non-streaming echo completion shape, streaming SSE chunk count
  + `data: [DONE]` termination, invalid-key 401 stable envelope. New
  `live_gateway` marker excluded from default addopts; tests additionally
  skip without `KB_GATEWAY_LIVE_URL/KEY/MODEL` env. Default run: 3 deselected;
  opt-in run without env: 3 skipped; with a minted 1h key: 3 passed.
- Server `/api/v1/chat` SSE through gateway: second uvicorn on 127.0.0.1:8010
  started with process-env overrides only (`KB_CHAT_BASE_URL` → gateway,
  `KB_CHAT_MODEL=gateway-echo`, fresh Gateway key; `KB_EMBEDDING_*` and the
  user's ignored `.env` untouched). Synthetic question → events `run_started`
  → `status` → `answer_delta`×4 → terminal `done`; server `event:`-typed
  vocabulary only, no Gateway `data:` frames leaked. Server log confirms the
  run used `gateway-echo`. Probe server stopped after the check.

### Stage 2: Provider-secret migration — PASS

- Server deployment path scanned (`README.md`, `docs/`, `.env.example`,
  `docker-compose.yml`, `docker/`): no chat-provider credentials anywhere.
  `.env.example` now carries only the embedding provider example plus Gateway
  chat variables; `docs/gateway-integration.md` remaining provider mentions
  are the embedding example and a "no upstream model names" warning (both
  intentional).
- Gateway Compose config verified by name only: its `docker-compose.yml`
  `environment:` section injects `OPENAI_API_KEY` into the Gateway service
  (value never read); admin API exposes only key management (no catalog
  listing endpoint), so public-model routing is evidenced functionally by the
  200 `gateway-echo` probes against the DB-backed Compose stack.
- Post-change checks re-run: direct non-stream (200, `chat.completion`,
  `finish_reason=stop`, echo content, usage present, request id echoed) and
  stream (multiple `data:` chunks + `data: [DONE]`) probes, plus the server
  SSE path above — all via one freshly minted 1h `subject_default` key.
- Rollback: configuration-only; restore the previous ignored `KB_CHAT_*`
  values in the server `.env` (or unset the process-env overrides). No code,
  schema, or volume state to roll back.
- Cleanup: throwaway key revoked via admin API (HTTP 200); temp files
  (key copy, probe outputs, probe-server log) removed. One list-endpoint
  field-name mismatch (`id` vs `key_id`) delayed the revoke by one retry;
  no key material was exposed at any point.

### Final gates (post-change): PASS

- `ruff check .` clean; `ruff format --check .` 157 files clean;
  `mypy src` clean; `pytest` 534 passed / 11 deselected (baseline 534/8 +
  3 new `live_gateway` desections). No new failures introduced.

## Evidence (2026-09-15, Stages 3–4, sanitized — no credentials, tokens or claim values)

### Stage 3: OIDC verification boundary — PASS

- New `src/app/auth/` package; JWT/JWKS parsing is confined there (layering
  matrix respected: services/repositories never import it):
  - `auth/principal.py`: frozen `Principal` dataclass (`subject_id`,
    `auth_method` = `oidc|service_account`, optional `tenant_id`/
    `membership_role` for later membership wiring, coarse `scopes`, verified
    `claims`). Email/`preferred_username` deliberately not carried as identity.
  - `auth/verifier.py`: `TokenVerifier` (PyJWT 2.13 + crypto, added as a
    direct dependency `pyjwt[crypto]>=2.9`). Validates `iss`, `aud`,
    `exp`/`nbf` with Settings leeway, required `exp/iss/aud/sub`, signature
    via JWKS (`PyJWKClient`, `cache_keys=True`, TTL `KB_OIDC_JWKS_CACHE_TTL_S`
    default 300s, every fetch timeout-bounded by `KB_OIDC_HTTP_TIMEOUT_S`
    default 5s), and token type (`typ` whitelist bearer/at+jwt/jwt;
    `token_use` must be absent or `access`). JWKS URL from `KB_OIDC_JWKS_URL`
    or discovered once from `{issuer}/.well-known/openid-configuration`.
    ALL failures map to one generic `AuthenticationError` (401, code
    `unauthorized`); the log carries only the failure class, never tokens.
  - `auth/dependencies.py`: `get_current_principal` (opt-in — an unconfigured
    `KB_OIDC_ISSUER` denies with 401 rather than admitting) and
    `get_service_account_principal` (constant-time compare against
    `KB_SERVICE_ACCOUNT_KEY`, stable subject `KB_SERVICE_ACCOUNT_SUBJECT`;
    empty key rejects everything). New Settings block documented in
    `core/config.py`.
- `core/exceptions.py`: new `AuthenticationError` (401/`unauthorized`,
  generic-message contract documented on the class).
- Offline tests `tests/test_auth_oidc.py` (19 tests, zero network): local
  RSA keypair stands in for the IdP; fake signing-key provider (plus an
  unknown-kid variant) and httpx MockTransport exercise discovery. Covered:
  valid Keycloak-shaped token, expiry within/beyond leeway, wrong issuer,
  wrong audience, bad signature, unknown kid, ID-token `typ`/`token_use`
  rejection, missing `sub`, missing/non-Bearer header, malformed token,
  generic non-leaky envelope, JWKS discovery URI, service-account
  accept/wrong-key/unconfigured paths.

### Stage 4: Tenant identity schema — PASS

- `src/app/models/tenant.py`: `Tenant` (unique slug natural key),
  `User` (unique immutable `subject` = OIDC `sub`; email is display data,
  nullable, not unique), `TenantMembership` (unique `(tenant_id, user_id)`,
  constrained `membership_role` = tenant_admin/editor/member/viewer/
  service_account, `membership_status`, user-id index for login resolution).
  Status enums `tenant_status`/`user_status` too; all `StrEnum` + `SAEnum`
  with `values_callable`, timestamps timezone-aware.
- Migration `alembic/versions/0010_tenant_identity.py` (revises 0009):
  explicit checkfirst enum lifecycle per database-guidelines.md, FKs with
  `ondelete=CASCADE`, unique constraints + indexes, and an idempotent
  deterministic default-tenant backfill (fixed uuid5-derived id
  `3d1f0a56-...-7a10`, slug `default`, `ON CONFLICT DO NOTHING`). Downgrade
  drops memberships → users → tenants → the four enum types (full reverse;
  touches nothing outside the three new tables).
- `src/app/repositories/tenant.py`: `UserRepository.get_by_subject`,
  `TenantRepository.get_by_slug/get_by_id`,
  `TenantMembershipRepository.get_membership/list_for_user`. Every method
  resolves one explicit key; no all-tenants/all-users method exists.
- `tests/test_tenant_migration.py` (db-marked, 4 tests): disposable
  `kb_migrate_test` database is dropped WITH (FORCE) and recreated each
  session (stale schema cannot poison the run); verifies upgrade creates
  tables+enums, deterministic single-row default-tenant backfill, full
  downgrade→re-upgrade round trip (enum types removed and re-created —
  the autogen trap; same backfill id after the cycle), and constraint
  enforcement (duplicate subject, duplicate (tenant, user), unknown role).
  `tests/test_tenant_repositories.py` (db-marked, 4 tests): resolution
  contract + no-fallback `None` semantics.

### Final gates (post-Stages 3–4): PASS

- `ruff check .` clean; `ruff format --check .` 167 files clean;
  `mypy src` clean (78 files); `pytest` 561 passed / 11 deselected
  (baseline 534 + 19 auth + 8 tenant tests). Default suite: fully offline
  (new tests need no IdP; migration/repository tests are db-marked and
  auto-skip without PostgreSQL).

## Evidence (2026-09-15, Stage 5, sanitized — no credentials, tokens or user content)

### Stage 5: Resource tenant isolation — PASS

- Migration `alembic/versions/0011_resource_tenant_isolation.py` (revises
  0010): adds nullable `tenant_id` to `documents`, `chat_sessions`,
  `agent_operations`, `document_revisions`; backfills all rows with the
  deterministic default tenant (idempotent re-insert of the fixed id);
  tightens to NOT NULL; adds `ondelete=CASCADE` FKs
  (`fk_<table>_tenant_id_tenants`) and tenant lookup indexes
  (`ix_documents_tenant_id`, `ix_chat_sessions_tenant_updated_id`,
  `ix_agent_operations_tenant_id`, `ix_document_revisions_tenant_id`).
  Downgrade is the exact reverse (indexes -> FKs -> columns); resource
  tables/rows survive. Covered by two new tests in
  `tests/test_tenant_migration.py` (constraint enforcement + 0011-scoped
  downgrade/upgrade round trip); the existing full-chain
  downgrade-to-0009/upgrade test now exercises 0011 too.
- Shared constant: `models/tenant.py::DEFAULT_TENANT_ID` (the fixed 0010 id)
  — application code and migrations can never drift.
- Tenant threading (required keyword `tenant_id` everywhere; NO all-tenants
  read on any request path; the one deliberate exception is
  `DocumentRepository.list_by_index_status`, documented ops-only for the CLI
  sweep, which threads each row's own tenant into its indexing job):
  - API: new `api/deps.py::get_tenant_scope` / `TenantScope` resolves the
    request's tenant from server persistence (`KB_TENANT_DEFAULT_SLUG` via
    `TenantRepository.get_by_slug`); a missing tenant is a clean 503
    `tenant_unavailable` (new `TenantUnavailableError`), never an
    all-tenants fallback. Every resource endpoint (documents, sessions,
    search, chat, operations, writing) takes the scope and passes it down.
    This is the documented single-user MVP compatibility path; Stage 6
    replaces it with membership resolution for a verified principal
    (principal -> membership repo -> (tenant, role) -> same UUID), which is
    dependency-local because services already require the scope.
  - Services: DocumentService, ChatSessionService, ChatService (`ask` and
    its prelude/persist/summary phases; new sessions carry the caller's
    tenant), SearchService, AgentOperationService (create/get/list/resume/
    apply/draft), SummarizeService, AssociationService, WritingService —
    all take required `tenant_id`.
  - Repositories: document, chat session/message, operation, revision,
    document chunk (vector leg, hydration, neighbor docs, excerpt) — every
    method tenant-filtered.
  - Retrieval: `Retriever.retrieve` requires `tenant_id`; the ES BM25 body
    gains a structural `term` filter on the new `tenant_id` keyword field
    (`search/queries.py`, `_CHUNK_MAPPINGS`), the pgvector leg and ES-leg
    hydration filter on `Document.tenant_id`, and the search-result cache
    key includes the tenant id.
  - Background indexing: `ReindexEnqueuer` now carries
    `(doc_id, tenant_id, updated_at)`; the ARQ payload is
    `(str(doc_id), str(tenant_id), iso_version)`; `index_document` skips
    legacy tenant-less payloads with `index_job_missing_tenant` (never runs
    unscoped; the CLI sweep recovers); the pipeline's document read and
    status updates are tenant-filtered; ES chunk docs store `tenant_id`.
    Agents (`ChatDeps`/`WritingDeps`) carry a required `tenant_id` so every
    retrieval tool call is scoped.
- 404/403 policy: one non-leaky 404 (`NotFoundError`) for cross-tenant
  reads/mutations on documents, sessions, operations and revisions — a
  cross-tenant id is indistinguishable from a missing one, and rejected
  writes mutate nothing. There are no routes yet where an actor is
  authenticated but merely lacks role permission, so `ForbiddenError` (403)
  remains unused until the Stage 6 RBAC matrix introduces it.
- Tests: new `tests/test_tenant_isolation.py` (17 tests): cross-tenant
  document get/update/delete/list, session get/delete/list + message reads,
  chat `ask` on a foreign session (404) and session creation in the
  caller's tenant, agent tool tenant threading, operation
  read/resume/apply invisibility, the full optimistic-concurrency apply
  (tenant B apply rejected with zero writes; revision carries tenant A),
  pgvector leg/hydration scoping, BM25 tenant-filter body shape (offline),
  an end-to-end two-tenant ES search no-leak proof (same index, disjoint
  results), out-of-tenant and legacy indexing-job skips, and tenant-scoped
  status updates. A `tenant_b` autouse fixture seeds the probe tenant.
- Discrepancy note: none — code matched design.md. Housekeeping: the
  Stage 3/4 identity Settings (OIDC/service-account/tenant) were missing
  from `.env.example`; the identity block is now documented there (no new
  Settings keys were added by Stage 5).
- Note: the dev database is not migrated by this stage; until
  `alembic upgrade head` runs, resource endpoints answer 503
  `tenant_unavailable` by design (the API contract for an un-migrated DB).

### Final gates (post-Stage 5): PASS

- `ruff check .` clean; `ruff format --check .` 169 files clean;
  `mypy src` clean (78 files); `pytest` 580 passed / 11 deselected
  (Stage 3–4 baseline 561 + 17 isolation tests + 2 migration tests).
  Default suite: fully offline (db/es-marked tests auto-skip without
  PostgreSQL/Elasticsearch; the DB-free API contract fixtures override the
  tenant-scope dependency with the constant default tenant so no request
  touches a database).

## Evidence (2026-09-15, Stages 6–7, sanitized — no tokens, subjects, keys or user content)

### Stage 6: Keycloak-compatible login and RBAC — PASS

- RBAC decision point: new `src/app/auth/rbac.py` — `Permission` enum + the
  design.md role matrix as `MembershipRole -> frozenset[Permission]`, and
  `ensure_allowed(role, permission)` as the single enforcement function
  (403 `forbidden` on denial). Matrix: tenant_admin = everything (incl. the
  future membership-admin permission); editor = read all + doc write +
  session delete + chat/writing + operation create/resume/apply; member =
  read all + chat + operation create/resume (no apply); viewer = read/search
  only; service_account = no user-resource permission at all (internal
  routes only). Gateway model policy deliberately absent from the matrix —
  resource roles never imply model access.
- Claim mapping / scope resolution (`api/deps.py`): Stage 5's
  `get_tenant_scope` grew the designed membership path. With
  `KB_OIDC_ISSUER` set: bearer -> `TokenVerifier` (signature/iss/aud/exp/
  JWKS, unchanged Stage 3 boundary) -> `users.subject` lookup ->
  `tenant_memberships` (tenant = default slug, role = membership) -> role
  attached to `request.state`. ONLY `sub` is consumed from claims; tenant
  and role come exclusively from server persistence. Failure policy:
  verification failures 401; authenticated-but-unprovisioned / membership-
  less / disabled-membership / `service_account`-role membership all 403;
  cross-tenant ids stay non-leaky 404 downstream. No new Settings keys.
- Default-tenant path: KEPT (decision per design.md rollout step 6 — RBAC
  enables by configuration, and the MVP single-user path is the documented
  compatibility mode). With the issuer empty the dependency resolves the
  default tenant from persistence exactly as Stage 5, attaches no role, and
  downstream permission checks are skipped; both paths are pinned by tests.
- API guard wiring: `tenant_scope_requiring(*permissions)` builds
  permission-scoped scope dependencies that layer on `get_tenant_scope`
  (so existing test overrides keep working); endpoints switched to typed
  scopes: documents create/update/delete -> `DocumentWriteScope`; session
  delete -> `SessionWriteScope`; chat + writing -> `ChatScope`; operations
  create/draft/resume -> `OperationCreateScope`; apply -> `OperationApplyScope`;
  all reads/search/list/summarize/associate keep the read-tier `TenantScope`.
  Services stay actor-agnostic by design (design.md data flow: the guard
  lives at the boundary; services only ever see an already-authorized
  tenant_id) — `ensure_allowed` remains the shared function so a future
  service-side re-check is one import; matrix is additionally unit-tested at
  the guard level for every role x permission cell.
- `get_token_verifier` now returns `TokenVerifier | None` (None =
  unconfigured) so the scope dependency can share the provider without
  breaking unconfigured deployments; issuer-set-but-verifier-missing denies
  with 401, never falls open onto the compatibility path. One regression
  this exposed (50 offline API tests failing on verifier construction) was
  caught by the full suite and fixed in the same change.
- Tests: new `tests/test_rbac.py` (66 tests, offline by default): the full
  role x permission matrix against `ensure_allowed` (25 parametrized cells),
  the viewer-chat-disabled pin, and db-marked API-level proofs on a
  micro-app wiring the REAL scope dependencies with locally signed RSA
  tokens + seeded memberships: admin passes all six probed routes, the
  editor/member/viewer x route matrix (200 vs generic-403 envelope), 401
  without a token, unknown-subject / membership-less / disabled-membership
  / service_account-membership all 403, the Gateway service key rejected
  (401) on user routes, one real-app documents-router smoke (viewer read
  200, member create 403, editor create 201, cross-tenant id 404 not 403),
  and the compatibility path pin. Non-destructive local login/claim
  verification procedure documented in `docs/identity-tenants.md` §6
  (disposable issuer + signed tokens; default suite stays offline).
- Gateway model policy vs resource RBAC: the `live_gateway` marker suite was
  re-run independently (`uv run pytest -m live_gateway`: 3 skipped in 0.2s —
  opt-in only, probe-skip mode because the Gateway stack was not running; a
  bounded `curl --max-time 3 /healthz` probe confirmed it down). Stage 6
  touches no LLM/provider wiring, so the model-policy boundary is unchanged;
  no live Gateway key was minted and nothing was mutated.
- Discrepancy note: none — code followed design.md (dependency-local swap as
  Stage 5 predicted; compatibility path retained deliberately and recorded).

### Stage 7: Quality and handoff — PASS

- Final gates: `ruff check .` clean (one import-sort autofix during the
  stage); `ruff format --check .` 172 files clean; `mypy src` clean (79
  files); `pytest` 646 passed / 11 deselected (Stage 5 baseline 580 + 66
  RBAC tests). Default suite fully offline (no IdP, no live Gateway; db/es
  tests auto-skip without PostgreSQL/ES).
- Docs: new `docs/identity-tenants.md` — identity/tenant runbook (config
  table for all OIDC/service-account/tenant variables, sub-only claim
  mapping, RBAC matrix table, Keycloak-enablement steps with placeholder
  SQL, compatibility mode, non-destructive local verification recipe,
  troubleshooting table, non-goals). `docs/gateway-integration.md` gained a
  boundary section (§11): the Gateway service key is not a user identity,
  user-facing auth points at the runbook, and Gateway model policy stays
  independent of RBAC. No secrets, real subjects, tokens, or user content
  anywhere; Keycloak realm files explicitly kept out of the repo.
- Gateway issue: none warranted — no Gateway contract defect was reproduced
  or observed in this stage (no live Gateway traffic; the known tools/MCP
  forwarding limitation is already documented in gateway-integration.md as
  a capability boundary, not a defect introduced or newly discovered here).
- Remaining for the main session: Trellis quality check, spec updates for
  the durable auth/tenant conventions, and archival (left unchecked in the
  Stage 7 list on purpose).
