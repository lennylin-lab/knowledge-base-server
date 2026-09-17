# Execution Plan: Gateway feature-4 close-out

All work in `../knowledge-base-gateway` (Go). Do NOT touch the server repo
outside task artifacts.

## Stage 1: Self-describing capability errors (R1)

- [x] Thread the failed capability name through `CheckCapabilities` /
      `ValidateResponseSpec` (internal/model/validate.go) to the admission
      wrapper; compose
      `model does not declare capability '<name>' (protocol <protocol>)`.
- [x] Update capability-rejection tests + golden fixtures (intentional,
      documented); replay fixtures if applicable.
      (Golden fixtures contain no capability cases — verified by grep over
      internal/httpapi/testdata/golden — so they are untouched. Replay
      fixtures capture no capability cases; 12/12 pass.)
- [x] Gates: gofmt, go vet ./..., go test ./..., go run ./cmd/replay.

## Stage 2: Capability reference doc (R2, R3)

- [x] New `docs/capabilities.md`: full key table (semantics, default,
      gated surfaces, error), extension protocol (6 steps), reserved keys
      (`vision`/`reasoning`), `retrieval_profile` note (catalog attribute
      served via discovery, intentionally not gated).
- [x] Link from developer-quickstart.md + gateway-client-contract.md.

## Stage 3: Per-model quota seam (R4)

- [x] Extract `policy.LimitsFor(ctx, subject, publicModel)`; admit() uses
      it; doc comment marks the per-model extension point (#8 semantics
      untouched, behavior identical).
      (Implemented as `policy.Resolver.LimitsFor` — `NewResolver(p *Policy)`
      wraps the existing Policy; no "Resolver" type existed in the package
      before. Zero Limits + nil error for no-policy subjects reproduces the
      old `found=false` skip exactly.)
- [x] Existing quota tests pass unchanged; add a trivial test pinning
      `LimitsFor` = current pooled behavior. (`TestResolverLimitsForPooledBehavior`)

## Stage 4: Verification and close-out (R5, R6)

- [x] Full gates incl. replay harness; `-race` on httpapi + policy.
- [x] Evidence appended here (sanitized).
- [ ] Close gateway issue #4 with feature→evidence mapping comment
      (Features 1-3 shipped in v1.3 + adopted server-side; Feature 4 this
      task; per-model quota reserved via LimitsFor seam).

## Validation Commands

```bash
cd ../knowledge-base-gateway
gofmt -l . ; go vet ./...
go test ./...
go test -race ./internal/httpapi/... ./internal/policy/...
go run ./cmd/replay
```

## Evidence (2026-09-17, sanitized)

- `gofmt -l .` — clean (no output). `go vet ./...` — clean.
- `go test ./...` — 18 packages ok, 0 failures.
- `go test -race ./internal/httpapi/... ./internal/policy/...` — ok.
- `go run ./cmd/replay` — 12/12 fixtures passed.
- Package `-v` pass count across httpapi/policy/model: 138 tests.
- Working tree at gates time: only this task's files plus the pre-existing
  user-owned `.env.example` edit (left unstaged/untouched).
- No golden fixture files changed: testdata/golden contains no capability
  error cases; the frozen envelope (400 / invalid_request_error /
  capability_not_supported / request_id / X-Request-ID) is unchanged.

## Risk and Rollback Points

- R1 touches golden fixtures — keep the envelope shape frozen (status/type/
  code/request id); only `message` text changes; update fixtures in the
  same commit.
- R4 is a pure refactor — behavior-identical; revert independently.
- Gateway working tree has a user-owned `.env.example` edit — leave it
  unstaged/untouched.
