"""Live calibration probes for the vector relevance gates (manual run).

Embeds fixed short keywords and chunk-like texts through the REAL embedding
endpoint and prints the query->chunk cosine-distance matrix, so the rescue
window (`SEARCH_VECTOR_RESCUE_MARGIN` / `SEARCH_VECTOR_RESCUE_MAX_DISTANCE`)
is calibrated against measured granularity shift instead of guesswork.
Excluded from the default suite (`live_llm`); the toy-corpus probe needs no
db/es — pure embedding math. The real-corpus probe (09-10) additionally
fetches the dev `kb_documents` chunks from ES and is `es`-marked so it
probe-skips like every ES test.
Run: `uv run pytest -m live_llm tests/test_vector_distance_probe.py -s`.
"""

from __future__ import annotations

import math

import pytest
from elasticsearch import AsyncElasticsearch

from app.core.config import get_settings
from app.llm.embeddings import OpenAIEmbeddingProvider
from app.rag.chunker import Chunk
from app.rag.indexer import embedding_input

pytestmark = pytest.mark.live_llm

# Short keywords are the granularity-shift suspects: a few CJK characters or
# one or two words. The long question at the end is the well-behaved control.
QUERIES = [
    "缓存",
    "锁",
    "redis",
    "向量搜索",
    "hybrid retrieval",
    "a longer natural-language question about distributed locks in redis",
]

# Indexed chunks embed as `embedding_input(title, chunk)` — title + heading
# breadcrumb + chunk text (rag/indexer.py). The probe reflects that wrapped
# form so the measured distribution matches what the gates see after a
# reindex. Queries are NOT enriched (no query-instruct prefix by design).
_RAW_CHUNKS = [
    (
        "Redis 缓存实践",
        "Redis 缓存实践 > 分布式锁",
        "Redis 分布式锁的实现要点: SETNX、过期时间与看门狗续期。",
    ),
    (
        "检索系统笔记",
        "检索系统笔记 > 混合检索",
        "pgvector 余弦距离与 Elasticsearch BM25 的 RRF 混合检索。",
    ),
    (
        "Kotlin notes",
        "Kotlin notes > coroutines",
        f"Kotlin coroutine cancellation notes: {'structured concurrency ' * 20}",
    ),
]
CHUNKS = [
    embedding_input(title, Chunk(text=text, heading_path=path)) for title, path, text in _RAW_CHUNKS
]


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / (norm_a * norm_b)


async def test_probe_prints_short_query_distance_matrix() -> None:
    provider = OpenAIEmbeddingProvider.from_settings(get_settings())
    query_vectors = await provider.embed_texts(QUERIES)
    chunk_vectors = await provider.embed_texts(CHUNKS)

    print("\nquery -> per-chunk cosine distance (min / max)")
    for query, query_vector in zip(QUERIES, query_vectors, strict=True):
        distances = [_cosine_distance(query_vector, chunk) for chunk in chunk_vectors]
        print(f"  {query!r}: min={min(distances):.3f} max={max(distances):.3f}")

    dim = get_settings().EMBEDDING_DIM
    assert all(len(vector) == dim for vector in [*query_vectors, *chunk_vectors])


# --- 09-10 real-corpus calibration: rescue on-domain trigger (PRD R1/AC2) ---

# Genuine short in-domain keywords whose leg_min may land between the primary
# ceiling (0.45) and the trigger — the recall the trigger must keep rescuing.
# The five off-domain queries are the PRD noise set whose band must stay
# outside the trigger.
REAL_CORPUS_IN_DOMAIN_QUERIES = ["缓存", "锁", "事务", "分布式", "索引", "redis"]
REAL_CORPUS_OFF_DOMAIN_QUERIES = [
    "红烧肉怎么做才好吃",
    "量子力学薛定谔的猫",
    "恐龙为什么灭绝了",
    "感冒了应该吃什么药",
    "世界杯足球赛冠军",
]


@pytest.mark.es
async def test_probe_real_corpus_on_domain_vs_off_domain(
    es_client: AsyncElasticsearch,
) -> None:
    """Distance bands of short in-domain keywords vs off-domain queries
    against every real-corpus chunk embedding input (dev `kb_documents`).

    Reconstructs each chunk's exact embedding input (`embedding_input`), so
    the measured bands are what the vector gate sees. The argmin chunk makes
    a small min interpretable: an in-domain keyword's closest chunk should be
    a topically matching one, not an accident.
    """
    settings = get_settings()
    response = await es_client.search(
        index=settings.ES_INDEX,
        query={"match_all": {}},
        size=100,
        source=["title", "heading_path", "chunk_text"],
    )
    corpus = [
        (
            hit["_source"]["title"],
            hit["_source"]["heading_path"],
            hit["_source"]["chunk_text"],
        )
        for hit in response["hits"]["hits"]
    ]
    assert corpus, f"dev index {settings.ES_INDEX!r} is empty — index the real corpus first"
    chunk_inputs = [
        embedding_input(title, Chunk(text=text, heading_path=heading))
        for title, heading, text in corpus
    ]

    provider = OpenAIEmbeddingProvider.from_settings(settings)
    queries = [*REAL_CORPUS_IN_DOMAIN_QUERIES, *REAL_CORPUS_OFF_DOMAIN_QUERIES]
    query_vectors = await provider.embed_texts(queries)
    chunk_vectors = await provider.embed_texts(chunk_inputs)

    print(f"\ncorpus: {len(chunk_inputs)} chunks from {settings.ES_INDEX!r}")
    print("query -> per-chunk cosine distance (min / max, argmin chunk)")
    for query, query_vector in zip(queries, query_vectors, strict=True):
        distances = [_cosine_distance(query_vector, chunk) for chunk in chunk_vectors]
        closest = distances.index(min(distances))
        print(
            f"  {query!r}: min={min(distances):.3f} max={max(distances):.3f}"
            f"  argmin={corpus[closest][0]!r} / {corpus[closest][1]!r}"
        )

    dim = settings.EMBEDDING_DIM
    assert all(len(vector) == dim for vector in [*query_vectors, *chunk_vectors])
