"""Shared corpus + provider scripting for retrieval/search tests.

Single-section bodies (>= chunker target 800, <= max 1600 chars) index as
exactly one chunk whose text equals the section verbatim (no whitespace for
the chunker to strip), so the scripted provider can key on chunk texts
deterministically and the vector leg gets known neighbors.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from fakes import ScriptedEmbeddingProvider, basis_vector

E0 = basis_vector(0)
E1 = basis_vector(1)

KOTLIN_SECTION = f"# Kotlin notes\n\n{('zorblat ' * 130).strip()}"
PYTHON_SECTION = f"# Python notes\n\n{('quibnard ' * 130).strip()}"
KOTLIN_CONTENT = f"---\ntitle: Kotlin Notes\ntags: [kotlin]\n---\n\n{KOTLIN_SECTION}"
PYTHON_CONTENT = f"---\ntitle: Python Notes\ntags: [python]\n---\n\n{PYTHON_SECTION}"

# BM25-empty query whose scripted embedding equals the Kotlin chunk's —
# isolates the vector leg.
VECTOR_QUERY = "unrelated phrasing"


def neighbor_scripted_provider() -> ScriptedEmbeddingProvider:
    """Kotlin chunk and `VECTOR_QUERY` share E0; everything else gets E1."""
    provider = ScriptedEmbeddingProvider(default=E1)
    provider.vectors[KOTLIN_SECTION] = E0
    provider.vectors[VECTOR_QUERY] = E0
    return provider


def distant_scripted_provider() -> ScriptedEmbeddingProvider:
    """Both chunks share E0; every unmapped text (any unrelated query) gets E1.

    The gate-test world: an unrelated query's embedding sits at cosine
    distance 1.0 from both chunks, so the vector relevance gate drops the
    whole leg — nothing may pad the results.
    """
    provider = ScriptedEmbeddingProvider(default=E1)
    provider.vectors[KOTLIN_SECTION] = E0
    provider.vectors[PYTHON_SECTION] = E0
    return provider


async def seed_corpus(
    seeder: Callable[[ScriptedEmbeddingProvider, str], Awaitable[UUID]],
    provider: ScriptedEmbeddingProvider,
) -> tuple[UUID, UUID]:
    """Seed the two-document corpus; returns (kotlin_id, python_id)."""
    kotlin_id = await seeder(provider, KOTLIN_CONTENT)
    python_id = await seeder(provider, PYTHON_CONTENT)
    return kotlin_id, python_id
