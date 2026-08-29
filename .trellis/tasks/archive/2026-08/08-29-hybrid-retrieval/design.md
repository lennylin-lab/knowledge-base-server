# Design: Hybrid Retrieval

## Architecture: ES ranks, PG hydrates

Both legs return **keys + leg scores**; PG hydrates and enforces visibility:

```
             q ┌─ ES BM25 (queries.py) ──> [(document_id, chunk_index, es_score)] ─┐
query embed ──┤                                                                   ├─ RRF fuse ─> hydrate(PG) ─> top-limit hits
               └─ pgvector cosine (repo) > [(document_id, chunk_index, distance)]┘
```

- ES leg returns key tuples only (`_source` minimal: nothing needed — the
  key IS the id `"{document_id}:{chunk_index}"`). ES staleness (docs of
  soft-deleted documents) is harmless: hydration joins live documents.
- Vector leg joins `documents` (`deleted_at IS NULL`) in the same query
  and hydrates inline (chunk + document title/tags).
- Hydration (`DocumentChunkRepository.get_live_chunks(keys)`): one
  `SELECT ... JOIN documents ... WHERE (document_id, chunk_index) IN
  (tuple list) AND deleted_at IS NULL`, keyed lookup in Python. Keys not
  hydrated = deleted since indexing → dropped from results.

## Fusion (`rag/retriever.py`)

Pure function, no I/O:

```python
RRF_K = 60
CANDIDATE_POOL = 50  # per leg

def fuse_rrf(
    es_keys: Sequence[ChunkKey], vector_keys: Sequence[ChunkKey], *, k: int = RRF_K
) -> list[FusedHit]  # (key, rrf_score, es_rank, vector_rank), rank 1-based
```

`score = Σ_legs 1/(k + rank_leg)`; ties broken by (score desc, es_rank
asc, vector_rank asc, key) for determinism. Retriever:

```python
class Retriever:
    def __init__(self, session_factory, es_client, embedding_provider | None, index_name): ...
    async def retrieve(self, query: str, *, limit: int = 10, tag: str | None = None) -> SearchOutcome
```

- `embedding_provider is None` ⇒ BM25-only mode (`mode="bm25"`).
- Legs run via `asyncio.gather` (ES + embed→pgvector). Provider/ES failure
  in ONE leg ⇒ warn + continue with the other; both fail ⇒ empty result
  is NOT silently returned — ES failure raises `SearchIndexError` to the
  caller (search endpoint 502), provider failure degrades to BM25.
  Asymmetry is deliberate: BM25-only is a user-visible degradation mode,
  zero legs is a broken dependency.
- Embedding happens once per retrieve; the vector leg uses the same
  provider contract as the indexer.

## ES queries (`search/queries.py`)

```python
def bm25_chunk_query(q: str, *, size: int, tag: str | None = None) -> dict
# bool(must=multi_match([chunk_text, title^2]), filter=[term(tags) if tag])
# size=size, _source=False, terminate_after not set
```

Pure dict builders; the retriever adds nothing. Tag normalized (strip/
lower) by the caller (service), consistent with documents list.

## Repository (`repositories/document_chunk.py` additions)

```python
async def search_similar(self, embedding: list[float], *, limit: int, tag: str | None) -> Sequence[Row]
    # SELECT chunk + document title/tags, JOIN documents ON id = document_id
    # WHERE deleted_at IS NULL [AND :tag = ANY(documents.tags)]
    # ORDER BY embedding.cosine_distance(:embedding) LIMIT :limit

async def get_live_chunks(self, keys: Sequence[ChunkKey]) -> dict[ChunkKey, Row]
    # hydration for the ES leg (same join/filters, key-tuple IN)
```

`(document_id, chunk_index)` tuple `IN` via `tuple_().in_()`; bounded by
CANDIDATE_POOL so the IN-list never explodes. Row = lightweight namedtuple
(content, chunk_index, document_id, document_title, document_tags).

## Service + endpoint

`services/search.py`:

