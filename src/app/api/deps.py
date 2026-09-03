"""Shared FastAPI dependencies for the v1 API."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Annotated, ClassVar
from uuid import UUID

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
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
from app.rag.worker import INDEX_DOCUMENT_TASK
from app.search.es import get_shared_es_client
from app.services.agents import AssociationService, SummarizeService, WritingService
from app.services.chat import ChatService
from app.services.document import DocumentService, ReindexEnqueuer
from app.services.search import SearchService
from app.services.session import ChatSessionService

logger = structlog.get_logger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_db)]


def get_document_service(session: SessionDep, background_tasks: BackgroundTasks) -> DocumentService:
    """One service per request, sharing the request's session.

    The FastAPI adapter for the write-path indexing trigger: the service only
    sees an injected enqueuer; this is the sole place BackgroundTasks appears.
    """
    return DocumentService(session, enqueuer=make_index_enqueuer(background_tasks))


# --- indexing enqueue transport (BackgroundTasks | ARQ) ---


def make_index_enqueuer(background_tasks: BackgroundTasks) -> ReindexEnqueuer:
    """Pick the indexing transport once, at service construction.

    Empty `REDIS_URL` (the default) keeps today's in-process BackgroundTasks
    path exactly — no Redis client is ever constructed. A configured URL
    routes enqueueing to the shared ARQ pool instead; BackgroundTasks then
    carries nothing (it stays solely the fallback's transport).
    """
    if not get_settings().REDIS_URL:
        return lambda doc_id: background_tasks.add_task(run_indexing, doc_id)
    return _ArqEnqueuer()


_shared_arq_pool: ArqRedis | None = None


async def _get_shared_arq_pool() -> ArqRedis:
    """The one ARQ pool per process, built lazily on first enqueue (the
    `get_shared_es_client` pattern): enqueuers are constructed per request
    but must never open a pool per request. Tests pre-set the module global
    to stub the pool."""
    global _shared_arq_pool
    if _shared_arq_pool is None:
        _shared_arq_pool = await create_pool(RedisSettings.from_dsn(get_settings().REDIS_URL))
    return _shared_arq_pool


async def close_arq_pool() -> None:
    """App-shutdown hook: close the shared pool if this process built one
    (a no-op in BackgroundTasks mode — the pool never exists)."""
    global _shared_arq_pool
    if _shared_arq_pool is not None:
        await _shared_arq_pool.aclose(close_connection_pool=True)
        _shared_arq_pool = None


class _ArqEnqueuer:
    """Enqueues indexing jobs on the shared ARQ pool.

    `__call__` keeps the sync `ReindexEnqueuer` shape the service expects;
    the async enqueue runs as a fire-and-forget task on the request's event
    loop. A Redis problem at enqueue time degrades loudly but safely: the
    write itself is already committed, so `index_enqueue_failed` is warned,
    the document stays `pending`, and the CLI reindex sweep finishes it —
    the CRUD response never 5xx-es over a queue hiccup.
    """

    # A live reference per fire-and-forget task, or the GC may collect it
    # mid-flight; done-callbacks keep the set bounded.
    _inflight: ClassVar[set[asyncio.Task[None]]] = set()

    def __call__(self, doc_id: UUID) -> None:
        task = asyncio.create_task(self._enqueue(doc_id))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _enqueue(self, doc_id: UUID) -> None:
        try:
            pool = await _get_shared_arq_pool()
            await pool.enqueue_job(INDEX_DOCUMENT_TASK, str(doc_id))
            logger.info("index_enqueued", document_id=str(doc_id), mode="arq")
        except Exception as exc:
            logger.warning(
                "index_enqueue_failed",
                document_id=str(doc_id),
                error_class=type(exc).__name__,
            )


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]


def get_chat_session_service(session: SessionDep) -> ChatSessionService:
    """One service per request, sharing the request's session."""
    return ChatSessionService(session)


ChatSessionServiceDep = Annotated[ChatSessionService, Depends(get_chat_session_service)]


def embedding_provider_from_settings(settings: Settings) -> OpenAIEmbeddingProvider | None:
    """Build the embedding provider only when an API key is configured.

    `None` means BM25-only search — a user-visible degradation mode, not a
    failure (see design.md); the accompanying warning is emitted by
    `build_search_service` so it fires once per construction.
    """
    if settings.EMBEDDING_API_KEY.get_secret_value():
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
        logger.warning("vector_search_disabled", reason="embedding_api_key_not_configured")
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
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Chat is not configured: set CHAT_API_KEY to enable it",
        )
    # External MCP tools ride along when the (process-lifetime) manager is up
    # with a non-empty tool snapshot; unconfigured deployments build none and
    # the agent is exactly the pre-MCP one. The snapshot is taken once here —
    # config changes need a restart (documented, no hot reload).
    manager = get_mcp_manager()
    extra_tools = build_agent_tools(manager, manager.list_tools()) if manager.running else []
    # Since provider-config isolation the embedding key is independent of
    # CHAT_API_KEY: chat with only a chat key wires a BM25-only retriever,
    # and run_started.mode must report that truthfully (writing's pattern).
    provider = embedding_provider_from_settings(settings)
    return ChatService(
        _build_retriever(settings, provider),
        get_chat_model(settings),
        mode="hybrid" if provider is not None else "bm25",
        extra_tools=extra_tools,
        # Session persistence: the process-lifetime service opens one session
        # per ask() via the factory (the SummarizeService lifetime pattern).
        session_factory=SessionFactory,
        history_char_budget=settings.CHAT_HISTORY_CHAR_BUDGET,
    )


