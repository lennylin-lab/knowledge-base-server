"""Embedding provider abstraction — provider plumbing only, no domain logic.

Retries and timeouts live in the SDK client built here (and nowhere else);
callers pass already-sized batches and get vectors back in input order.
"""

from __future__ import annotations

import hashlib
import time
from array import array
from typing import Protocol, runtime_checkable

import openai
import structlog
from openai import AsyncOpenAI, Omit

from app.core.cache import Cache, cache_key
from app.core.config import Settings
from app.core.exceptions import LLMProviderError, LLMRateLimitedError

logger = structlog.get_logger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns a batch of texts into fixed-dimension vectors, in input order."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    """OpenAI-compatible embeddings via the SDK (base_url/key/model from Settings)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        # Injection seam for tests; production always builds its own client so
        # retries/timeouts are configured in exactly one place.
        self._client = client or AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAIEmbeddingProvider:
        """Wire the provider from application Settings."""
        return cls(
            base_url=settings.EMBEDDING_BASE_URL,
            api_key=settings.EMBEDDING_API_KEY.get_secret_value(),
            model=settings.EMBEDDING_MODEL,
            dimensions=settings.EMBEDDING_DIM,
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch; SDK-level retries run before errors surface here."""
        if not texts:
            return []
        started = time.perf_counter()
        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=list(texts),
                # encoding_format must be explicit: the SDK defaults to "base64",
                # which OpenRouter-backed Nvidia embedding models reject with a 400.
                encoding_format="float",
                # Omit() keeps the parameter absent unless a width is configured.
                dimensions=self._dimensions if self._dimensions is not None else Omit(),
            )
        except openai.RateLimitError as exc:
            raise LLMRateLimitedError("Embedding provider rate limit exceeded") from exc
        except openai.APIError as exc:
            # Retries are exhausted at this point (SDK max_retries). Message
            # stays generic; provider details belong to logs, not responses.
            raise LLMProviderError("Embedding provider request failed") from exc
        vectors = [item.embedding for item in response.data]
        if len(vectors) != len(texts):
            raise LLMProviderError(
                "Embedding provider returned a mismatched number of vectors",
                details={"expected": len(texts), "received": len(vectors)},
            )
        if self._dimensions is not None and any(
            len(vector) != self._dimensions for vector in vectors
        ):
            # Without this the failure surfaces later as an opaque pgvector
            # insert/query error against the fixed-width embedding column.
            received = len(next(v for v in vectors if len(v) != self._dimensions))
            raise LLMProviderError(
                "Embedding provider returned a vector with an unexpected dimension",
                details={"expected": self._dimensions, "received": received},
            )
        logger.info(
            "embeddings_completed",
            model=self._model,
            text_count=len(texts),
            total_tokens=response.usage.total_tokens if response.usage else None,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return vectors

    async def aclose(self) -> None:
        """Close the underlying SDK client; owned by whoever constructed it."""
        await self._client.close()


def _encode_vector(vector: list[float]) -> bytes:
    """float64 little-endian bytes: round-trips the provider's Python floats
    bit-exactly, so a cache hit returns the exact vector a miss would (a
    lossy encoding would change pgvector distances depending on cache state)."""
    return array("d", vector).tobytes()


def _decode_vector(raw: bytes) -> list[float]:
    """Inverse of `_encode_vector`."""
    return list(array("d", raw))


class CachingEmbeddingProvider:
    """`EmbeddingProvider` decorator: per-text vector cache over any provider.

    The single choke point for both search-query and indexing-time embedding.
    Each text of a batch is checked individually; only the misses reach the
    wrapped provider, and vectors are reassembled in input order (a fully
    cached batch makes no provider call). Cache ops are best-effort by the
    `Cache` contract — a Redis fault here is just a recompute.
    """

    def __init__(
        self,
        inner: EmbeddingProvider,
        cache: Cache,
        *,
        model: str,
        dim: int,
        ttl_seconds: int,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._model = model
        self._dim = dim
        self._ttl_seconds = ttl_seconds

    def _key(self, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # Model and dim are in the key: changing either changes the output
        # (provider-config isolation lets the embedding model differ from the
        # chat model).
        return cache_key("emb", self._model, self._dim, digest)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Cached batch embed: hits from the cache, misses from the provider."""
        if not texts:
            return []
        keys = [self._key(text) for text in texts]
        results: list[list[float]] = []
        misses: list[int] = []
        for index, key in enumerate(keys):
            raw = await self._cache.get(key)
            if raw is not None and len(raw) == self._dim * 8:
                results.append(_decode_vector(raw))
            else:
                results.append([])
                misses.append(index)
        hits = len(texts) - len(misses)
        if hits:
            logger.info("cache_hit", domain="embedding", count=hits)
        if misses:
            logger.info("cache_miss", domain="embedding", count=len(misses))
            vectors = await self._inner.embed_texts([texts[i] for i in misses])
            if len(vectors) != len(misses):
                # Surface the wrapped provider's contract violation as-is; the
                # provider itself already validates its own outputs.
                raise LLMProviderError("Embedding provider returned a mismatched number of vectors")
            for index, vector in zip(misses, vectors, strict=True):
                results[index] = vector
                await self._cache.set(
                    keys[index], _encode_vector(vector), ttl_seconds=self._ttl_seconds
                )
        return results
