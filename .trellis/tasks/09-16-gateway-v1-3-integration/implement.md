# Execution Plan: Gateway v1.3 integration

## Stage 1: Gateway upgrade + catalog/policy config (ops)

Done inline by main session 2026-09-16:

- [x] Gateway master @ `4841408` (migration 0005 present);
      `docker compose up -d --build` → schema `5 | f`; healthz/readyz 200.
- [x] `gpt-5.5` capabilities merged with `embeddings: true`,
      `embedding_dim: 1536` (pre-change JSON captured to
      `/tmp/cap-before.json`, session-temp; config_version bumped);
      visible via `/v1/models/gpt-5.5` after gateway restart.
- [x] Policy defaults set via admin API (hot, audited):
      `default_model=gpt-5.5 (chat)`, `default_embedding_model=gateway-echo`
      (see blocker below); visible in `/admin/policies`.
- [x] `/v1/embeddings` smoke: `gateway-echo` returns `object=list`, width
      **256** (fake dim per migration 0005), usage {prompt_tokens: 15,
      total_tokens: 15}.
- [ ] NOTE / blocker: **real-model embeddings unavailable upstream** — the
      upstream group (`api.longxiadev.store`) serves no embeddings model
      (`gpt-5.5` → `model_not_found`; `text-embedding-3-small` →
      `Service temporarily unavailable`, retried). Consequence: embedding
      proxy mechanics are verified against the fake 256-dim model; real
      embedding e2e + dim-mismatch-at-1536 acceptance stay blocked until
      the upstream group provisions an embeddings model. Keep
      `default_embedding_model=gateway-echo` until then.
- [x] Subject key minted (4h, temp file 0600, never echoed).

## Stage 2: Discovery module + optional chat model (server)

- [x] Add `src/app/llm/discovery.py` per design §1 (ModelFacts, retrieve,
      chat-default probe, defensive parsing, bounded timeouts, None on
      failure).
- [x] `CHAT_MODEL: str | None = None`; startup wiring in `main.py`
      lifespan + `deps.py` factories resolve the effective name; unset +
      discovery-failure ⇒ fast startup error.
- [x] Embedding dim discovery threaded into provider construction; dim ≠
      pgvector column width ⇒ startup warning.
- [x] `model_control_plane_resolved` structlog event logging all sources.
- [x] Offline tests: discovery success/failure/timeout, default probe,
      optional-model startup error, env-set unchanged behavior.

## Stage 3: Retrieval profile override (server)

- [x] Profile key mapping (explicit allowlist mirroring `SEARCH_*` names),
      unknown keys ignored with debug log, invalid values tolerated with
      warn; `apply_profile` pure function + unit tests (precedence
      profile > env > Retriever defaults).
- [x] Wire overrides into `_build_retriever`; include per-threshold source
      in the startup resolution log.

### Stage 2/3 evidence (server, 2026-09-16)

- New `src/app/llm/discovery.py`: `ModelFacts` (model_name,
  embedding_dim, retrieval_profile), `discover()` via
  `AsyncOpenAI.models.retrieve` (10s timeout, max_retries=0; defensive
  parse of the `extra="allow"` resource); unset-`CHAT_MODEL` probe = raw
  `client.post("/chat/completions", body={messages,max_tokens,stream})`
  with NO `model`, 2 attempts, resolves the response's `model` echo;
  probe failure ⇒ None, retrieve failure ⇒ facts with env fallbacks.
- New `src/app/llm/profile.py`: explicit allowlist
  (`search_bm25_min_score`/`search_bm25_min_coverage`/
  `search_vector_max_distance`/`search_vector_rescue_{margin,
  max_distance,trigger_max_distance}`/`search_rrf_min_relative`/
  `search_max_query_length`) → Retriever kwargs; unknown keys ignored
  (debug `profile_key_ignored`), invalid values warn
  (`profile_value_invalid`) and keep the env value; pure
  `apply_profile(base_kwargs, profile)`.
- `core/config.py`: `CHAT_MODEL: str | None = None` +
  `DEFAULT_CHAT_MODEL = "gpt-4o-mini"` (retired hard default, used only
  for direct construction before startup resolution — tests/tooling).
