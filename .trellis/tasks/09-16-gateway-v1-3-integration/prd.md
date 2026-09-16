# Gateway v1.3 integration

## Goal

Upgrade the gateway to v1.3 (model control plane) and adopt it server-side:
embedding calls proxied through the gateway with dimension discovery,
optional `KB_CHAT_MODEL` (gateway default backfill), and RAG retrieval
thresholds driven by the gateway's retrieval profile — with env values kept
as offline fallbacks.

## Background / confirmed facts (verified 2026-09-16)

- Authoritative guide: `docs/gateway-v1.3-integration.md`; §5 is the
  acceptance checklist. Gateway migration 0005 is additive
  (`access_policies.default_model` / `default_embedding_model`,
  `model_catalog.retrieval_profile`); target schema `version 5 (dirty=false)`.
- Server: single chat-model construction point (`llm/models.py::get_chat_model`
  → `OpenAIChatModel(settings.CHAT_MODEL, ...)` — pydantic-ai requires a
  model name at construction); embeddings client `llm/embeddings.py`
  (`EMBEDDING_DIM` used in request param, response validation, cache key);
  RAG thresholds flow env → `api/deps.py::_build_retriever` → `Retriever`
  kwargs (single choke point); pgvector column width is `models/document_chunk.py::EMBEDDING_DIM = 1536`
  (independent of Settings, no startup cross-check today).
- openai SDK 3.5.0 `Model` resource is pydantic `extra="allow"` — gateway
  `capabilities` / `retrieval_profile` are reachable via attribute access.
- Settings: `KB_` prefix, `.env`, no post-init validators; test-hermetic
  pattern `hermetic_settings()` exists in `tests/fakes.py`.
- Prior verified gateway state: schema v4, `gpt-5.5` catalog row (chat/tools
  matrix), admin API on :8092, data plane :8091; upstream key valid.

## Requirements

- R1. Upgrade the gateway stack to v1.3: migrate to `version 5 (dirty=false)`,
  rebuild, healthz/readyz green; declare `embeddings`/`embedding_dim: 1536`
  on the target model's catalog row (capturing the previous capabilities for
  rollback); verify `/v1/embeddings` smoke (guide §2.3).
- R2. Embedding dimension discovery: on startup (and wherever the embedding
  client is built), read `embedding_dim` from `GET /v1/models/{model}`
  capabilities; env `KB_EMBEDDING_DIM` remains the fallback when discovery is
  unavailable or the key is absent; the effective source is logged (structlog,
  identifiers only). Keep response-width validation and cache-key semantics
  correct under a discovered dim.
- R3. Optional chat model: `KB_CHAT_MODEL` becomes optional. When unset, the
  server discovers the gateway-backfilled default at startup with one
  minimal probe request (response `model` echo) and uses the resolved name
  for the process lifetime (pydantic-ai requires a concrete name);
  discovery failure with no env model is a fast-failing startup error with a
  clear message. When set, behavior is unchanged.
- R4. Retrieval profile: fetch `retrieval_profile` from
  `GET /v1/models/{model}` at startup; profile keys override the matching
  `SEARCH_*` env defaults at the `_build_retriever` choke point; env values
  remain the fallback (gateway unavailable → server still boots);
  startup log states each threshold's source (profile/env/default).
- R5. Upgrade acceptance checklist (guide §5) executed and evidenced,
  including the人为维度不一致 `embedding_dim_mismatch` check and revert.
- R6. Offline tests for all new logic (discovery, profile override, optional
  model) using mock transports / hermetic settings; default suite stays
  fully offline and green. Update `.env.example` (new optional semantics)
  and docs if observed behavior contradicts the guide.

## Constraints

- Keep `llm/` as the only provider-SDK layer; discovery/profiling helpers
  live under `llm/` (or a small settings-override layer it feeds).
- Bounded timeouts on all gateway calls (startup discovery included); never
  echo keys/tokens; server `.env` untouched (probe instances use
  process-env overrides on port 8010, per prior integration practice).
- No destructive gateway commands; capability/policy changes captured for
  rollback; probe keys revoked afterwards.
- Default pytest suite offline; gates: ruff / format / mypy / pytest all
  green before commit.

## Acceptance Criteria

- [x] AC1: verified (Stage 1/4 evidence)  Gateway at `version 5 (dirty=false)`, healthz/readyz 200; target
      model advertises `embeddings` + `embedding_dim=1536` matching the
      pgvector column width.
- [x] AC2: verified via gateway-echo fake (real-model blocked, gateway #5)  `/v1/embeddings` smoke returns correctly-wide vectors (guide §2.3).
- [x] AC3: backfill audit-proven; happy-path round-trip deferred (upstream degradation), fail-fast path live-verified  With env model unset and gateway defaults configured, server
      starts, resolves the backfilled default at startup (logged), and a
      chat round-trip works; with env model set, requests behave as before.
- [x] AC4: verified live (startup log sources)  Server startup log shows embedding dim source (discovered/fallback)
      and each RAG threshold's source (profile/fallback); profile values
      provably override env when present.
- [x] AC5: NOT reproducible on fake path (gateway returns 200); deferred with gateway #5  `embedding_dim_mismatch` scenario demonstrated and reverted.
- [x] AC6: 22 offline tests; 682 passed / 11 deselected / 1 pre-existing env failure  Offline tests cover discovery (success/failure/timeout), optional
      model resolution, and profile override precedence; gates green
      (ruff/format/mypy/pytest), suite fully offline.
- [x] AC7: done  `.env.example` updated; task evidence sanitized; no secrets.
