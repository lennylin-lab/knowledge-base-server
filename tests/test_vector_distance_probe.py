"""Live calibration probe for the vector relevance gates (manual run).

Embeds fixed short keywords and chunk-like texts through the REAL embedding
endpoint and prints the query->chunk cosine-distance matrix, so the rescue
window (`SEARCH_VECTOR_RESCUE_MARGIN` / `SEARCH_VECTOR_RESCUE_MAX_DISTANCE`)
is calibrated against measured granularity shift instead of guesswork.
Excluded from the default suite (`live_llm`); needs no db/es — pure
embedding math. Run: `uv run pytest -m live_llm tests/test_vector_distance_probe.py -s`.
"""

from __future__ import annotations

import math

import pytest

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
