"""Embedding provider abstraction — provider plumbing only, no domain logic.

Retries and timeouts live in the SDK client built here (and nowhere else);
callers pass already-sized batches and get vectors back in input order.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

import openai
import structlog
from openai import AsyncOpenAI, Omit

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
