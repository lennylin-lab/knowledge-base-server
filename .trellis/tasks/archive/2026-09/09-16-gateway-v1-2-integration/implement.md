# Execution Plan: Gateway v1.2 integration

## Stage 1: Upgrade the stack (PRD R1 / guide §2, §7-1)

- [ ] Preflight: gateway repo state (master @ v1.2+, user dirty files
      untouched), stack healthy; run `go run ./cmd/migrate version`
      against the running DB and record it.
- [ ] If version < 4: `go run ./cmd/migrate up`; re-check
      `version 4 (dirty=false)`.
- [ ] Recreate gateway service (`docker compose up -d --build`); bounded
      wait; `/healthz` + `/readyz` 200.
- [ ] Mint a short-lived subject key via admin API (shell var only);
      `GET /v1/models` lists authorized models.

## Stage 2: Capability matrix declaration (PRD R2 / guide §2.1, §7-2)

- [ ] List self-built public models from `model_catalog` (read-only) with
      their pre-change `capabilities`; capture for rollback.
- [ ] Apply conservative `capabilities` UPDATE per model (§2.1 reference,
      adjusted to the real model), bumping `config_version`.
- [ ] Verify `/v1/models/{model}` returns the declared matrix;
      `gateway-echo` reports the full seed matrix.

## Stage 3: Fake-mode tool-loop smoke (PRD R3 / guide §3.1, §7-3)

- [ ] Start disposable fake-provider gateway on a spare port; point a
      second server instance (port 8010, process-env overrides) at it.
- [ ] POST `/api/v1/chat`; parse SSE; verify `run_started` → deltas →
      terminal `done`, tool loop executed server-side (finished-log
      `tool_calls > 0`), no Gateway tool errors.
- [ ] Stop probe gateway + server; revoke key.

## Stage 4: Real-model e2e (PRD R4 / guide §3.2, §7-4)

- [ ] Admin-API preflight: real provider configured + secret injected +
      subject policy authorizing a real model (metadata only).
- [ ] If available: chat smoke through DB-mode gateway with the real
      model; acceptance = `sources` present and citations map to
      retrieved chunks (`carried_sources > 0` in server log).
- [ ] If not available: record sanitized blocker; skip without touching
      user provider config.

## Stage 5: 429 / quota verification (PRD R5 / guide §5, §7-5)

- [ ] Record the subject policy's current limits (admin API).
- [ ] Lower a limit, trigger one 429, verify error code +
      `Retry-After`.
- [ ] Restore the original limit; confirm normal 200 works again.

## Stage 6: Evidence and docs (PRD R6 / guide §7-6)

- [ ] All checklist outcomes recorded sanitized in this file.
- [ ] If a genuine v1.2 defect: minimal sanitized repro + ONE public
      issue in the gateway repo; record URL.
- [ ] Update `docs/gateway-integration.md` / v1.2 guide only on
      contradiction; note anything learned for the spec step.

## Evidence (2026-09-16, sanitized — no credentials, prompts or raw logs)

### Stage 1: Upgrade the stack — PASS (AC1)

- Preflight: gateway repo `master` @ `a2f58c2` (v1.2 in tree); user dirty
  files untouched. Stack healthy (gateway/postgres/redis all healthy).
- No gateway `.env` on disk; container DSN uses the internal hostname, so
  migrations were applied from the host against the published postgres port
  (DSN assembled in shell vars, never echoed). Before: `version 3`;
  after `go run ./cmd/migrate up`: `version 4 (dirty=false)`.
- `docker compose up -d --build` → migration job exited cleanly, gateway
  restarted; bounded wait → `/healthz` 200, `/readyz` 200.
- Minted a 4h `subject_default` key via admin API (held in a 0600 temp
  file, shredded afterwards); `GET /v1/models` → `["gateway-echo"]`.

### Stage 2: Capability matrix declaration — PASS, nothing to declare (AC2)

- `model_catalog` contains exactly ONE row: seed `gateway-echo`
  (provider fake-primary) with the migration-0003 full matrix
  (`chat/stream/tools/usage/responses/structured_output/json_mode: true`,
  `vision/reasoning: false`, context 8192 / max_output 2048 / max_tools 8).
  There are NO self-built models, so the §2.1 UPDATE had no targets;
  no catalog mutation performed (nothing to roll back).
- `GET /v1/models/gateway-echo` returns the full matrix as declared.

### Stage 3: Fake-mode tool-loop smoke — PARTIAL PASS + gateway defect (AC3)

- DB-mode stack already serves fake `gateway-echo`, so no disposable fake
  gateway was needed; probe server on port 8010 with process-env overrides
  only (`KB_CHAT_*` + empty `KB_OIDC_ISSUER`/`KB_OIDC_AUDIENCE` to disable
  the user-enabled OIDC in the probe process; user `.env` untouched).
