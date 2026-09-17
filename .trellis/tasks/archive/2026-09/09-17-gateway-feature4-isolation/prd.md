# Gateway feature-4: per-model feature isolation

## Goal

Close out gateway issue #4 Feature 4 (特性按模型隔离): complete the
capability-isolation pattern so every current and future feature is declared
on the model capabilities matrix, rejected 400 `capability_not_supported`
before the provider is touched, with an authoritative capability reference,
a diagnosable error, and a reserved per-model quota seam.

## Background / confirmed facts (exploration 2026-09-17)

- The core mechanism ALREADY EXISTS and is consistent: `internal/model/validate.go::CheckCapabilities`
  (protocol/stream/tools/structured_output/json_mode gates), called from the
  single admission pipeline `internal/httpapi/common.go::admit` (:145-151) —
  capability rejection happens before limiter, quota reserve, and provider.
  Error envelope: 400 `capability_not_supported` + request_id
  (`internal/httpapi/errors.go:72`).
- Capabilities struct: `internal/model/model.go:213-233` (incl. embeddings /
  embedding_dim / context_tokens / max_output_tokens / max_tools);
  `capabilities` catalog column is free-form JSONB (no migration needed for
  new keys).
- Gaps found (the actual work):
  1. The 400 error does not name which capability failed (generic message;
     clients can't self-serve; the server-side operator experience during
     v1.2/v1.3 integrations repeatedly needed catalog SQL to diagnose).
  2. `vision` / `reasoning` flags exist in the struct but have no gated
     surface or documented semantics (fine today, but undocumented traps).
  3. No authoritative single reference enumerating the capability matrix +
     the "new feature ⇒ new capability key" extension protocol (docs are
     scattered between developer-quickstart and gateway-client-contract;
     principle note exists only in gateway spec `03d0fc6`).
  4. Per-model quota seam (reserved in #8/#4 discussion) exists only as a
     comment; `policy.Limits` resolution is subject-pooled
     (`internal/policy/policy.go:145-188`, consumed `common.go:208-211`).
- Gateway working tree: only `.env.example` dirty (user-owned doc edit);
  #6/#7 fixes are committed. Target repo: `../knowledge-base-gateway`.

## Requirements

- R1. Capability errors become self-describing: `capability_not_supported`
  responses include the missing capability name and requested protocol in
  the message (still no content/keys), e.g.
  `"model does not declare capability 'tools' (protocol chat)"`. Golden
  fixtures updated deliberately (documented, versioned change).
- R2. Authoritative capability reference: one doc section (in
  `docs/developer-quickstart.md` or a new `docs/capabilities.md`) listing
  every capability key, its semantics, default (undeclared = false), the
  surfaces it gates, and the extension protocol for future features
  (new feature ⇒ new key ⇒ gate in `CheckCapabilities` ⇒ test ⇒ docs).
- R3. `vision` / `reasoning`: document as reserved (no gate surface yet);
  if a surface arrives it MUST gate before provider (extension protocol
  covers this). No code change.
- R4. Per-model quota seam reserved without behavior change: refactor the
  quota-limit resolution in `admit()` to a named function taking
  `(subject, publicModel)` (e.g. `policy.LimitsFor(subject, model)`),
  documented as the single place a per-model override would compose; quota
  behavior and tests unchanged.
- R5. Full verification: `gofmt`, `go vet`, `go test ./...` (incl. `-race`
  if CI-parity is cheap), replay harness `go run ./cmd/replay`, golden
  contract tests updated only for the intentional R1 change.
- R6. Close issue #4 with a completion comment mapping each Feature
  (1-3 shipped in v1.3 and adopted server-side; 4 this task) to evidence.

## Constraints

- All work in `../knowledge-base-gateway`; server repo only gets task
  artifacts. Zero behavior change except the R1 error-message enrichment
  (same status/code/envelope shape).
- No schema migration (capabilities JSONB is free-form); no new feature
  keys invented — only document existing ones.
- Keep `FoldPolicyRow` / min-of-declared (#8) semantics untouched.
- Secrets never in code/docs/tests; error messages stay content-free.

## Acceptance Criteria

- [ ] AC1: A request lacking a declared capability returns 400 whose
      message names the capability and protocol (chat/responses/embeddings
      each covered by a test); golden fixtures updated intentionally.
- [ ] AC2: One doc section authoritatively lists every capability key
      (semantics, default, gated surfaces) + the extension protocol;
      scattered docs link to it.
- [ ] AC3: `vision`/`reasoning` documented as reserved.
- [ ] AC4: Quota-limit resolution expressed as `LimitsFor(subject, model)`
      (named seam, doc comment describing the per-model extension point);
      existing quota tests pass unchanged.
- [ ] AC5: gofmt/vet/test/replay all green; replay fixtures unchanged
      unless R1 touches a replayed error case (then updated + noted).
- [ ] AC6: Issue #4 closed with a feature-to-evidence mapping comment.
