# Prepare and run knowledge-base gateway integration

## Goal

Prepare `knowledge-base-server` to call the running Go
`knowledge-base-gateway`, then produce reproducible, sanitized evidence for
the direct Gateway path and the server-to-Gateway path. If a failure is
confirmed to be in the public Gateway repository, file a public issue in
`lenny-lab/knowledge-base-gateway` without exposing any credential or user
content.

## Background and Confirmed Facts

- The server already constructs its chat model through `KB_CHAT_BASE_URL`,
  `KB_CHAT_API_KEY`, and `KB_CHAT_MODEL` in `src/app/llm/models.py` and
  `src/app/core/config.py`; no new provider URL should be added to business
  code.
- The Gateway client contract is an OpenAI-compatible
  `POST /v1/chat/completions` endpoint with a Gateway API key, public model
  name, stable error envelope, and optional SSE stream.
- The Compose Gateway uses separate host ports from the server stack in the
  documented setup (`8091` for chat and `8092` for admin). The exact running
  ports and credentials must be read from the local runtime, never committed.
- The server's embedding path is independent and the Gateway currently does
  not provide `/v1/embeddings`; embedding configuration remains out of scope.
- The current Gateway contract accepts a minimal chat subset and does not
  promise `tools`/function-calling or MCP forwarding. A missing `sources`
  event in the full QA flow is therefore a capability limitation unless the
  Gateway advertises or implements tool support.
- Existing working-tree changes in either repository are user-owned and must
  be preserved.

## Requirements

### R1. Safe local configuration

Use the existing server settings to point chat at the Gateway. The integration
must use a Gateway-issued service API key and Gateway public model name, never
an upstream provider key or upstream model name. Secrets remain in ignored
local environment state or the runtime secret store; no secret is added to
tracked files, command output, logs, test fixtures, or issue text.

### R2. Gateway preflight

Verify `/healthz` and `/readyz`, authenticate with a throwaway or existing
non-production Gateway key, and exercise one non-streaming and one streaming
chat completion. Record status, response shape, request/trace correlation and
sanitized failure evidence. Do not print the admin token or plaintext API key.

### R3. Server-to-Gateway integration

Start the server with only the Gateway chat settings changed, leaving the
embedding provider independent. Exercise the server's public chat endpoint and
confirm that the server can establish a model call through the Gateway and
finish its own SSE protocol. Keep the server SSE contract separate from the
Gateway's SSE contract.

### R4. Capability boundary

Report ordinary completion success separately from the current
tools/MCP/RAG limitation. Do not alter server business permissions or claim
knowledge-grounded retrieval is verified when the Gateway did not forward
tool definitions.

### R5. Failure ownership and public issue

For each failure, first reproduce it directly against the Gateway, then through
the server, to classify ownership. Fix a server-side integration defect within
this task when it is small and covered by a regression test. For a confirmed
Gateway defect, use `gh issue create --repo lenny-lab/knowledge-base-gateway`
against the repository's default branch, with a minimal reproducible request
using placeholders, sanitized status/error data, tested commit/version, and
no prompt, completion, API key, provider URL containing credentials, DSN,
admin token, or private logs. Record the issue URL and classification in the
task evidence.

## Constraints and Out of Scope

- Do not implement an IdP, user login, tenant RBAC, or document-level access
  control in this task.
- Do not move embedding traffic to the Gateway.
- Do not modify Gateway source or its user working-tree changes as part of
  server preparation; Gateway changes are tracked as a public issue unless
  the user explicitly asks for a separate fix.
- Do not run destructive Compose commands, remove volumes, rotate unknown
  production credentials, or expose secrets while diagnosing.
- Do not treat a known, documented lack of tools/MCP support as a Gateway bug.

## Acceptance Criteria

- [ ] The server has a documented, reproducible Gateway configuration using
      existing `KB_CHAT_*` settings and no tracked secret.
- [ ] Gateway liveness/readiness, authenticated non-streaming completion, and
      authenticated streaming completion are tested; evidence contains only
      sanitized metadata.
- [ ] The server public chat endpoint completes through the Gateway, or a
      reproducible blocker is documented with ownership and next action.
- [ ] Gateway and server SSE formats are validated independently.
- [ ] Any server code change has focused offline regression coverage and all
      applicable server quality gates pass.
- [ ] Any confirmed Gateway defect has exactly one sanitized public issue (or
      an explicit record that no Gateway defect was found), with no secret or
      user content disclosed.
- [ ] Final task notes include commands run, outcomes, skipped checks and
      remaining limitations, without copying credentials or raw prompts.

## Open Questions

None blocking: the runtime may use either the documented Compose ports or
overrides, so the actual endpoint and key are discovered from local runtime
configuration at execution time rather than hard-coded in the product.
