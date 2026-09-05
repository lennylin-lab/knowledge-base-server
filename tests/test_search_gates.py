"""Relevance gate units: pure helper behavior, offline (no db/es).

Covers the post-fusion relative floor, the per-leg absolute gates, and the
Settings/Retriever default drift guard.
"""

from __future__ import annotations

from uuid import UUID

from app.rag.retriever import (
    DEFAULT_BM25_MIN_SCORE,
    DEFAULT_RRF_MIN_RELATIVE,
    DEFAULT_VECTOR_MAX_DISTANCE,
    ChunkKey,
    FusedHit,
    apply_relative_score_floor,
    filter_es_hits,
    filter_vector_rows,
)
from app.repositories.document_chunk import ChunkRow
from app.search.es import EsChunkHit
from fakes import hermetic_settings


def _fused(*scores: float) -> list[FusedHit]:
    """Fused hits in RRF order (score desc) with throwaway keys."""
    return [
        FusedHit(
            key=ChunkKey(UUID(int=index), 0),
            score=score,
            es_rank=index + 1,
            vector_rank=None,
        )
        for index, score in enumerate(scores)
    ]


def _row(distance: float | None) -> ChunkRow:
    return ChunkRow(
        document_id=UUID(int=0),
        chunk_index=0,
        content="content",
        document_title="Notes",
        document_tags=[],
        distance=distance,
    )


def _es_hit(score: float) -> EsChunkHit:
    return EsChunkHit(document_id=UUID(int=0), chunk_index=0, score=score)


# --- apply_relative_score_floor (post-fusion relative gate) ---


def test_relative_floor_empty_input_passes_through():
    assert apply_relative_score_floor([], min_relative=DEFAULT_RRF_MIN_RELATIVE) == []


def test_relative_floor_zero_disables_gate():
    hits = _fused(1.0, 0.2, 0.01)
    assert apply_relative_score_floor(hits, min_relative=0.0) == hits


def test_relative_floor_keeps_single_hit_at_any_floor():
    hits = _fused(0.5)
    assert apply_relative_score_floor(hits, min_relative=0.99) == hits


def test_relative_floor_drops_trailing_weak_hits():
    hits = _fused(1.0, 0.4, 0.3)
    kept = apply_relative_score_floor(hits, min_relative=0.35)

    assert [hit.score for hit in kept] == [1.0, 0.4]


def test_relative_floor_keeps_hit_exactly_at_cutoff():
    hits = _fused(1.0, 0.35)
    assert apply_relative_score_floor(hits, min_relative=0.35) == hits


def test_relative_floor_non_positive_top_keeps_nothing():
    assert apply_relative_score_floor(_fused(0.0, 0.0), min_relative=0.35) == []


# --- filter_es_hits (BM25 absolute gate) ---


def test_es_gate_zero_floor_disables_gate():
    hits = [_es_hit(0.0), _es_hit(0.5)]
    assert filter_es_hits(hits, min_score=0.0) == hits


def test_es_gate_drops_hits_below_floor_keeping_boundary():
    hits = [_es_hit(2.0), _es_hit(0.9), _es_hit(1.0)]
    kept = filter_es_hits(hits, min_score=1.0)

    assert [hit.score for hit in kept] == [2.0, 1.0]


def test_es_gate_empty_input_passes_through():
    assert filter_es_hits([], min_score=1.0) == []


# --- filter_vector_rows (vector distance gate) ---


def test_vector_gate_max_distance_disables_gate():
    rows = [_row(2.0), _row(1.0)]
    assert filter_vector_rows(rows, max_distance=2.0) == rows


def test_vector_gate_drops_rows_beyond_ceiling_keeping_boundary():
    rows = [_row(0.1), _row(0.5), _row(0.45)]
    kept = filter_vector_rows(rows, max_distance=0.45)

    assert [row.distance for row in kept] == [0.1, 0.45]


def test_vector_gate_row_without_distance_passes():
    rows = [_row(None)]
    assert filter_vector_rows(rows, max_distance=0.45) == rows


def test_vector_gate_empty_input_passes_through():
    assert filter_vector_rows([], max_distance=0.45) == []


# --- Settings <-> Retriever default drift guard ---


def test_settings_gate_defaults_match_retriever_defaults():
    settings = hermetic_settings()

    assert settings.SEARCH_BM25_MIN_SCORE == DEFAULT_BM25_MIN_SCORE
    assert settings.SEARCH_VECTOR_MAX_DISTANCE == DEFAULT_VECTOR_MAX_DISTANCE
    assert settings.SEARCH_RRF_MIN_RELATIVE == DEFAULT_RRF_MIN_RELATIVE
