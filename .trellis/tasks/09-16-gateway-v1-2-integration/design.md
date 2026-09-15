# Design: Gateway v1.2 integration

This is an integration/ops task: no server code design. The "design" is the
verified procedure and its guardrails, derived from
`docs/gateway-v1.2-integration.md` (authoritative) plus prior archived
integration evidence (`archive/2026-09/09-15-gateway-integration/`).

## Upgrade path (R1)

1. Preflight: `git -C ../knowledge-base-gateway status` — master already
   contains v1.2 (`a2f58c2`); do not touch user-owned dirty files.
2. Read current schema version via the gateway repo's migrate tooling
   against the running stack (`go run ./cmd/migrate version` with the
   stack's env); expect `3` (v1.1) or `4` (already upgraded).
3. If < 4: `go run ./cmd/migrate up`, re-check `version` = 4, dirty=false.
   Migrations are additive (new table `admin_audit`, new columns
   `protocol`, `first_token_millis`); do NOT run `.down.sql`.
4. Recreate/restart the gateway service via Compose (`up -d --build` —
   compose file includes the one-shot migration job; harmless if already
   applied). Bounded wait for healthy; probe `healthz`/`readyz`.
5. Sanity: `GET /v1/models` and `GET /v1/models/gateway-echo` with a fresh
   subject key (minted via admin API, held in shell var only).

## Capability declaration (R2)

- Discover self-built public models: query `model_catalog` (read-only SQL
  via the stack's postgres container) for rows where `public_name !=
  'gateway-echo'`; note current `capabilities`.
- For each, run the guide §2.1 `UPDATE model_catalog SET capabilities =
  jsonb_build_object(...)` with conservative real values:
  `chat/stream/tools/usage: true`; `responses`/`structured_output` only if
  the upstream model actually supports them; `context_tokens`/
  `max_output_tokens`/`max_tools` per the model's real limits; bump
  `config_version` in the same statement.
- Verify: `/v1/models/{model}` returns the declared matrix; a capability
  not declared is rejected 400 `capability_not_supported` (optionally
  demonstrate once with a tools request on a tools-disabled model — only
  if a safe target exists; otherwise skip).

## Fake-mode tool-loop smoke (R3)

Per guide §3.1: start a disposable fake-provider gateway on a spare port
(`GATEWAY_PROVIDER=fake`, env-inline keys/models — never echoed), run a
second server instance (process-env `KB_CHAT_*` overrides, port 8010 as
before), POST `/api/v1/chat` with a knowledge question, parse SSE:
expect `run_started` → progress → answer deltas with the tool loop
executed server-side (`tool_calls` in the finished log > 0 is the
signal the loop actually ran; content remains echo — quality is not the
acceptance here) → terminal `done`. Stop the probe gateway and server
afterwards.

## Real-model e2e (R4)

- Preflight via admin API (metadata only): is a real provider configured
  with secret injected and a subject policy authorizing a real model?
- If yes: mint subject key, point `KB_CHAT_*` at the DB-mode gateway with
  that model, run the same chat smoke; acceptance = answer carries
  `sources` and citation numbers map to retrieved chunks (server log
  `carried_sources > 0`).
- If no usable real provider: record sanitized blocker in evidence; do not
  modify the user's provider config without instruction.

## 429 / quota check (R5)

- Read the subject's current policy limits (admin API, record value).
- Lower a token/request limit to trigger one 429 on a bounded probe;
  verify `rate_limit_exceeded` or `quota_exceeded` plus `Retry-After`.
- Restore the original limit and re-verify normal 200. Never leave the
  policy modified.

## Evidence, docs, issue (R6)

- All outcomes into `implement.md` evidence, sanitized (no keys/tokens/
  prompts/completions/DSNs; metadata only).
- File one sanitized public gateway issue only if a genuine v1.2 defect is
  reproduced and it is not documented behavior; record the URL.
- Update `docs/gateway-integration.md` / v1.2 guide only on contradiction.

## Rollback points

- Migration rollback deliberately not exercised; if upgrade fails, stop,
  capture logs, report (gateway docs state binary rollback doesn't require
  DB rollback).
- Capability UPDATE is reversible by re-running with the previous
  capabilities JSON (captured before the update).
- Policy limit restored immediately after the 429 probe.
- No server-side state changes at all.
