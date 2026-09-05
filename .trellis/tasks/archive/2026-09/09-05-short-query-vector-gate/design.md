# Design: Short-query vector gate calibration (head-rescue)

## Problem restatement

The vector gate's single absolute ceiling (0.45) is granularity-blind: short
keyword queries sit systematically farther from long chunks than long
natural-language queries, so the whole vector leg can be gated out and
fusion degenerates to pure BM25 — ES becomes the sole decider exactly when
semantic recall matters (vocabulary-mismatch short queries).

Policy decision (user, 2026-09-05): **recall-first head rescue** — when the
vector head is marginal-but-clustered, admit it; keep a hard noise floor so
genuinely unrelated legs stay silent (rare-term keywords remain
ES-dominated).

## Mechanism: two-tier vector gate (rescue only when the primary tier is empty)

```
vector rows ──> primary tier:  distance <= SEARCH_VECTOR_MAX_DISTANCE (0.45)
                 │ survivors → fusion (unchanged behavior)
                 └ empty ──> rescue tier:
                       distance <= min(leg_min + SEARCH_VECTOR_RESCUE_MARGIN,
                                       SEARCH_VECTOR_RESCUE_MAX_DISTANCE)
                       survivors → fusion (flagged as rescued)
                     else → leg empty (today's behavior)
```

- **Rescue triggers only when zero rows pass the primary tier.** If 0.45
  already has survivors the leg was never silenced — adding marginal extras
  would only dilute fusion. This keeps today's behavior bit-identical
  whenever the primary tier is alive.
- **Rescue window is anchored to the leg's own head**: `leg_min + margin`
  (default margin 0.15) admits the clustered head of a shifted-up leg and
  drops the tail — distribution-adaptive, no query-length detection needed.
- **Hard cap** `SEARCH_VECTOR_RESCUE_MAX_DISTANCE` (default 0.85) bounds the
  window: `leg_min = 0.9` (nothing plausible) rescues nothing — the
  empty-over-noise contract holds; scripted-orthogonal worlds (distance 1.0)
  stay empty; "zorblat" stays ES-dominated.

## Query length cap (single enforcement point)

`SEARCH_MAX_QUERY_LENGTH: int = 256` (chars; `<= 0` disables). Enforced once
in `Retriever.retrieve()` before any leg work: `q = q[:cap]` when
`0 < cap < len(q)`. Both the API and the agent tools flow through this one
point, so no per-path validation is added.

Why truncate instead of 422: agent tools cannot return HTTP validation
errors, and a single mechanism keeps the retriever the only place that knows
the bound. 256 CJK chars ≈ 256 standard-analyzer tokens, comfortably under
Lucene's 1024 `max_clause_count` (the >1000-char → `query_shard_exception`
→ 502 failure mode is eliminated by construction) and under embedding
provider token limits. `q_length` in `search_executed` keeps logging the raw
caller-provided length (truncation happens inside the retriever), so the
event stays the audit surface. Over-engineering rejected: no
`Query(max_length=)` duplication on the endpoint, no truncation flag on
`SearchHit`.

## Settings (new fields, `core/config.py` search relevance gates block)

| Field | Type | Default | Meaning | Disable |
|-------|------|---------|---------|---------|
| `SEARCH_VECTOR_RESCUE_MARGIN` | `float` | `0.15` | Rescue window width above the leg's minimum distance | `<= 0` disables rescue |
| `SEARCH_VECTOR_RESCUE_MAX_DISTANCE` | `float` | `0.85` | Hard cap on rescued distances (noise floor) | `<= 0` disables rescue |
| `SEARCH_MAX_QUERY_LENGTH` | `int` | `256` | Query truncation bound applied once in `Retriever.retrieve` | `<= 0` disables |

`SEARCH_VECTOR_MAX_DISTANCE` itself is unchanged (0.45). Defaults are
starting points — the live probe (below) calibrates them; record final
values if they shift.

## Pure helper (offline-testable, `rag/retriever.py`)

```python
def filter_vector_rows_with_rescue(
    rows: Sequence[ChunkRow], *,
    max_distance: float,
    rescue_margin: float,
    rescue_max_distance: float,
) -> tuple[list[ChunkRow], int]:
    """Primary absolute gate; when it empties the leg, rescue the clustered
    head. Returns (kept rows, rescued count)."""
```

