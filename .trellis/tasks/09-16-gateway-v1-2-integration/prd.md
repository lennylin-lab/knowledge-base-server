# Gateway v1.2 integration

## Goal

Complete the v1.2 joint-integration of `knowledge-base-gateway` with
`knowledge-base-server`, following `docs/gateway-v1.2-integration.md`
(the upgrade guide) and its §7 acceptance checklist, so that QA-agent tool
calling and real-model e2e acceptance work through the Gateway.

## Background / confirmed facts (verified 2026-09-16)

- Gateway repo at `../knowledge-base-gateway`, branch `master` @ `a2f58c2`;
  tag `v1.2` (`d5266cb`) is an ancestor of master; migrations 0003/0004 are
  in the tree. Repo dirty files are user-owned — never revert them.
- Compose stack running healthy in DB mode (`kb-gateway-gateway-1`,
  `kb-gateway-postgres-1`, `kb-gateway-redis-1`, up 5h) — schema version
  must be checked at execution time (target: `version 4 (dirty=false)`).
- v1.1 integration already verified earlier (archived task
  `09-15-gateway-integration`); `KB_CHAT_*` config and topology unchanged.
- v1.2 unlock: tools / function-calling passthrough now works through the
  Gateway; `gateway-echo` seed model has full capability matrix via
  migration 0003; self-built models must get explicit `capabilities`
  (upgrade does NOT auto-open them).

## Requirements

- R1. Bring the running Gateway stack to v1.2: migrations 0003/0004 applied
  (`version 4, dirty=false`), gateway process restarted on the v1.2 code,
  `healthz`/`readyz` green. No volume removal or destructive Compose
  commands; rollback path documented but not exercised.
- R2. Declare the v1.2 capability matrix for every self-built public model
  in `model_catalog` (the one mandatory upgrade config), choosing
  `structured_output`/`context_tokens`/`max_output_tokens` conservatively
  per the real model; verify via `GET /v1/models/{model}` for an authorized
  subject. Seed `gateway-echo` needs no change.
- R3. Fake-mode tool-loop smoke (guide §3.1): with a fake provider, run the
  server `/api/v1/chat` SSE path through the Gateway and verify the tool
  loop (`tool_calls` → server `search_knowledge` → tool result round-trip)
  completes with terminal `done`.
- R4. Real-model e2e (guide §3.2/§7): through the DB-mode Gateway with a
  real provider, QA answers must include `sources` whose citation numbers
  correspond to retrieved chunks. If no real provider is configured/usable
  in the Gateway at execution time, record the blocker and skip — this is
  the enablement condition for the v1.1 deferred acceptance item.
- R5. Quota/429 behavior (guide §5/§7): trigger one 429 by lowering a
  subject policy limit, verify the server/Gateway surfaces
  `rate_limit_exceeded`/`quota_exceeded` with `Retry-After`, then restore
  the original limit.
- R6. Record sanitized evidence for every checklist item (no keys, tokens,
  prompts, or completion content); update `docs/gateway-integration.md`
  only if observed behavior contradicts it (the v1.2 guide is the
  authoritative doc and was just written).

## Constraints

- All live probes bounded (`curl --max-time` etc.); never echo secrets;
  never `docker compose down -v` or remove volumes; reuse the running stack.
- No server code changes expected — this is an integration/ops task; if a
  server defect surfaces, file it as evidence and stop for a follow-up
  decision rather than expanding scope silently.
- Server `.env` is user-owned: run probe instances with process-env
  overrides on a separate port, as in prior integration tasks.
- If the Gateway shows a defect, reproduce minimally and file ONE sanitized
  public issue in `lennylin-lab`/the gateway repo (matches prior practice;
  verify it is not a documented limitation or local config error first).

## Acceptance Criteria

- [ ] AC1: Gateway schema at `version 4 (dirty=false)`; healthz/readyz 200
      on the upgraded stack; v1.2 endpoints (`/v1/models*`, admin metrics
      fields) respond.
- [ ] AC2: Every self-built model returns its declared capability matrix
      from `/v1/models/{model}`; `gateway-echo` reports full matrix.
- [ ] AC3: Fake-mode server chat smoke: SSE sequence completes with
      terminal `done` and no Gateway-side tool errors.
- [ ] AC4: Real-model e2e attempted with recorded outcome: pass
      (`sources` + citations correspond) or a documented, sanitized
      skip/blocker.
- [ ] AC5: 429 verification performed and the policy limit restored
      afterwards (original value recorded in evidence, restored value
      confirmed).
- [ ] AC6: Gates unaffected (no server code change ⇒ baseline `uv run
      pytest` still 661 passed / 11 deselected if anything touching src is
      accidentally modified, revert); task evidence complete and secret-free.
