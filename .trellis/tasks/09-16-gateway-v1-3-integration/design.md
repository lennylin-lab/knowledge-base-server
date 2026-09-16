# Design: Gateway v1.3 integration (model control plane)

Anchors reference `main` @ `6b4f678` + exploration report 2026-09-16.

## 1. New module: `src/app/llm/discovery.py`

One place that talks to the gateway's model-discovery surface (bounded
timeouts, `llm/` stays the only SDK layer):

```python
@dataclass(frozen=True)
class ModelFacts:
    model_name: str
    embedding_dim: int | None
    retrieval_profile: Mapping[str, object]   # opaque; validated by caller

async def discover(settings) -> ModelFacts | None
```

- Uses `AsyncOpenAI(base_url=CHAT_BASE_URL, api_key=CHAT_API_KEY)` +
  `models.retrieve(model)`; `capabilities` / `retrieval_profile` read off
  the pydantic `extra="allow"` resource (attribute access, defensive
  `getattr` + type checks).
- **Chat-default resolution** (only when `CHAT_MODEL` is unset): send one
  minimal streaming-disabled chat request **without `model`** via the raw
  client (bypasses pydantic-ai, which requires a concrete name); the
  response's `model` echo is the gateway-backfilled default. One retry,
  `--max-time`-equivalent bounded timeout. Failure ⇒ `None`.
- Returns `None` on any discovery failure (network, auth, unexpected
  shape) so callers can fall back to env — **except** the chat case below.

## 2. Startup wiring (`main.py` lifespan)

- If `CHAT_MODEL` set: unchanged; `ModelFacts` still fetched best-effort for
  dim/profile (failures ⇒ fallbacks, warn log).
- If `CHAT_MODEL` unset:
  - discovery succeeded ⇒ resolved default name is used everywhere
    `settings.CHAT_MODEL` was used (chat model construction
    `llm/models.py::get_chat_model`, summarize/association display names,
    cache keys via existing injection — all downstream of the single
    construction point, so the change is: resolve the effective name once
    in `deps.py` and pass it through `get_chat_model(effective_model)`).
  - discovery failed ⇒ **fail startup** with a clear `AppError`/lifespan
    error (`KB_CHAT_MODEL` unset and gateway default undiscoverable), per
    guide §3 (gateway would 400 every request anyway; fail fast instead).
- Embedding dim: effective dim = discovered `embedding_dim` else
  `EMBEDDING_DIM`; threaded into `OpenAIEmbeddingProvider` construction in
  `deps.py` (unchanged validation/cache logic — they already take the dim
  as a parameter).
- Cross-check only: if effective dim ≠ `models/document_chunk.py::EMBEDDING_DIM`
  (the pgvector column width), log a prominent warning at startup
  (mismatch actually fails at write time via gateway
  `embedding_dim_mismatch` / insert error; no migration in this task).
- Every resolution logs its source: `model_control_plane_resolved` structlog
  event with `{chat_model_source: "env"|"discovered", embedding_dim_source:
  "env"|"discovered", thresholds: {name: "profile"|"env"}}` — identifiers
  only, no keys (guide §5 wants "阈值取值来源" visible).

## 3. Retrieval profile override layer

- New `src/app/llm/profile.py` (or fold into discovery module): maps known
  `retrieval_profile` keys → the `SEARCH_*` Settings fields. Accepted key
  set is explicit and mirrors config names in snake_case
  (`search_vector_max_distance`, `search_rrf_min_relative`,
  `search_bm25_min_coverage`, rescue family, `search_bm25_min_score`);
  unknown keys are ignored with a debug log (guide: profile is opaque to
  the gateway, validation is server-side).
- Applied at the single choke point `api/deps.py::_build_retriever`: build
  an `overrides: Mapping[str, float|str]` from the fetched profile and pass
  it after env values (kwargs precedence: profile > env > `Retriever`
  module defaults). Values cast/validated (float/percent-string) with
  per-key error tolerance: invalid profile value ⇒ keep env value + warn.
- `Retriever` itself unchanged; unit-testable as a pure function
  `apply_profile(base_kwargs, profile) -> kwargs`.

## 4. Config changes (`core/config.py`)

- `CHAT_MODEL: str | None = None` (was `str = "gpt-4o-mini"`).
  `KB_CHAT_MODEL` set ⇒ exact current behavior. All consumers of
  `settings.CHAT_MODEL` audited (exploration: `deps.py` build_* factories
  and `get_chat_model`) and switched to the effective name resolved at
  startup; service-level `model_name` params stay as-is (they receive the
  effective name through the factories).
- `EMBEDDING_DIM` semantics unchanged (fallback); no new env keys required.

## 5. Gateway-side (ops, not server code)

- Migration to v5; `capabilities || {"embeddings": true, "embedding_dim":
  1536}` on `gpt-5.5` (capture pre-change JSON for rollback);
  `POST /admin/policies/subject_default/default-model` for both slots
  (`kind: chat` / `kind: embedding`); `/v1/embeddings` smoke;
  `embedding_dim_mismatch` negative check then revert; all per guide §5.

## 6. Tests (offline)

- `tests/test_gateway_discovery.py`: MockTransport-based —
  discovery success (capabilities/profile parsed), `models.retrieve`
  failure ⇒ None, chat-default probe (response `model` echo) including
  timeout ⇒ None; profile key mapping incl. unknown keys ignored and
  invalid values tolerated; `apply_profile` precedence unit tests.
- `tests/test_embeddings*.py`/config tests via `hermetic_settings`: unset
  `CHAT_MODEL` + no discovery ⇒ startup error; set ⇒ unchanged; dim
  discovery feeding the embedding provider (width validation + cache key
  still correct).
- Existing suite untouched and green (661 passed / 11 deselected).

## 7. Trade-offs

- Startup-time resolution (cached for process lifetime) vs per-request
  defaulting: chosen because pydantic-ai requires a concrete model name;
  gateway-side default changes need a server restart — documented.
- One tiny probe request for default discovery vs a future subject-visible
  default endpoint: probe works with v1.3 as shipped; if the gateway later
  exposes the default in `/v1/models`, swap the mechanism.
- Profile override at `_build_retriever` vs Settings post-init validation:
  keeps Settings pure-env (existing convention), overrides visible at the
  one place thresholds are consumed.

## 8. Rollback

- Server: revert to env-only config (`KB_CHAT_MODEL` set, `KB_EMBEDDING_*`
  direct-to-provider values) — the code paths are additive; discovery
  failures never block startup when env values exist.
- Gateway: catalog capabilities restored from captured JSON; policy
  defaults cleared via admin API; migration 0005 has a down migration
  (not exercised).