```python
class SearchService:
    def __init__(self, retriever: Retriever): ...
    async def search(self, q: str, *, limit: int = 10, tag: str | None = None) -> SearchResponse
```

Normalizes tag, calls retriever, maps to schemas. Event
`search_executed` (info): `q_length`, `limit`, `tag`, `mode`, `hit_count`,
`latency_ms`, `es_hits`, `vector_hits`. Query TEXT never logged at info
(queries may contain sensitive phrasing — log `q_length` only, aligns
with logging spec's content rule).

`api/v1/endpoints/search.py`: `GET /search` — Query params
(`q: min_length=1`, `limit: ge=1 le=50 default 10`, `tag`), one service
call, `SearchResponse`. Registered in `api/v1/router.py` (`tags=["search"]`).

`api/deps.py`: `get_retriever` / `get_search_service` — builds
`OpenAIEmbeddingProvider` **only when `OPENAI_API_KEY` is non-empty**,
else `None` (BM25-only); warn log `vector_search_disabled` once at first
construction. ES client shared per app lifetime (singleton via
`app.state` or lru_cache in `search/es.py` — follow existing
`get_es_client` shape; do NOT open a client per request).

`schemas/search.py`: `SearchHit` (document_id, document_title,
document_tags, chunk_index, content, score: float, es_rank: int | None,
vector_rank: int | None), `SearchResponse` (mode: Literal["hybrid","bm25"],
items: list[SearchHit]).

## Error handling

- ES down/unreachable on search ⇒ `SearchIndexError` → 502 envelope
  (existing handler; add nothing).
- Provider failure (key configured but call fails) ⇒ warn
  `vector_search_degraded` (error_class only) ⇒ BM25 continues.
- Empty `q` / bad limit ⇒ FastAPI 422 envelope (already standard).
- Retrieval never raises raw ES/SQLA exceptions — wrapped at the
  search/ and repository boundaries per error-handling spec.

## Tests

| Suite | World | Key cases |
|---|---|---|
| `test_rrf_fusion.py` | offline | hand-computed scores/order; single-leg; empty legs; tie determinism |
| `test_es_queries.py` | offline | DSL dict shapes: boost, tag filter present/absent, size |
| `test_retriever.py` | db+es | seeded via `IndexingPipeline` + `ScriptedEmbeddingProvider`; distinctive-term BM25 top hit; scripted-vector top hit; fused response has both ranks; soft-deleted doc absent (both legs); tag narrows both legs; ES-failure ⇒ SearchIndexError; provider-failure ⇒ bm25 mode |
| `test_search_api.py` | db+es | 200 shape/mode; 422 empty q / bad limit / missing q; no-key app ⇒ mode bm25 |
| conftest/fakes | — | `ScriptedEmbeddingProvider` (text→preset vector map) added to `tests/fakes.py`; seeding helper fixture reusing the pipeline |

Seeding pattern: create documents with the service (enqueuer = run
pipeline inline with scripted provider), so ES+PG are populated exactly as
production does it — no manual ES writes.

## Tradeoffs / Rejected

- **Return chunk_text straight from ES** (rejected): PG hydration is one
  query and makes visibility/consistency single-sourced; ES stays a pure
  ranking index. Cost: one extra SELECT per search (bounded ≤100 keys).
- **Search pagination** (rejected MVP): limit-bounded; cursor pagination
  over fused scores is unstable by nature (RRF scores shift with corpus) —
  would need fixed tie-break keys; later feature.
- **Highlighting** (rejected): chunk ≤1600 chars is its own snippet.
- **Query-time tag filter in ES only** (rejected): applied in BOTH legs
  (ES term filter + PG `ANY(tags)`) so fused ranks are tag-consistent.
- **k as a Settings knob** (rejected): RRF k=60 is the standard default;
  tuning belongs to a relevance-evaluation task with real data.
- **Re-ranking / cross-encoder** (rejected): later, after real usage data.

## Rollback

Single commit (no schema change). Revert restores indexing-only state;
ES index and chunks remain valid; `search/` additions are additive.
