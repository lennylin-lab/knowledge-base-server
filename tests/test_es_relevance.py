"""Live-ES relevance regressions for the BM25 leg (09-08-es-bm25-scoring).

Two worlds:

- D1/D2 tests are corpus-pinned to the dev `kb_documents` index (rebuilt by
  this task's migration, see README runbook): they pin measured rank
  orderings and hit counts against the real Chinese programming corpus.
  Re-anchored 2026-09-10 (task 09-10-irrelevant-query-noise-gates) after the
  dev corpus was replaced: D2 identifier anchors are now `useState`
  (CamelCase) / `lru_cache` (snake_case) and D1's both-halves chunk is the
  React 渲染与并发 chunk — the 09-08 anchors (ConnectionPool, async_bulk,
  BLoC/Widget) no longer exist. Presence and rank order are the invariants;
  hit totals are not pinned (totals track the corpus, not the query path).
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
from app.models.tenant import DEFAULT_TENANT_ID
from app.rag.chunker import Chunk
from app.search.es import ensure_index, replace_document_chunks
from app.search.queries import bm25_chunk_query

pytestmark = pytest.mark.es


async def _search_hydrated(
    es_client: AsyncElasticsearch, index: str, q: str
) -> list[dict[str, Any]]:
    """Run the BM25 leg and hydrate title/heading/description per hit for rank
    assertions."""
    body = {**bm25_chunk_query(q, size=50), "source": ["title", "heading_path", "description"]}
    response = await es_client.search(index=index, **body)
    return [
        {
            "score": float(hit["_score"]),
            "title": hit["_source"]["title"],
            "heading": hit["_source"]["heading_path"],
            "description": hit["_source"]["description"],
        }
        for hit in response["hits"]["hits"]
    ]


# --- D2: identifier queries must not silently become phrase queries ---
# Re-anchored 2026-09-10 (task 09-10-irrelevant-query-noise-gates): the dev
# corpus was replaced after 09-08 and the original anchors (ConnectionPool /
# async_bulk) no longer exist. Same naming-convention coverage on the current
# corpus: CamelCase `useState` (React 渲染与并发 chunk) and snake_case
# `lru_cache` (Python 装饰器 chunk) — both verified present in chunk bodies.


async def test_identifier_query_matches_by_parts(es_client):
    # Pre-fix, the search-side token graph stacked alternatives at one
    # position and Lucene compiled them into an adjacency phrase: identifier
    # queries returned 0 hits against a corpus that holds the identifiers
    # (09-08 measured ConnectionPool/async_bulk; re-anchored 09-10 to
    # useState/lru_cache — one anchor per naming convention).
    index = get_settings().ES_INDEX

    use_state = await _search_hydrated(es_client, index, "useState")
    lru_cache = await _search_hydrated(es_client, index, "lru_cache")

    assert len(use_state) >= 1
    assert len(lru_cache) >= 1


async def test_plain_words_still_match_identifier_documents(es_client):
    # The reverse direction (`use state` -> the useState chunk, `lru cache`
    # -> the lru_cache chunk) must survive the analyzer split: the flat
    # search stream is a plain OR over the split parts.
    #
    # 09-08 pinned an exact total (`== 2`); that pin rotted when the dev
    # corpus was replaced (09-10) — hit totals track the corpus, not the
    # query path. The invariant is presence of the identifier-bearing chunks
    # (the heading breadcrumbs below are unique in the current 37-chunk
    # corpus); re-calibrate totals on corpus growth instead of pinning them.
    index = get_settings().ES_INDEX

    use_state = await _search_hydrated(es_client, index, "use state")
    lru_cache = await _search_hydrated(es_client, index, "lru cache")

    assert any("渲染与并发" in hit["heading"] for hit in use_state), (
        "plain `use state` must reach the useState chunk via its split parts"
    )
    assert any("装饰器" in hit["heading"] for hit in lru_cache), (
        "plain `lru cache` must reach the lru_cache chunk via its split parts"
    )


# --- D1: cross-field evidence must rank the both-halves chunk first ---
# Re-anchored 2026-09-10: the BLoC/Widget pair is gone from the corpus. The
# same shape holds on the current data — the React 渲染与并发 chunk (heading
# breadcrumb carries 状态管理 AND body carries setState) vs the React
# doc-root chunk (title carries 状态管理, no setState in its body). The
# Flutter doc is NOT a single-side candidate anymore: its root chunk now
# mentions setState in its body, making it a both-halves chunk too.


async def test_cross_field_evidence_outscales_single_field_match(es_client):
    # Query `setState 状态管理`: the 渲染与并发 chunk (matches the Chinese
    # topic in its breadcrumb AND the setState identifier in its body) must
    # outrank the doc-root chunk (strong single-field match on the Chinese
    # topic only) — additive groups sum cross-field evidence where
    # best_fields kept just the best field score. Measured 2026-09-10 on the
    # current corpus: 18.84 (rank 1) vs 16.03 (rank 4); the 09-08 anchors
    # measured 39.99/30.99 vs 15.67 (BLoC both-halves vs Widget title-only).
    index = get_settings().ES_INDEX

    hits = await _search_hydrated(es_client, index, "setState 状态管理")

    both_halves_rank = next(
        rank for rank, hit in enumerate(hits, 1) if "渲染与并发" in hit["heading"]
    )
    title_only_rank = next(
        rank
        for rank, hit in enumerate(hits, 1)
        if hit["heading"] == hit["title"] and "React" in hit["title"]
    )
    assert both_halves_rank < title_only_rank


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
            tenant_id=str(DEFAULT_TENANT_ID),
            document_id=uuid4(),
            title=title,
            tags=[],
            description="",
            chunks=[Chunk(text=text, heading_path=heading)],
        )
    await es_client.indices.refresh(index=es_index_name)

    hits = await _search_hydrated(es_client, es_index_name, "alpha")

    assert hits, "probe term must match both documents"
    body_code_rank = next(rank for rank, hit in enumerate(hits, 1) if hit["title"] == "omega")
    title_only_rank = next(rank for rank, hit in enumerate(hits, 1) if hit["title"] == "alpha")
    assert body_code_rank < title_only_rank


# --- R8: description-field calibration probes (09-25-description-rag-association)
# Disposable synthetic corpora per the C1 pattern; the boost they calibrate is
# pinned by tests/test_es_queries.py and recorded in the task design.md
# § Calibration Record. Re-run these probes before changing
# `_DESCRIPTION_BOOST`.


async def test_description_only_term_recalls_the_document(es_client, es_index_name):
    # A term that appears ONLY in a document's description (absent from
    # title/breadcrumb/body) must still recall that document's chunk — the
    # reason the blurb joined the ES mapping and the identity group.
    await ensure_index(es_client, es_index_name)
    await replace_document_chunks(
        es_client,
        index=es_index_name,
        tenant_id=str(DEFAULT_TENANT_ID),
        document_id=uuid4(),
        title="omega",
        tags=[],
        description="bluefin",
        chunks=[Chunk(text="omega body", heading_path="omega")],
    )
    await replace_document_chunks(
        es_client,
        index=es_index_name,
        tenant_id=str(DEFAULT_TENANT_ID),
        document_id=uuid4(),
        title="tango",
        tags=[],
        description="",
        chunks=[Chunk(text="golf body", heading_path="tango")],
    )
    await es_client.indices.refresh(index=es_index_name)

    hits = await _search_hydrated(es_client, es_index_name, "bluefin")

    assert hits, "description-only term must recall the document"
    assert all(hit["title"] == "omega" for hit in hits)


async def test_description_duplication_does_not_displace_body_evidence(es_client, es_index_name):
    # No over-boost on duplication. C1-shaped corpus with identical per-field
    # statistics for the probe term:
    #
    #   doc A: title "delta", heading "delta", body "omega",
    #          description "delta"  (blurb repeats the title verbatim)
    #   doc B: title "omega", heading "omega", body "delta", description ""
    #
    # Query "delta": A's identity evidence is best_fields MAX(title^2,
    # description^b) = 2.0x — the blurb must not ADD on top of the title
    # (additive group or b >= 2.5 would give A >= 3.5x and displace B's
    # 2.5x body 1.0x + code 1.5x sum).
    await ensure_index(es_client, es_index_name)
    for title, heading, text, description in (
        ("delta", "delta", "omega", "delta"),
        ("omega", "omega", "delta", ""),
    ):
        await replace_document_chunks(
            es_client,
            index=es_index_name,
            tenant_id=str(DEFAULT_TENANT_ID),
            document_id=uuid4(),
            title=title,
            tags=[],
            description=description,
            chunks=[Chunk(text=text, heading_path=heading)],
        )
    await es_client.indices.refresh(index=es_index_name)

    hits = await _search_hydrated(es_client, es_index_name, "delta")

    assert hits, "probe term must match both documents"
    body_rank = next(rank for rank, hit in enumerate(hits, 1) if hit["title"] == "omega")
    identity_rank = next(rank for rank, hit in enumerate(hits, 1) if hit["title"] == "delta")
    assert body_rank < identity_rank, (
        "duplicated description must stay max-grouped: body+code evidence outranks it"
    )


async def test_empty_description_documents_keep_their_scores(es_client, es_index_name):
    # Empty-description parity: two structurally identical documents, one
    # with a (non-matching) blurb, one without. On a body-term query both
    # must be recalled with EQUAL scores — an empty description contributes
    # nothing and a non-matching blurb must not perturb its sibling (no
    # blank-line/dynamic-mapping artifacts from the new field).
    await ensure_index(es_client, es_index_name)
    await replace_document_chunks(
        es_client,
        index=es_index_name,
        tenant_id=str(DEFAULT_TENANT_ID),
        document_id=uuid4(),
        title="tango",
        tags=[],
        description="",
        chunks=[Chunk(text="golf club", heading_path="tango")],
    )
    await replace_document_chunks(
        es_client,
        index=es_index_name,
        tenant_id=str(DEFAULT_TENANT_ID),
        document_id=uuid4(),
        title="tango",
        tags=[],
        description="papaya",
        chunks=[Chunk(text="golf club", heading_path="tango")],
    )
    await es_client.indices.refresh(index=es_index_name)

    hits = await _search_hydrated(es_client, es_index_name, "golf")

    assert len(hits) == 2, "both siblings must be recalled"
    assert {hit["description"] for hit in hits} == {"", "papaya"}
    assert hits[0]["score"] == pytest.approx(hits[1]["score"], rel=1e-6)
