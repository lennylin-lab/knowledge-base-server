# Execution Plan: Gateway Integration

## Stage 1: Planning and preflight

- [x] Confirm the active task artifacts and backend specifications.
- [x] Inspect server and Gateway default branches, current commits and dirty
      files without reverting user changes.
- [x] Verify tool availability (`uv`, `curl`, `jq` if used, `docker compose`,
      `gh`) and enumerate configuration names without printing values.
- [x] Obtain permission for Docker/remote GitHub commands if sandbox policy
      requires escalation; keep every command bounded.

## Stage 2: Direct Gateway verification

- [x] Verify `healthz` and `readyz` on the actual running host endpoint.
- [x] Safely obtain or mint a short-lived local Gateway key; keep it only in a
      shell variable and never echo it.
- [x] Test non-streaming and streaming `gateway-echo` (or the configured
      public model) with a synthetic message.
- [x] Capture sanitized status, response shape, SSE termination and request /
      trace correlation.
- [x] Revoke the throwaway key where possible.

## Stage 3: Server preparation and end-to-end verification

- [x] Set only ignored runtime values: `KB_CHAT_BASE_URL`,
      `KB_CHAT_API_KEY`, and `KB_CHAT_MODEL`; leave `KB_EMBEDDING_*` separate.
- [x] Start or reuse the server with its normal dependencies and run the
      server `/api/v1/chat` smoke using a synthetic question.
- [x] Parse server SSE events and verify `run_started` plus terminal `done` or
      a correctly classified terminal `error`; do not confuse it with
      Gateway `data:` events.
- [x] Exercise the documented tools/MCP boundary only as a capability check;
      record it as unsupported unless the Gateway contract says otherwise.

## Stage 4: Fix or issue

- [x] If a server-side compatibility defect is found, write a regression test,
      apply the smallest boundary fix, and rerun the direct and server probes.
- [x] If a Gateway defect is found, reproduce it with a minimal sanitized
      request, verify it is not a documented limitation or local configuration
      error, and file one public issue with `gh` in
      `lenny-lab/knowledge-base-gateway`.
- [x] Review the issue body and command output for secrets before submission;
      record only the public URL and sanitized classification.

## Stage 5: Quality and handoff

- [x] Run focused tests for any changed server files.
- [x] Run `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src`, and `uv run pytest` when code changes are present.
- [x] Update `docs/gateway-integration.md` if commands, endpoint assumptions,
      or capability limitations changed.
- [x] Write task evidence with outcomes, skips, issue URL if any, and no raw
      credentials/prompts/logs.

## Validation Commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Live checks are opt-in and must use bounded timeouts. Do not run destructive
commands such as `docker compose down -v` or Gateway migration rollback during
this task.

## Risk and Rollback Points

- **Credentials:** stop and redact if any command would print a key, token,
  DSN, or provider secret.
- **Compose state:** reuse the running stack; do not remove volumes or mutate
  unknown production data.
- **Protocol mismatch:** classify Gateway tools/MCP absence as documented
  capability unless the public contract contradicts the behavior.
- **Server changes:** keep changes at `llm/` or test/documentation boundaries;
  revert the environment override to restore the pre-integration provider.

## Evidence (2026-09-15, sanitized — no credentials, prompts or raw logs)

### Stage 1: Preflight

- Artifacts and backend specs loaded (index, chat-guidelines + others via
  implement.jsonl). Tools present: `uv`, `curl`, `jq`, `gh`, `docker compose`
  v5.5.1. Config var names enumerated only; values never printed.
- Server repo: branch `main`, HEAD `1bf8475` (docs gateway integration),
  dirty: `.claude/settings.json` (user-owned, untouched).
- Gateway repo (`~/Projects/knowledge-base/knowledge-base-gateway`): branch
  `v1.2`, HEAD `f9cc953`, several user-owned dirty files incl. replaced
  `docker-compose.yml` (untouched).

### Stage 2: Direct Gateway verification — PASS

- Compose DB-backed stack running: chat `127.0.0.1:8091`, admin `127.0.0.1:8092`.
  `/healthz` → 200 `{"status":"ok"}`; `/readyz` → 200 `{"status":"ready"}`.
- Minted 1h `subject_default` key via admin API (key only in shell var).
- Non-stream: HTTP 200, `chat.completion` shape, `finish_reason=stop`,
  echo content confirmed, usage object present; `X-Request-Id` echoed,
  `X-Trace-Id` defaults to request id when not supplied.
- Stream: 5 `data:` chunks + terminal `data: [DONE]`.
- Key revoked via admin API (200); post-revoke request → 401
  `authentication_error` (stable error envelope confirmed).

### Stage 3: Server end-to-end — PASS

- Server `.env` already has `KB_CHAT_*` (points at user's own remote
  provider; left untouched). For the probe, a second uvicorn instance on
  port 8010 was started with process-env overrides only:
  `KB_CHAT_BASE_URL=http://127.0.0.1:8091/v1`, `KB_CHAT_MODEL=gateway-echo`,
  `KB_CHAT_API_KEY=<fresh 1h gateway key>`. `KB_EMBEDDING_*` untouched.
- `/api/v1/chat` SSE smoke: event sequence `run_started` → `status` →
  `answer_delta`×6 → terminal `done` (server event vocabulary only; no
  Gateway `data:` frames leaked). Server log `agent_run_finished`:
  `outcome=success`, `model=gateway-echo`, `tool_calls=0`,
  `carried_sources=0`.
- Tools/MCP capability check: direct gateway request carrying a `tools`
  array returns 200 (field tolerated, not functionally forwarded; echo
  model cannot emit tool calls). Per the documented contract
  (`docs/gateway-integration.md` compatibility note), tool-calling/MCP
  forwarding is UNSUPPORTED: no `sources` event is expected in this flow and
  knowledge-grounded RAG through the Gateway remains unverified. This is a
  documented capability boundary, not a defect.

### Stage 4: Fix or issue — NONE

- No server defect found (no code change). No Gateway defect found: all
  behavior matched the documented contract. No public issue filed.

### Stage 5: Quality and handoff

- No server code changed → lint/format/mypy/pytest gates not applicable to
  this task (baseline gates unchanged); run them before any future code
  change. `docs/gateway-integration.md` matches observed behavior; no
  updates required.
- Cleanup: test server on 8010 stopped; all three throwaway gateway keys
  revoked (revocation confirmed 200; one probe returned 401 due to a
  scripting order mistake and was re-revoked with 200). Temp files removed.
- Remaining limitations: tools/MCP/RAG-through-gateway unsupported until the
  Gateway forwards tool definitions; server request-id is not yet injected
  into the OpenAI client (existing known tracing gap, separate task).
