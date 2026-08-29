"""Shared FastAPI dependencies for the v1 API."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

import structlog
from fastapi import BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import SessionFactory, get_db
from app.core.exceptions import ChatUnavailableError
from app.llm.embeddings import OpenAIEmbeddingProvider
from app.llm.models import get_chat_model
from app.mcp.manager import get_mcp_manager
from app.mcp.tools import build_agent_tools
from app.rag.indexer import run_indexing
from app.rag.retriever import Retriever
from app.search.es import get_shared_es_client
from app.services.chat import ChatService
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


def _build_retriever(settings: Settings, provider: OpenAIEmbeddingProvider | None) -> Retriever:
    """Shared retriever wiring for every retrieval-backed service."""
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory
    return Retriever(
        session_factory=session_factory,
        es_client=get_shared_es_client(),
        embedding_provider=provider,
        es_index=settings.ES_INDEX,
    )


def build_search_service(settings: Settings) -> SearchService:
    """Wire the search service from Settings (uncached constructor)."""
    provider = embedding_provider_from_settings(settings)
    if provider is None:
        logger.warning("vector_search_disabled", reason="openai_api_key_not_configured")
    return SearchService(_build_retriever(settings, provider))


@lru_cache(maxsize=1)
def get_search_service() -> SearchService:
    """Cached accessor: one retriever/service (and one degradation warning)
    per process; the ES transport is shared via `search.es`, never opened
    per request."""
    return build_search_service(get_settings())


SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]


def build_chat_service(settings: Settings) -> ChatService:
    """Wire the chat service from Settings (uncached constructor).

    Chat has no non-LLM fallback: without an API key this raises
    `ChatUnavailableError` BEFORE any stream can start (a clean 503 envelope,
    never a half-open stream). The retrieval leg may still degrade to BM25 at
    search time — that is the retriever's own per-run behavior.

    Ordering note: FastAPI resolves dependencies before body validation, so an
    unconfigured deployment answers 503 `chat_unavailable` even for invalid
    request bodies — the same contract as any gate-style (e.g. auth)
    dependency. The 422 request-schema contract holds whenever the service is
    constructible (key present).
    """
    if not settings.OPENAI_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Chat is not configured: set OPENAI_API_KEY to enable it",
        )
    # External MCP tools ride along when the (process-lifetime) manager is up
    # with a non-empty tool snapshot; unconfigured deployments build none and
    # the agent is exactly the pre-MCP one. The snapshot is taken once here —
    # config changes need a restart (documented, no hot reload).
    manager = get_mcp_manager()
    extra_tools = build_agent_tools(manager, manager.list_tools()) if manager.running else []
    # With a non-empty key the embedding provider always exists, so the chat
    # retriever is always wired hybrid; BM25-only chat is not a state this
    # constructor can produce.
    return ChatService(
        _build_retriever(settings, embedding_provider_from_settings(settings)),
        get_chat_model(settings),
        mode="hybrid",
        extra_tools=extra_tools,
    )


@lru_cache(maxsize=1)
def get_chat_service() -> ChatService:
    """Cached accessor, like `get_search_service`: one model, one SDK client
    per process. `lru_cache` never memoizes raised exceptions, so the no-key
    path still fails every request with `ChatUnavailableError`."""
    return build_chat_service(get_settings())


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
