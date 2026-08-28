"""Offline test doubles shared across test modules (importable as `fakes`)."""

from __future__ import annotations

import hashlib
import math
import random
from typing import Any
from uuid import UUID

from app.models.document_chunk import EMBEDDING_DIM


class FakeEmbeddingProvider:
    """Deterministic offline stand-in: hash-seeded, unit-norm vectors.

    Identical texts embed to identical vectors; `error` (if set) is raised on
    the next call to script provider-failure paths.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim
        self.calls: list[list[str]] = []
        self.error: Exception | None = None

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(self._dim)]
        norm = math.sqrt(sum(component * component for component in vector))
        return [component / norm for component in vector]


class RecordingEsStore:
    """Stands in for search.es.ensure_index / replace_document_chunks."""

    def __init__(self) -> None:
        self.ensure_calls: list[str] = []
        self.replace_calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def ensure_index(self, client: object, index: str) -> None:
        self.ensure_calls.append(index)
        if self.error is not None:
            raise self.error

    async def replace_chunks(
        self,
        client: object,
        *,
        index: str,
        document_id: UUID,
        title: str,
        tags: list[str],
        chunks: list[str],
    ) -> None:
        self.replace_calls.append(
            {
                "index": index,
                "document_id": document_id,
                "title": title,
                "tags": list(tags),
                "chunks": list(chunks),
            }
        )
        if self.error is not None:
            raise self.error


class StubEsClient:
    """Just enough client for pipeline construction and aclose()."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True
