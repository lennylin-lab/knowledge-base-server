"""Live-ES relevance regressions for the BM25 leg (09-08-es-bm25-scoring).

Two worlds:

- D1/D2 tests are corpus-pinned to the dev `kb_documents` index (rebuilt by
  this task's migration, see README runbook): they pin measured rank
  orderings and hit counts against the real Chinese programming corpus.
- The C1 test builds its own disposable index with a synthetic two-document
  corpus so title-vs-body scoring is exactly comparable.

Auto-skipped when ES is unreachable (conftest probe), like every `es`-marked
test.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from elasticsearch import AsyncElasticsearch

from app.core.config import get_settings
from app.rag.chunker import Chunk
from app.search.es import ensure_index, replace_document_chunks
from app.search.queries import bm25_chunk_query

pytestmark = pytest.mark.es


async def _search_hydrated(
    es_client: AsyncElasticsearch, index: str, q: str
) -> list[dict[str, Any]]:
    """Run the BM25 leg and hydrate title/heading per hit for rank assertions."""
    body = {**bm25_chunk_query(q, size=50), "source": ["title", "heading_path"]}
    response = await es_client.search(index=index, **body)
    return [
        {
            "score": float(hit["_score"]),
            "title": hit["_source"]["title"],
            "heading": hit["_source"]["heading_path"],
        }
        for hit in response["hits"]["hits"]
    ]


# --- D2: identifier queries must not silently become phrase queries ---


async def test_identifier_query_matches_by_parts(es_client):
    # Pre-fix, the search-side token graph stacked alternatives at one
    # position and Lucene compiled them into an adjacency phrase: both
    # queries returned 0 hits against a corpus that holds both identifiers.
    index = get_settings().ES_INDEX

    connection_pool = await _search_hydrated(es_client, index, "ConnectionPool")
    async_bulk = await _search_hydrated(es_client, index, "async_bulk")

    assert len(connection_pool) >= 1
    assert len(async_bulk) >= 1


async def test_plain_words_still_match_identifier_documents(es_client):
    # The reverse direction (`connection pool` -> ConnectionPool documents)
    # must survive the analyzer split: the flat search stream is a plain OR
    # over the split parts.
    index = get_settings().ES_INDEX

    hits = await _search_hydrated(es_client, index, "connection pool")

    assert len(hits) == 2


# --- D1: cross-field evidence must rank the both-halves chunk first ---


async def test_cross_field_evidence_outscales_single_field_match(es_client):
    # Query `setState 状态管理`: the BLoC chunk (matches the Chinese topic in
    # its breadcrumb AND the setState identifier in its body) previously lost
    # to the Widget chunk (strong single-field match only) because
    # best_fields kept just the best field score. Additive groups invert the
    # ordering.
    index = get_settings().ES_INDEX

    hits = await _search_hydrated(es_client, index, "setState 状态管理")

    bloc_rank = next(rank for rank, hit in enumerate(hits, 1) if "BLoC" in hit["heading"])
    widget_rank = next(rank for rank, hit in enumerate(hits, 1) if "Widget 体系" in hit["title"])
    assert bloc_rank < widget_rank


# --- G3 calibration gates: coverage silences noise, keeps relevance ---
# Pin the 2026-09-08 calibration (design.md § Calibration): the default
# coverage is the loosest value that zeroes the noise probe set while every
# relevance probe keeps >= 1 hit. If corpus growth shifts these counts,
# re-calibrate rather than deleting these tests — they ARE the record.


async def test_noise_queries_return_no_hits(es_client):
    # An unrelated query must feed nothing into the BM25 leg (empty beats
    # noise). Pre-gate these returned 4 hits scoring 2.07-4.49 and entered
    # RRF fusion with real ranks.
    index = get_settings().ES_INDEX

    for q in ("股票基金定投策略", "如何做红烧肉", "今天天气怎么样"):
        hits = await _search_hydrated(es_client, index, q)
        assert hits == [], f"noise query {q!r} leaked {len(hits)} hits"


async def test_relevance_probe_queries_keep_hits(es_client):
    # The coverage gate must not silence legitimate queries: every probe
    # query from the calibration set returns at least one hit.
    index = get_settings().ES_INDEX

    for q in (
        "setState 状态管理",
        "MVCC 间隙锁",
        "缓存穿透怎么解决",
        "for 循环怎么写",
        "if not None 判断",
        "Redis 一致性",
    ):
        hits = await _search_hydrated(es_client, index, q)
        assert hits, f"relevance query {q!r} was fully gated out"


# --- C1: title evidence must not double-count through its breadcrumb ---


async def test_title_match_is_not_double_counted_via_breadcrumb(es_client, es_index_name):
    # Synthetic corpus with identical per-field statistics for the probe term
    # (one doc has it in title+breadcrumb, the other in body+code, all field
    # lengths 1, docFreq 1 everywhere):
    #
    #   doc T: title "alpha", heading "alpha", body "omega"  (identity-only)
    #   doc C: title "omega", heading "omega", body "alpha"  (body+code)
    #
    # Identical stats make one field hit score the same BM25 in every field,
    # so the shape alone decides: max-within-group gives T 2.0x and C
    # 2.5x (body 1.0x + code 1.5x) — C wins. Summing title AND heading (the
    # C1 defect) would give T 3.5x and flip the ranking.
    await ensure_index(es_client, es_index_name)
    for title, heading, text in (
        ("alpha", "alpha", "omega"),
        ("omega", "omega", "alpha"),
    ):
        await replace_document_chunks(
            es_client,
            index=es_index_name,
            document_id=uuid4(),
            title=title,
            tags=[],
            chunks=[Chunk(text=text, heading_path=heading)],
        )
    await es_client.indices.refresh(index=es_index_name)

    hits = await _search_hydrated(es_client, es_index_name, "alpha")

    assert hits, "probe term must match both documents"
    body_code_rank = next(rank for rank, hit in enumerate(hits, 1) if hit["title"] == "omega")
    title_only_rank = next(rank for rank, hit in enumerate(hits, 1) if hit["title"] == "alpha")
    assert body_code_rank < title_only_rank