- Effective resolution: `api/deps.py::resolve_model_control_plane`
  runs in `main.py` lifespan, sets process-lifetime globals; env model
  wins over discovery (byte-identical `KB_CHAT_MODEL`-set behavior);
  unset + undiscoverable default ⇒ `RuntimeError` at startup (message
  names `KB_CHAT_MODEL`). Effective dim threads into
  `OpenAIEmbeddingProvider.from_settings(dimensions=...)` and the
  CachingEmbeddingProvider cache key; dim ≠ pgvector 1536 ⇒
  `embedding_dim_mismatch` startup warning. Consumers switched to
  `effective_chat_model(settings)`: get_chat_model (5 call sites),
  Summarize/Association display names. Resolver skips the gateway
  entirely when `CHAT_API_KEY` is empty (503-anyway deployments +
  offline suite). `model_control_plane_resolved` logs
  chat_model_source / embedding_dim_source / per-threshold map —
  identifiers only.
- Tests: `tests/test_gateway_discovery.py` (20 tests, httpx2
  MockTransport — the openai 3.5.0 SDK pins httpx2, NOT the plain httpx
  package; hermetic settings; autouse control-plane reset). Suite
  stays fully offline.
- Gates: ruff check ✓, ruff format --check ✓ (src+tests; a PRE-EXISTING
  unformatted `docs/gateway-v1.2-integration.md` working-tree edit from
  the main session fails repo-wide `ruff format --check`), mypy src ✓,
  pytest 680 passed / 11 deselected / 1 failed — the 1 failure
  (`test_rbac.py::test_compatibility_path_kept_when_oidc_unconfigured`)
  and, with the dev `.env` OIDC enabled, 52 API failures are
  ENVIRONMENTAL and pre-date this change (verified via `git stash`:
  they fail identically on the untouched tree). With
  `KB_OIDC_ISSUER=` in the process env the only failure is that rbac
  probe (also fails on the stash baseline). 661 baseline + 20 new = 681
  total, 680 green.
- Design deviations (followed design where code differed):
  1. `DEFAULT_CHAT_MODEL` legacy fallback added so existing tests
     constructing services without lifespan resolution stay green —
     the "unset + failed discovery ⇒ error" contract is enforced at
     startup (lifespan), not in per-request builders.
  2. Two `test_mcp_lifespan.py` lifespan tests stub
     `resolve_model_control_plane` (the dev `.env` points CHAT_* at the
     live gateway; resolution must not run in the offline suite).
  3. Resolver no-ops when `CHAT_API_KEY` is empty (design silent on
     keyless deployments; failing them at startup would regress
     LLM-less boots).

### Stage 2/3 follow-up evidence (empty-string CHAT_MODEL, 2026-09-16)

- Live acceptance found `KB_CHAT_MODEL=` (shell-style "unset") parses as
  `""`, not None — an empty model name would have been used/sent.
  Fix: `api/deps.py::configured_chat_model()` normalizes None/empty/
  whitespace-only to None and is now the single check in
  `resolve_model_control_plane` (probe path + fail-fast) and
  `effective_chat_model` (env-wins precedence); `llm/discovery.py`
  normalizes at its own boundary too. Behavior for a real model name is
  untouched.
- Tests: +2 offline (`KB_CHAT_MODEL=""` + discovery success ⇒ resolved
  default logged as `discovered`; whitespace `KB_CHAT_MODEL="   "` +
  discovery failure ⇒ startup `RuntimeError`). 22 total in
  test_gateway_discovery.py.
- Gates: ruff check ✓, ruff format --check ✓ (src+tests), mypy src ✓,
  pytest 682 passed / 11 deselected / 1 failed (the same pre-existing
  environmental rbac failure documented above — fails on the untouched
  baseline via `git stash`).

## Stage 4: Live acceptance (guide §5)

- [ ] With `KB_CHAT_MODEL` unset: server starts via discovered default
      (logged); chat round-trip through gateway OK; gateway audit shows
      backfilled model.
- [ ] With `KB_CHAT_MODEL` set: behavior unchanged.
- [ ] Profile: set a distinctive `retrieval_profile` on the catalog row,
      restart probe server, verify startup log shows profile as source and
      values override env; then remove profile, verify env fallback.
- [ ] Dim mismatch: temporarily set `embedding_dim: 1537`, verify 500
      `embedding_dim_mismatch`, revert to 1536, verify recovery.
- [ ] Full gates: `uv run ruff check .`, `uv run ruff format --check .`,
      `uv run mypy src`, `uv run pytest` (default suite offline; baseline
      661 passed / 11 deselected + new tests).

## Stage 5: Docs and evidence

- [ ] `.env.example`: `KB_CHAT_MODEL` optional semantics, dim discovery
      note; no other doc changes unless behavior contradicts the guide.
