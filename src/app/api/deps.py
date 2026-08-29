"""Shared FastAPI dependencies for the v1 API."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

import structlog
from fastapi import BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import SessionFactory, get_db
from app.llm.embeddings import OpenAIEmbeddingProvider
from app.rag.indexer import run_indexing
from app.rag.retriever import Retriever
from app.search.es import get_shared_es_client
from app.services.document import DocumentService
from app.services.search import SearchService

logger = structlog.get_logger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_document_service(session: SessionDep, background_tasks: BackgroundTasks) -> DocumentService:
    """One service per request, sharing the request's session.

    The FastAPI adapter for the write-path indexing trigger: the service only
    sees an injected enqueuer; this is the sole place BackgroundTasks appears.
    """
    return DocumentService(
        session,
        enqueuer=lambda doc_id: background_tasks.add_task(run_indexing, doc_id),
    )


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def embedding_provider_from_settings(settings: Settings) -> OpenAIEmbeddingProvider | None:
    """Build the embedding provider only when an API key is configured.

    `None` means BM25-only search — a user-visible degradation mode, not a
    failure (see design.md); the accompanying warning is emitted by
    `build_search_service` so it fires once per construction.
    """
    if settings.OPENAI_API_KEY.get_secret_value():
        return OpenAIEmbeddingProvider.from_settings(settings)
    return None


def build_search_service(settings: Settings) -> SearchService:
    """Wire the search service from Settings (uncached constructor)."""
    provider = embedding_provider_from_settings(settings)
    if provider is None:
        logger.warning("vector_search_disabled", reason="openai_api_key_not_configured")
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory
    return SearchService(
        Retriever(
            session_factory=session_factory,
            es_client=get_shared_es_client(),
            embedding_provider=provider,
            es_index=settings.ES_INDEX,
        )
    )


@lru_cache(maxsize=1)
def get_search_service() -> SearchService:
    """Cached accessor: one retriever/service (and one degradation warning)
    per process; the ES transport is shared via `search.es`, never opened
    per request."""
    return build_search_service(get_settings())


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
