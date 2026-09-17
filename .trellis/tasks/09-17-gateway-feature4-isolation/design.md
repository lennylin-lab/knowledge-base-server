# Design: Gateway feature-4 close-out (per-model feature isolation)

Anchors from gateway exploration 2026-09-17 (paths relative to
`../knowledge-base-gateway`). Principle: the isolation mechanism exists —
this task makes it complete, diagnosable, and documented, then closes #4.

## 1. Self-describing capability errors (R1)

- `internal/model/validate.go::CheckCapabilities` currently returns a bare
  sentinel; the admission layer wraps it into the generic
  `capability_not_supported` message.
- Change: thread a capability identifier through the check results —
  `CheckCapabilities` (and `ValidateResponseSpec`) return/expose which
  capability failed (e.g. `Capability: "tools"`); `admit()`'s wrapper
  composes the message
  `model does not declare capability '<name>' (protocol <protocol>)`.
- Envelope shape unchanged (400, `invalid_request_error`,
  `capability_not_supported`, request id, `X-Request-ID`) — only the
  message becomes actionable. Golden fixtures for capability cases updated
  in the same commit with a note; replay fixtures updated only if they
  capture capability cases.
- Message stays content-free (capability key + protocol name only).

## 2. Authoritative capability reference (R2, R3)

- New `docs/capabilities.md`: table of every key in
  `model.Capabilities` — boolean semantics, numeric bounds
  (`context_tokens`, `max_output_tokens`, `max_tools`, `embedding_dim`),
  default when undeclared (false/0 = unsupported), which surfaces gate on
  it (chat/responses/embeddings + streaming/tools/structured-output
  specifics), and the error clients receive.
- "Extension protocol" section (the actual Feature-4 contract):
  1. new feature ⇒ new key in `model.Capabilities`;
  2. parse from catalog JSONB (free-form, no migration);
  3. gate in `CheckCapabilities` before provider;
  4. `capability_not_supported` names the key;
  5. unit test in `validate_test.go` + pre-provider rejection test in the
     surface's `_test.go`;
  6. document in `docs/capabilities.md`.
- `vision` / `reasoning`: marked **reserved** (declared, no surface yet;
  any future surface must follow the protocol). `retrieval_profile`:
  documented as a model-level catalog attribute served via model discovery
  (data, not a request feature — intentionally not capability-gated).
- `developer-quickstart.md` and `gateway-client-contract.md` link to it
  instead of partial tables.

## 3. Per-model quota seam (R4)

- Extract the limit resolution in `admit()` (common.go:208-211) into
  `internal/policy/policy.go`:
  `func (r *Resolver) LimitsFor(ctx, subject, publicModel string) (Limits, error)`
  — today it returns exactly the current pooled min-folded limits
  (behavior-identical, #8 semantics untouched).
- Doc comment on `LimitsFor`: **the single place a per-model override
  composes** (e.g. `model_quota_overrides[public_model]` later); quota
  gate keying stays subject-pooled until that feature is designed
  (reserving ≠ implementing).
- No quota behavior change; existing quota tests must pass unchanged.

## 4. Tests

- Update capability-rejection tests to assert the new message names the
  capability (`chat_tools_test.go:125`, `responses_test.go:551`,
  `embeddings_test.go:192`, `validate_test.go:81`).
- Golden fixtures: update the capability cases; fixture-stability test
  updated deliberately.
- New: message-content test per protocol (no capability name leakage of
  content — trivially true, but pin the format).
- Full gates: `gofmt`, `go vet ./...`, `go test ./...` (and `-race` on the
  httpapi/policy packages if runtime allows), `go run ./cmd/replay`.

## 5. Rollback

- Single-commit change set per concern (R1 / R2+R3 docs / R4 seam);
  revert any independently. No migration, no persisted state change.