Rules: `rescue_margin <= 0 or rescue_max_distance <= 0` → rescue disabled
(behavior identical to today's `filter_vector_rows`); rows without a
measured distance pass (unchanged fail-open, unreachable via
`search_similar`); rescue disabled or primary non-empty → rescued count 0.
`filter_vector_rows` stays (used by tests); the retriever switches to the
new helper.

## Wiring / observability

- `Retriever.__init__` gains `vector_rescue_margin` / `vector_rescue_max_distance`
  kwargs (defaults mirror Settings constants; drift-guard test extended).
- `api/deps.py` `_build_retriever` injects both (single wiring point —
  search, chat, writing inherit).
- `SearchOutcome.vector_rescued: int = 0` — rows admitted only via rescue.
- `search_executed` event gains `vector_rescued` (count only, no query text).
  `vector_gated` keeps meaning "dropped by the vector gate" (both tiers).
- `SearchHit` / API surface unchanged (rescue is a gate-internal admission;
  `vector_distance` already exposes the signal for auditing).

## Live probe (calibration evidence)

`tests/test_vector_distance_probe.py`, marked `@pytest.mark.live_llm`
(excluded from the default run per quality-guidelines): embeds a fixed set
of short keywords and chunk-like texts through the real endpoint, prints the
query↔chunk cosine-distance matrix (head vs tail, short vs long query).
Run manually during this task to confirm the mechanism and calibrate
margin/cap; numbers recorded in this file's Calibration section. No DB/ES
dependency — pure embedding math.

## Edge cases

| Case | Behavior |
|------|----------|
| Primary tier has survivors | Rescue never runs — today's behavior, bit-identical |
| Primary empty, `leg_min = 0.50`, margin 0.15, cap 0.85 | Rows ≤ 0.65 rescued → short-query recall restored |
| Primary empty, `leg_min = 0.90` | 0.90 > cap 0.85 → nothing rescued (rare-term keyword stays ES-only) |
| Rescue disabled (`margin <= 0` or `cap <= 0`) | Identical to current single-tier gate |
| GATES_OFF fixture (`max_distance = 2.0`) | Primary keeps everything; rescue unreachable — legacy tests unchanged |
| Vector leg degraded (`ran=False`) | No rows, no rescue — degradation paths untouched |
| Tag filter + rescue | Rescue operates on the tag-filtered leg result; filter unchanged |

## Tradeoffs / rejected

- **Length-tiered static ceiling** (rejected): needs an arbitrary query-length
  split and a second constant to tune; rescue adapts per-query for free.
- **Rescue as union with primary** (rejected): dilutes fusion when the
  primary tier is already alive; trigger-on-empty is the minimal intervention.
- **Confidence-weighted RRF** (deferred): solves the mirror problem (weak
  vector ranks overriding precise ES matches); out of scope per policy
  decision.
- **Lowering `SEARCH_VECTOR_MAX_DISTANCE` globally** (rejected): reintroduces
  noise for long queries where 0.45 is well-calibrated.

## Rollback

Single revert; no migration. Hotfix without code revert: set
`SEARCH_VECTOR_RESCUE_MARGIN=0` (rescue off → exact pre-task behavior).

## Files touched (expected)

| File | Change |
|------|--------|
| `core/config.py` | New Settings fields + docstring sentinels |
| `rag/retriever.py` | `filter_vector_rows_with_rescue`, Retriever kwargs, `vector_rescued` counter, query truncation bound |
| `services/search.py` | `vector_rescued` into `search_executed` |
| `api/deps.py` | Inject the three new thresholds |
| `.env.example` | New settings entries |
| `tests/test_search_gates.py` | Rescue + truncation unit cases + drift-guard update |
| `tests/corpus.py` / `tests/fakes.py` | `shifted_scripted_provider` (head 0.50–0.65, tail ≥ 0.9) |
| `tests/test_retriever.py` | Rescue integration: recall restored / unrelated empty / zorblat unchanged; over-long query truncates |
| `tests/test_vector_distance_probe.py` (new) | `live_llm`-marked calibration probe |
| `tests/test_search_api.py` | `search_executed` counter assertion; over-long-q smoke (200, no 502) |

## Calibration (measured 2026-09-05, live probe vs Qwen3-Embedding-4B)

`tests/test_vector_distance_probe.py` (`live_llm`) printed query→chunk
cosine distances on a small CJK/English toy corpus:

| query | min distance | max distance |
|-------|--------------|--------------|
| `缓存` (2 CJK chars) | 0.560 | 0.605 |
| `锁` (1 CJK char) | 0.483 | 0.654 |
| `redis` | 0.425 | 0.578 |
| `向量搜索` | 0.288 | 0.652 |
| `hybrid retrieval` | 0.327 | 0.677 |
| long natural-language control | 0.219 | 0.701 |

Confirms the mechanism: pure-CJK short keywords sit entirely above the 0.45
primary ceiling (0.48–0.61) while the long control passes at 0.22 — the
fixed ceiling would silence the vector leg exactly for the shortest queries.
Final values: `SEARCH_VECTOR_RESCUE_MARGIN=0.15` (window 0.63–0.71 admits
the measured CJK heads while dropping their 0.65+ tails) and
`SEARCH_VECTOR_RESCUE_MAX_DISTANCE=0.85` (far above every measured head,
still excluding orthogonal noise at 1.0) — the design defaults hold.
