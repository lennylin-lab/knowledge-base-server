"""RRF fusion: hand-computed scores, ordering, determinism (pure, offline)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.rag.retriever import RRF_K, ChunkKey, fuse_rrf


def key(seed: int) -> ChunkKey:
    """Deterministic distinct key (`seed` as the chunk index)."""
    return ChunkKey(uuid4(), seed)


A = key(1)
B = key(2)
C = key(3)
D = key(4)


def test_both_legs_fuse_with_hand_computed_scores_and_order():
    hits = fuse_rrf([A, B, C], [B, D])

    # es ranks: A=1 B=2 C=3; vector ranks: B=1 D=2 (k=60)
    assert [(hit.key, hit.score, hit.es_rank, hit.vector_rank) for hit in hits] == [
        (B, pytest.approx(1 / 62 + 1 / 61), 2, 1),
        (A, pytest.approx(1 / 61), 1, None),
        (D, pytest.approx(1 / 62), None, 2),
        (C, pytest.approx(1 / 63), 3, None),
    ]


def test_single_leg_preserves_rank_order_with_other_rank_none():
    hits = fuse_rrf([C, A], [])

    assert [hit.key for hit in hits] == [C, A]
    assert [(hit.es_rank, hit.vector_rank) for hit in hits] == [(1, None), (2, None)]


def test_vector_only_leg_reports_es_rank_none():
    hits = fuse_rrf([], [B, D])

    assert [(hit.key, hit.vector_rank, hit.es_rank) for hit in hits] == [
        (B, 1, None),
        (D, 2, None),
    ]


def test_empty_legs_yield_empty_result():
    assert fuse_rrf([], []) == []


def test_equal_scores_break_ties_deterministically_by_es_rank():
    # Symmetric ranks (1+2 vs 2+1): identical scores, so es_rank decides.
    # A vector-only tie with equal es_rank cannot occur with real legs (a key
    # absent from ES gets es_rank None, and two vector-only keys have distinct
    # vector ranks — hence distinct scores); the later tie-break components
    # exist for totality, not reachability.
    hits = fuse_rrf([A, B], [B, A])

    assert [hit.key for hit in hits] == [A, B]
    assert hits[0].score == pytest.approx(hits[1].score)
    assert [hit.es_rank for hit in hits[:2]] == [1, 2]

    # Same shape with unrelated keys: outcome depends only on ranks, not identity.
    x, y = key(5), key(6)
    mirrored = fuse_rrf([x, y, A], [y, x])
    assert [hit.key for hit in mirrored] == [x, y, A]


def test_duplicate_keys_within_a_leg_keep_first_rank():
    hits = fuse_rrf([A, A, B], [])

    assert [(hit.key, hit.es_rank) for hit in hits] == [(A, 1), (B, 2)]


def test_k_is_parameterizable_and_defaults_to_the_standard_60():
    assert fuse_rrf([A], [], k=1)[0].score == pytest.approx(1 / 2)
    assert fuse_rrf([A], [])[0].score == pytest.approx(1 / (RRF_K + 1))