- `POST /api/v1/chat`: SSE `run_started` → `status` →
  `tool_call_started` → `tool_call_finished(status=failed)` →
  `answer_delta`×20 → terminal `done` (`outcome=success`). The stream
  always terminates cleanly; no Gateway-side protocol errors.
- Defect: the streamed tool call arrived with an EMPTY tool name, so the
  server could not dispatch it (`tool_calls=0` in the finished log).
  Reproduced minimally: non-stream + tools → correct
  `function.name=search_knowledge`; stream + tools → every
  `delta.tool_calls[]` fragment has `"id":""`/`"name":""`. Root cause in
  gateway code: provider adapters emit `EventArgsDelta` without the
  `ToolCall`, and the chat SSE encoder only writes id/name when
  `ev.ToolCall != nil` on a delta; the assembled call rides on
  `EventArgsDone`, which emits no chunk. Streaming tools passthrough is
  broken for all clients. Issue filed:
  https://github.com/lennylin-lab/knowledge-base-gateway/issues/2
- Probe server stopped; key revoked at task end (revocation confirmed,
  post-revoke probe 401).

### Stage 4: Real-model e2e — BLOCKED (AC4)

- Admin `/admin/providers` shows only `fake-primary` / `fake-backup`
  (both fake, serving). No real provider is configured in the gateway, so
  the v1.1 deferred acceptance item stays blocked. User provider config
  deliberately not modified. Retry once a real provider + secret are
  configured and the streaming tool-name defect above is fixed (it would
  break streamed RAG regardless of provider).

### Stage 5: 429 / quota verification — PASS (AC5)

- Original policy (`subject_default`/`gateway-echo`): rate_per_minute=120,
  max_concurrent=8, daily_tokens=1000000, monthly_tokens=0 (no admin API
  for policy mutation in v1.2; read-only endpoint + direct SQL used).
- Lowered rate_per_minute to 1; needed a gateway restart before the new
  limit took effect, then bounded probes returned HTTP 429
  `rate_limit_exceeded` with `Retry-After: 46`.
- Restored rate_per_minute to 120 (DB RETURNING confirmed 120); after the
  rate window passed, the same probe returns HTTP 200. Policy not left
  modified.

### Stage 6: Docs / spec notes (AC6 + handoff)

- No contradiction with `docs/gateway-integration.md` (already carries the
  v1.2 pointer); v1.2 guide §2.1/§3.1 procedures match observed behavior
  except for the streaming defect above.
- FOR MAIN SESSION / spec step: `.trellis/spec/backend/chat-guidelines.md`
  "Gateway chat adapter boundary" convention ("gateway does not
  functionally forward tools/MCP; treat gateway-routed chat as tools-free")
  is now STALE — v1.2 supports tools passthrough and the v1.2 integration
  guide documents it as the QA-agent main path. Caveat: streaming
  tool-name defect (#2) must be fixed before relying on it over SSE.
  Spec NOT edited by this agent, per instructions.

### Baseline gates (AC6)

- `src/` untouched (`git status`: only user-owned docs changes and this
  task dir; no code modified by this task).
- `uv run pytest` as-invoked: 52 failed / 609 passed / 11 deselected —
  ALL failures are `unauthorized` on API calls: the user's `.env`
  (modified 2026-09-16 01:37 +0800, before this task's probes) now sets
  `KB_OIDC_ISSUER`/`KB_OIDC_AUDIENCE`, and the offline suite's app fixtures
  pick OIDC up. Re-run with `KB_OIDC_ISSUER= KB_OIDC_AUDIENCE=` in the
  process env: 660 passed / 11 deselected.
- One residual failure
  (`tests/test_rbac.py::test_compatibility_path_kept_when_oidc_unconfigured`)
  is also `.env`-driven: it deletes the process-env var but pydantic
  Settings still reads `env_file=".env"`, so `get_settings()` reports OIDC
  configured. Verified via a direct Settings probe (issuer set from the
  file with the env var popped). Environmental, pre-existing relative to
  this task, zero server code change; NOT fixed here (user-owned `.env`).
- Not a src regression: gate result is a function of the environment only.

## Validation Commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest   # must stay 661 passed / 11 deselected (no src changes)
```

Live checks bounded (`--max-time`); no destructive Compose commands; no
volume removal; server `.env` untouched.

## Risk and Rollback Points

- Migration failure → stop, capture logs, report (no `.down.sql` runs).
- Capability UPDATE reversible from the captured pre-change JSON.
- Policy limit restored immediately after Stage 5.
- Any server defect found → evidence + stop for follow-up decision, not
  silent scope expansion.