@lru_cache(maxsize=1)
def get_chat_service() -> ChatService:
    """Cached accessor, like `get_search_service`: one model, one SDK client
    per process. `lru_cache` never memoizes raised exceptions, so the no-key
    path still fails every request with `ChatUnavailableError`."""
    return build_chat_service(get_settings())


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]


def build_summarize_service(settings: Settings) -> SummarizeService:
    """Wire the summarize service from Settings (uncached constructor).

    Same no-key gate as chat: without `CHAT_API_KEY` this raises
    `ChatUnavailableError` before any document load or model call. Summarize
    has no non-LLM fallback either, so it reuses chat's error (503
    `chat_unavailable`) rather than inventing a near-identical one. The
    configured `CHAT_MODEL` name is passed alongside the model so responses
    echo it even when tests inject a FunctionModel stand-in.
    """
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Summarize is not configured: set CHAT_API_KEY to enable it",
        )
    return SummarizeService(
        get_chat_model(settings),
        settings.CHAT_MODEL,
        session_factory=SessionFactory,
    )


@lru_cache(maxsize=1)
def get_summarize_service() -> SummarizeService:
    """Cached accessor like `get_chat_service`: one model and SDK client per
    process. The service opens its own session per run via the injected
    session factory (the Retriever pattern), so it needs no request-scoped
    session — and the no-key path still fails every request."""
    return build_summarize_service(get_settings())


SummarizeServiceDep = Annotated[SummarizeService, Depends(get_summarize_service)]


def build_association_service(settings: Settings) -> AssociationService:
    """Wire the association service from Settings (uncached constructor).

    Same no-key gate as chat and summarize: without `CHAT_API_KEY` this
    raises `ChatUnavailableError` before any document load, candidate query,
    or model call. Reuses chat's error (503 `chat_unavailable`) for the same
    one-no-LLM-fallback-gate reason as summarize.
    """
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Associations are not configured: set CHAT_API_KEY to enable it",
        )
    return AssociationService(
        get_chat_model(settings),
        settings.CHAT_MODEL,
        session_factory=SessionFactory,
    )


@lru_cache(maxsize=1)
def get_association_service() -> AssociationService:
    """Cached accessor like `get_summarize_service`: one model and SDK client
    per process, own session per run, and the no-key path still fails every
    request."""
    return build_association_service(get_settings())


AssociationServiceDep = Annotated[AssociationService, Depends(get_association_service)]


def build_writing_service(settings: Settings) -> WritingService:
    """Wire the writing service from Settings (uncached constructor).

    Same no-key gate as chat: without `CHAT_API_KEY` this raises
    `ChatUnavailableError` BEFORE any stream can start (a clean 503 envelope,
    never a half-open stream). Unlike chat's constructor, the retriever may
    legitimately degrade to BM25 when no embedding key is configured —
    retrieval is optional for writing, so such runs simply report
    `mode: "bm25"`. Wrapped MCP tools are not wired into writing yet; the
    agent's `extra_tools` seam stays empty until that later task.

    Ordering note: FastAPI resolves dependencies before body validation, so
    an unconfigured deployment answers 503 `chat_unavailable` even for
    invalid request bodies (see `build_chat_service`).
    """
    if not settings.CHAT_API_KEY.get_secret_value():
        raise ChatUnavailableError(
            "Writing assistance is not configured: set CHAT_API_KEY to enable it",
        )
    provider = embedding_provider_from_settings(settings)
    if provider is None:
        logger.warning("vector_search_disabled", reason="embedding_api_key_not_configured")
    return WritingService(
        _build_retriever(settings, provider),
        get_chat_model(settings),
        mode="hybrid" if provider is not None else "bm25",
    )


@lru_cache(maxsize=1)
def get_writing_service() -> WritingService:
    """Cached accessor like `get_chat_service`: one model, one retriever and
    one SDK client per process; `lru_cache` never memoizes raised exceptions,
    so the no-key path still fails every request."""
    return build_writing_service(get_settings())


WritingServiceDep = Annotated[WritingService, Depends(get_writing_service)]
