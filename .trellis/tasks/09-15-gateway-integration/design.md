# Technical Design: Gateway Integration and Joint Verification

## Change Boundary

The smallest behavior gap is not a new authentication system: the server can
already construct an OpenAI-compatible client, but it has not been proven
against the running Gateway. This task therefore keeps the provider boundary
in `src/app/llm/models.py`, uses the existing `Settings` fields, and adds only
the smallest test or documentation support revealed by the live probe.

Expected server-side change locations, only if evidence requires them:

- `src/app/core/config.py` / `.env.example`: configuration discoverability or
  safe defaults, only if the existing fields cannot express the Gateway setup.
- `src/app/llm/models.py`: compatibility fix at the SDK boundary, only if the
  Gateway contract and captured sanitized request evidence prove one is needed.
- `tests/`: focused regression or opt-in live integration coverage.
- `docs/gateway-integration.md`: commands, capability boundaries and evidence
  runbook.

No business service, repository, document model, embedding path or permission
model should change for this integration.

## Runtime Data Flow

```text
KB_CHAT_BASE_URL + KB_CHAT_API_KEY + KB_CHAT_MODEL
        |
        v
Settings -> get_chat_model() -> OpenAI-compatible client
        |
        v
POST {base_url}/chat/completions
        |
        v
Gateway auth -> model policy -> provider -> OpenAI-compatible response/SSE
        |
        v
ChatService -> server-owned SSE event vocabulary
```

The server's `KB_CHAT_BASE_URL` must end at `/v1`; the OpenAI SDK appends
`/chat/completions`. A Gateway key and public catalog model are used. The
embedding client continues to use `KB_EMBEDDING_*` independently.

## Verification Phases

### Phase A: static and local evidence

Read both repositories' client contracts, inspect current branches and
working-tree changes, enumerate environment variable names without values, and
confirm the endpoint/port mapping. Never dump `.env` contents.

### Phase B: direct Gateway probe

Use liveness/readiness first. In database-backed Compose mode, use the admin
API only to mint a short-lived key for the seeded integration subject when a
safe local admin token is available. Send one bounded non-streaming request and
one bounded streaming request with a synthetic, non-sensitive message. Save
only HTTP status, selected response field names, event count/termination, and
request/trace IDs. Revoke a throwaway key after the probe when safe.

### Phase C: server probe

Run the server with the Gateway settings in the server process environment (or
its ignored local `.env`) and its own PostgreSQL/Elasticsearch dependencies.
Call `/api/v1/chat` with a synthetic question and parse the server SSE events.
The expected success criterion for the current Gateway is a completed server
stream through an ordinary model response. Retrieval sources and tool calls
are a separate capability check and must be reported as unsupported if the
Gateway strips tools.

### Phase D: ownership classification

For a failure, compare direct Gateway and server results:

| Direct Gateway | Server path | Likely owner |
| --- | --- | --- |
| fails | fails before provider call | Gateway/runtime or credentials |
| succeeds | fails | server client/config or server dependencies |
| succeeds ordinary chat; no tools | no sources/tools | documented Gateway capability boundary |
| succeeds | server returns its own domain error after model call | server agent/service |

Only a reproducible Gateway behavior that contradicts its documented contract
is eligible for a public issue.

## Compatibility and Safety

- Do not add a second retry loop beyond the existing SDK and Gateway bounded
  policies without evidence; duplicated retries can multiply provider calls.
- Keep request IDs and trace IDs in local evidence, but redact message content
  and all credentials. The existing server request middleware does not yet
  dynamically inject its ID into the OpenAI client, so correlation may require
  recording both IDs until a separate tracing change is approved.
- Treat live tests as opt-in. No default pytest run may require Gateway,
  Docker, an external provider, or a non-disposable database.
- If a public issue is needed, use placeholders such as `<gateway-key>` and
  `<synthetic-message>`, quote only generic response codes, and verify the
  final issue body locally before sending it.

## Rollback

Configuration changes are environment-only and reversible by restoring the
previous `KB_CHAT_*` values. Any server code change must be isolated, tested,
and revertible without touching data migrations. Gateway issue filing is an
external action and must happen only after ownership is confirmed; do not
include any irreversible or sensitive local state in it.