- [ ] Sanitized evidence per stage in this file; probe keys revoked;
      config changes captured for rollback.

## Validation Commands

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Risk and Rollback Points

- Riskiest: `CHAT_MODEL: str | None` ripples through deps factories —
  run full pytest immediately after Stage 2 core, before Stage 3.
- Discovery never blocks startup when env fallbacks exist (except the
  documented unset-chat-model case, which fails fast by design).
- Gateway config rollback: catalog capabilities JSON captured pre-change;
  policy defaults cleared via admin API; migration 0005 down exists
  (not exercised).
- Server `.env` untouched; probes on port 8010 with process-env overrides.

## Stage 4: Live acceptance (guide §5) — main session, 2026-09-16

- [x] §5-1 migrate version 5 (dirty=false) — Stage 1.
- [x] §5-2 capabilities: `gpt-5.5` advertises `embeddings=true,
      embedding_dim=1536` via `/v1/models/gpt-5.5` (matches pgvector width);
      `gateway-echo` keeps its seed 256.
- [x] §5-3 `/v1/embeddings` smoke: `gateway-echo` (fake) returns width-256
      vectors + usage. Real-model embeddings blocked (see Stage 1 blocker:
      upstream group has no embeddings model; per-provider credentials
      needed for the second upstream — filed gateway issue #5).
- [x] §5-4 default-model backfill: request WITHOUT `model` reached upstream
      and the gateway audit records `model=gpt-5.5`; bogus model still 403
      `model_not_allowed` (no auth bypass). Server-level: probe boot with
      `KB_CHAT_MODEL` empty resolved/discovery-failed exactly per design
      (see below).
- [x] §5-5 retrieval_profile: catalog profile
      `{search_vector_max_distance: 0.2, search_rrf_min_relative: 0.5}` on
      `gpt-5.5` → server startup log
      `model_control_plane_resolved` shows those two keys `profile`, the
      rest `env`, `embedding_dim_source=discovered` — profile/catalog
      consistency + source logging verified.
- [x] §5-6 dim mismatch: NOT reproducible with the fake provider — declared
      `embedding_dim: 255` on `gateway-echo`, restarted, `/v1/embeddings`
      still returned 200 with width 256 (gateway did not emit the
      documented 500 `embedding_dim_mismatch`). Enforcement appears
      real-upstream-only; demo deferred with the same blocker (#5).
      Reverted to 256 and restarted; catalog verified.
- [x] Unset-model fail-fast (design, beyond guide): boot with
      `KB_CHAT_MODEL=` (empty) while upstream degraded → probe attempts
      logged (`chat_default_probe_failed`, timeout/5xx) → startup
      `RuntimeError` naming `KB_CHAT_MODEL`. Happy path (chat round-trip on
      discovered default) deferred: upstream currently times out on all
      direct calls (transient vendor degradation); audit-level backfill
      proof + 22 offline discovery tests stand in.
- Follow-up fix during acceptance: empty/whitespace `KB_CHAT_MODEL` now
  normalizes to unset (was treated as a configured empty model); +2 tests.

## Stage 5: Docs and evidence

- [x] `.env.example`: `KB_CHAT_MODEL` documented optional (discovery +
      fail-fast semantics); `KB_EMBEDDING_DIM` documented as discovered from
      the gateway catalog with env fallback.
- [x] Probe keys revoked; gateway config rollback points recorded in
      design.md §8 (capabilities pre-change JSON, policy defaults clearable
      via admin API); profile to be removed after acceptance (done below).
- [x] Real-model embeddings e2e + dim-mismatch demo remain blocked on
      gateway #5 (per-provider credentials) + upstream provisioning.

## Retest round: gateway #5 landed (per-provider credentials) — 2026-09-16

- [x] #5 verified: gateway master `33f0eb0` (feat `16a37cd`) rebuilt;
      second openai-kind provider `openai-embed` → router.tumuer.me
      registered with its own key via `OPENAI_API_KEY__OPENAI_EMBED`
      (compose env); upstream reached and authenticated with the right
      key — evidenced by real Qwen3-Embedding-4B responses (2560-wide)
      triggering the catalog width check.
- [x] `embedding_dim_mismatch` NOW DEMONSTRATED (was deferred): declared
      1536 vs upstream 2560 → 500 `embedding_dim_mismatch` exactly as
      documented. Root cause of the width mismatch is a new gateway gap
      (below), not the enforcement.
- [x] New gateway gap filed: embeddings proxy drops the standard
      `dimensions` parameter (`EmbeddingsRequest`/wire struct lack it) —
      gateway issue #6. With MRL upstreams this makes the proxy unusable
      until fixed; enforcement itself works.
- [x] New gateway bug filed: subject-level default-model backfill fails
      (400 model is required) when any access_policies row of the subject
      has default_model NULL — gateway issue #7 (workaround: set the chat
      default on every policy row; done live, backfill+stream then OK).
- [x] Deferred AC3 (unset-model happy path) now verified end-to-end:
      probe boot with `KB_CHAT_MODEL=` → `chat_model_source=discovered`
      (`gpt-5.5`), server chat reached the gateway. Full generation
      blocked by an upstream vendor change: tools+stream now rejected 400
      by longxiadev directly (gateway fake path 200) — vendor-side, not a
      gateway/server regression; discovered-dim + backfill + discovery
      resolution all proven.
- [x] Discovery probe timeout tuned 10s → 30s: the vendor's first-token
      latency (~11.5s for "hi") exceeded the old budget, failing the
      startup probe spuriously.
- Cleanup: probe server stopped; gateway key revoked; temp files shredded.

## Retest round 2: gateway #6/#7 fixes — 2026-09-16

- [x] #6 verified: gateway working-tree fix (catalog-injected dimensions;
      `EmbeddingsRequest.Dimensions` from `embedding_dim`) rebuilt and live —
      `/v1/embeddings` with `qwen3-embedding` (Qwen3-Embedding-4B via
      `openai-embed` per-provider credential) returns exactly **1536-wide**
      vectors + usage. Real-model embedding proxy now functional.
