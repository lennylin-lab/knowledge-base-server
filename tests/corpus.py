"""Shared corpus + provider scripting for retrieval/search tests.

Single-section bodies (>= chunker target 800, <= max 1600 chars) index as
exactly one chunk whose text equals the section verbatim (no whitespace for
the chunker to strip). The scripted provider keys on the pipeline's
EMBEDDING INPUTS — `embedding_input(title, chunk)` = title + heading
breadcrumb + chunk text (see `rag/indexer.py`) — derived here through the
same production functions, so the vector leg gets known neighbors.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from app.rag.chunker import chunk_markdown_structured
from app.rag.indexer import embedding_input
from fakes import ScriptedEmbeddingProvider, basis_vector, vector_at_distance

E0 = basis_vector(0)
E1 = basis_vector(1)
E2 = basis_vector(2)

KOTLIN_SECTION = f"# Kotlin notes\n\n{('zorblat ' * 130).strip()}"
PYTHON_SECTION = f"# Python notes\n\n{('quibnard ' * 130).strip()}"
KOTLIN_CONTENT = f"---\ntitle: Kotlin Notes\ntags: [kotlin]\n---\n\n{KOTLIN_SECTION}"
PYTHON_CONTENT = f"---\ntitle: Python Notes\ntags: [python]\n---\n\n{PYTHON_SECTION}"


def _embed_text(title: str, content: str) -> str:
    """The single chunk's embedding input for a single-section document."""
    chunks = chunk_markdown_structured(content)
    assert len(chunks) == 1, "corpus documents must chunk to exactly one chunk"
    return embedding_input(title, chunks[0])


KOTLIN_EMBED_TEXT = _embed_text("Kotlin Notes", KOTLIN_CONTENT)
PYTHON_EMBED_TEXT = _embed_text("Python Notes", PYTHON_CONTENT)

# BM25-empty query whose scripted embedding equals the Kotlin chunk's —
# isolates the vector leg.
VECTOR_QUERY = "unrelated phrasing"

# The rescue-gate world: a short-keyword query embedding sitting beyond the
# primary ceiling (0.45) yet clustered near the chunks — the granularity
# shift the head-rescue tier exists for.
SHIFTED_QUERY = "short word"
SHIFTED_DISTANCE = 0.55
# A rare-term query whose embedding sits 0.9 from the chunks: beyond the
# rescue cap (0.85), so the vector leg stays silenced — ES-dominated by design.
RESCUE_PROOF_QUERY = "zorblat quibnard"
RESCUE_PROOF_DISTANCE = 0.9


def shifted_scripted_provider() -> ScriptedEmbeddingProvider:
    """Both chunks share E0; `SHIFTED_QUERY` sits at cosine 0.55 from them
    (E0/E2 plane, so orthogonal E1 defaults stay at distance 1.0).

    The primary ceiling empties the whole leg but the head is clustered —
    only the rescue tier can admit it. Unmapped texts land orthogonally:
    nothing rescues, empty over noise still holds.
    """
    provider = ScriptedEmbeddingProvider(default=E1)
    provider.vectors[KOTLIN_EMBED_TEXT] = E0
    provider.vectors[PYTHON_EMBED_TEXT] = E0
    provider.vectors[SHIFTED_QUERY] = vector_at_distance(E0, E2, SHIFTED_DISTANCE)
    provider.vectors[RESCUE_PROOF_QUERY] = vector_at_distance(E0, E2, RESCUE_PROOF_DISTANCE)
    return provider


def neighbor_scripted_provider() -> ScriptedEmbeddingProvider:
    """Kotlin chunk and `VECTOR_QUERY` share E0; everything else gets E1."""
    provider = ScriptedEmbeddingProvider(default=E1)
    provider.vectors[KOTLIN_EMBED_TEXT] = E0
    provider.vectors[VECTOR_QUERY] = E0
    return provider


def distant_scripted_provider() -> ScriptedEmbeddingProvider:
    """Both chunks share E0; every unmapped text (any unrelated query) gets E1.

    The gate-test world: an unrelated query's embedding sits at cosine
    distance 1.0 from both chunks, so the vector relevance gate drops the
    whole leg — nothing may pad the results.
    """
    provider = ScriptedEmbeddingProvider(default=E1)
    provider.vectors[KOTLIN_EMBED_TEXT] = E0
    provider.vectors[PYTHON_EMBED_TEXT] = E0
    return provider


async def seed_corpus(
    seeder: Callable[[ScriptedEmbeddingProvider, str], Awaitable[UUID]],
    provider: ScriptedEmbeddingProvider,
) -> tuple[UUID, UUID]:
    """Seed the two-document corpus; returns (kotlin_id, python_id)."""
    kotlin_id = await seeder(provider, KOTLIN_CONTENT)
    python_id = await seeder(provider, PYTHON_CONTENT)
    return kotlin_id, python_id
