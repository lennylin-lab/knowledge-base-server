"""Relevance gate units: pure helper behavior, offline (no db/es).

Covers the post-fusion relative floor, the per-leg absolute gates, the
two-tier vector rescue gate, the query length cap, and the Settings/Retriever
default drift guard.
"""

from __future__ import annotations

from uuid import UUID

from app.rag.retriever import (
    DEFAULT_BM25_MIN_SCORE,
    DEFAULT_MAX_QUERY_LENGTH,
    DEFAULT_RRF_MIN_RELATIVE,
    DEFAULT_VECTOR_MAX_DISTANCE,
    DEFAULT_VECTOR_RESCUE_MARGIN,
    DEFAULT_VECTOR_RESCUE_MAX_DISTANCE,
    ChunkKey,
    FusedHit,
    apply_relative_score_floor,
    filter_es_hits,
    filter_vector_rows,
    filter_vector_rows_with_rescue,
    truncate_query,
)
from app.repositories.document_chunk import ChunkRow
from app.search.es import EsChunkHit
from app.search.queries import DEFAULT_BM25_MIN_COVERAGE
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


# --- filter_vector_rows_with_rescue (two-tier vector gate) ---


def test_rescue_gate_primary_survivors_skip_rescue():
    rows = [_row(0.1), _row(0.9)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=0.45, rescue_margin=0.15, rescue_max_distance=0.85
    )

    assert [row.distance for row in kept] == [0.1]
    assert rescued == 0


def test_rescue_gate_admits_clustered_head_when_primary_empties():
    rows = [_row(0.50), _row(0.55), _row(0.80)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=0.45, rescue_margin=0.15, rescue_max_distance=0.85
    )

    # Window = min(0.50 + 0.15, 0.85) = 0.65: the clustered head, not the tail.
    assert [row.distance for row in kept] == [0.50, 0.55]
    assert rescued == 2


def test_rescue_gate_cap_binds_the_window():
    rows = [_row(0.70), _row(0.71)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=0.45, rescue_margin=0.15, rescue_max_distance=0.85
    )

    # Window = min(0.85, 0.85): both rows exactly at the cap survive.
    assert [row.distance for row in kept] == [0.70, 0.71]
    assert rescued == 2


def test_rescue_gate_cap_keeps_high_leg_min_silent():
    rows = [_row(0.90), _row(0.95)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=0.45, rescue_margin=0.15, rescue_max_distance=0.85
    )

    # leg_min 0.90 puts the whole window above the cap: rare-term keywords
    # stay ES-dominated.
    assert kept == []
    assert rescued == 0


def test_rescue_gate_margin_sentinel_disables_rescue():
    rows = [_row(0.50), _row(0.55)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=0.45, rescue_margin=0.0, rescue_max_distance=0.85
    )

    assert kept == []
    assert rescued == 0


def test_rescue_gate_cap_sentinel_disables_rescue():
    rows = [_row(0.50)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=0.45, rescue_margin=0.15, rescue_max_distance=0.0
    )

    assert kept == []
    assert rescued == 0


def test_rescue_gate_row_without_distance_passes_primary():
    rows = [_row(None), _row(0.9)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=0.45, rescue_margin=0.15, rescue_max_distance=0.85
    )

    # The fail-open row survives the primary tier, so the rescue branch —
    # which judges measured distances only — never runs.
    assert [row.distance for row in kept] == [None]
    assert rescued == 0


def test_rescue_gate_full_ceiling_sentinel_disables_everything():
    rows = [_row(1.5)]
    kept, rescued = filter_vector_rows_with_rescue(
        rows, max_distance=2.0, rescue_margin=0.15, rescue_max_distance=0.85
    )

    assert kept == rows
    assert rescued == 0


def test_rescue_gate_empty_input_passes_through():
    assert filter_vector_rows_with_rescue(
        [], max_distance=0.45, rescue_margin=0.15, rescue_max_distance=0.85
    ) == ([], 0)


# --- truncate_query (query length cap) ---


def test_truncate_over_long_query_to_cap():
    truncated = truncate_query("x" * 300, max_length=DEFAULT_MAX_QUERY_LENGTH)

    assert truncated == "x" * DEFAULT_MAX_QUERY_LENGTH


def test_truncate_leaves_query_at_exact_bound_untouched():
    query = "字" * DEFAULT_MAX_QUERY_LENGTH

    assert truncate_query(query, max_length=DEFAULT_MAX_QUERY_LENGTH) == query


def test_truncate_disabled_sentinel_keeps_query_whole():
    query = "x" * 1000

    assert truncate_query(query, max_length=0) == query


# --- Settings <-> Retriever default drift guard ---


def test_settings_gate_defaults_match_retriever_defaults():
    settings = hermetic_settings()

    assert settings.SEARCH_BM25_MIN_SCORE == DEFAULT_BM25_MIN_SCORE
    assert settings.SEARCH_VECTOR_MAX_DISTANCE == DEFAULT_VECTOR_MAX_DISTANCE
    assert settings.SEARCH_VECTOR_RESCUE_MARGIN == DEFAULT_VECTOR_RESCUE_MARGIN
    assert settings.SEARCH_VECTOR_RESCUE_MAX_DISTANCE == DEFAULT_VECTOR_RESCUE_MAX_DISTANCE
    assert settings.SEARCH_RRF_MIN_RELATIVE == DEFAULT_RRF_MIN_RELATIVE
    assert settings.SEARCH_MAX_QUERY_LENGTH == DEFAULT_MAX_QUERY_LENGTH


def test_settings_coverage_default_matches_query_builder_default():
    # The coverage gate's single source: Settings.SEARCH_BM25_MIN_COVERAGE.
    # The Retriever constructor default IS this constant (imported, not
    # redefined), so one assertion covers the whole wiring chain — Settings,
    # Retriever, and bm25_chunk_query.
    assert hermetic_settings().SEARCH_BM25_MIN_COVERAGE == DEFAULT_BM25_MIN_COVERAGE