- [x] #7 verified: reset the qwen3-embedding policy row's chat slot to NULL
      (undoing the workaround) — no-`model` request still backfills to
      `gpt-5.5` and streams (3 chunks, model echoed). Multi-row default
      resolution fixed.
- [x] Server-level real-embedding e2e: probe server with
      `KB_EMBEDDING_BASE_URL` → gateway, `KB_EMBEDDING_MODEL=qwen3-embedding`,
      `KB_EMBEDDING_DIM=` (empty) → boots cleanly, log shows
      `embedding_dim=1536, embedding_dim_source=discovered`, no mismatch
      warning; hybrid search with a gateway-embedded query returns
      semantically correct hits (Vue query → Vue doc top-3).
- [x] Follow-up server fix: empty/whitespace `KB_EMBEDDING_DIM` used to
      crash Settings (int parsing); now `int | None` with
      `_empty_dim_means_discover` validator; fallback chain
      discovered → env → `EMBEDDING_DIM_FALLBACK` (1536, config) wired
      through `effective_embedding_dim` and `from_settings`; +2 offline
      tests (684 passed / 11 deselected / 1 pre-existing env failure).
- Known remaining external item: upstream chat vendor still rejects
  tools+stream (400 direct-verified) — QA full generation pending vendor;
  everything else on the v1.3 checklist is closed.
- Cleanup: probe server stopped; gateway key revoked; temp files shredded.

## Retest round 3: root-cause correction — 2026-09-16

- [x] CORRECTION to the round-1 conclusion ("upstream vendor rejects
      tools+stream"): disproven. Same client code (probe server, pydantic-ai,
      tools+stream) pointed DIRECTLY at the vendor completes successfully
      (tool_call_finished success, tool_calls=1, done). The vendor accepts
      the server's real payload; the minimal curl shape I used earlier was
      rejected by the vendor from anywhere (schema pickiness: missing
      property descriptions/additionalProperties) — a red herring.
- [x] Actual root cause of the intermittent `llm_provider_error` (~1s
      failures): gateway-container → upstream connection resets. Audit
      shows identical payloads succeeding (200, 4.6s) and failing
      (error_class=network, status=0, ~1s) interchangeably; ~27 network
      errors accumulated. The upstream hostname resolves to 198.18.0.136
      (host transparent-proxy fake-ip), and some of the gateway container's
      outbound streaming connections are dropped by that local proxy path.
      Environmental (local proxy), not a gateway/server defect.
- [x] With the proxy path healthy, full QA loop through the gateway now
      completes: earlier runs recorded success (audit 200s incl. 31.7s
      stream at 11:30, embeddings 200s); latest replay of the exact
      pydantic-ai payload via gateway streams in 4.6s.
- Suggestion (deployment, not code): route the gateway container's upstream
  traffic outside the fake-ip proxy (direct DNS + rule exemption for
  api.longxiadev.store / router.tumuer.me), or pin stable DNS in the
  container.
