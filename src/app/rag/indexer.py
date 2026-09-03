"""Indexing pipeline: chunk -> embed -> pgvector + Elasticsearch -> status.

`index_status == done` means BOTH stores are populated and consistent. Two
entry shapes share one core: `process_document` / `run_indexing` swallow
stage failures (BackgroundTasks mode — must never raise into a response),
while `process_document_raising` / `run_indexing_raw` propagate them so a
queue worker can classify retry-worthy failures (see `rag/worker.py`). Both
failure paths leave the document retriable via the CLI sweep.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

import structlog
from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.database import SessionFactory
from app.llm.embeddings import EmbeddingProvider, OpenAIEmbeddingProvider
from app.models.document import IndexStatus
from app.rag.chunker import chunk_markdown
from app.repositories.document import DocumentRepository
from app.repositories.document_chunk import DocumentChunkRepository
from app.search.es import ensure_index, get_es_client, replace_document_chunks

logger = structlog.get_logger(__name__)


class EnsureIndexFn(Protocol):
    """Shape of `search.es.ensure_index` (injectable for tests)."""

    async def __call__(self, client: AsyncElasticsearch, index: str) -> None: ...


class ReplaceChunksFn(Protocol):
    """Shape of `search.es.replace_document_chunks` (injectable for tests)."""

    async def __call__(
        self,
        client: AsyncElasticsearch,
        *,
        index: str,
        document_id: UUID,
        title: str,
        tags: Sequence[str],
        chunks: Sequence[str],
    ) -> None: ...


class IndexingPipeline:
    """Whole-document indexing; idempotent (replace semantics in both stores)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        es_client: AsyncElasticsearch,
        es_index: str,
        ensure_index: EnsureIndexFn = ensure_index,
        replace_chunks: ReplaceChunksFn = replace_document_chunks,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._es_client = es_client
        self._es_index = es_index
        self._ensure_index = ensure_index
        self._replace_chunks = replace_chunks

    async def process_document(self, doc_id: UUID) -> IndexStatus | None:
        """Index one document; `None` means it no longer exists (skip).

        Every stage error flips the status to `failed` and is swallowed —
        this runs as a background task and must not propagate.
        """
        try:
            return await self.process_document_raising(doc_id)
        except Exception as exc:
            await self._mark_failed(doc_id, exc)
            return IndexStatus.FAILED

    async def process_document_raising(self, doc_id: UUID) -> IndexStatus | None:
        """The raising core of `process_document` (queue-mode entry).

        Stage errors PROPAGATE — typed provider/search errors, connection
        failures — so a queue worker can classify them for retry; the caller
        owns settling `index_status=failed`. A missing/soft-deleted document
        is still a clean skip (`None`), and success still sets `done` here.
        """
        started = time.perf_counter()
        log = logger.bind(document_id=str(doc_id))
        async with self._session_factory() as session:
            document = await DocumentRepository(session).get_by_id(doc_id)
            if document is None:
                # Background race after a delete: nothing to index.
                log.info("document_index_skipped", reason="missing")
                return None
            chunks = chunk_markdown(document.content)
            vectors = await self._embedding_provider.embed_texts(chunks)
            # Chunks are staging: commit them, status untouched — a later
            # stage failure must still mark the document `failed`.
            await DocumentChunkRepository(session).replace_for_document(
                document.id, chunks, vectors
            )
            await session.commit()
            title, tags = document.title, document.tags
        await self._ensure_index(self._es_client, self._es_index)
        await self._replace_chunks(
            self._es_client,
            index=self._es_index,
            document_id=document.id,
            title=title,
            tags=tags,
            chunks=chunks,
        )
        async with self._session_factory() as session:
            await DocumentRepository(session).set_index_status(document.id, IndexStatus.DONE)
            await session.commit()
        log.info(
            "document_indexed",
            chunk_count=len(chunks),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return IndexStatus.DONE

    async def _mark_failed(self, doc_id: UUID, exc: Exception) -> None:
        """Flip to `failed` in a small follow-up transaction; never raise."""
        # Error class only — chunk text/content never reaches the logs.
        logger.warning(
            "document_index_failed",
            document_id=str(doc_id),
            error_class=type(exc).__name__,
        )
        try:
            async with self._session_factory() as session:
                await DocumentRepository(session).set_index_status(doc_id, IndexStatus.FAILED)
                await session.commit()
        except Exception:
            # The DB itself is unhappy; the doc stays in its previous status
            # and remains retriable via the CLI sweep.
            logger.exception("index_status_update_failed", document_id=str(doc_id))

    async def aclose(self) -> None:
        """Release owned clients; the pipeline takes ownership on construction."""
        await self._es_client.close()
        # Test fakes satisfy only the protocol; the real provider owns an SDK
        # client (httpx pool) that must not outlive the background run.
        if isinstance(self._embedding_provider, OpenAIEmbeddingProvider):
            await self._embedding_provider.aclose()


def build_default_pipeline() -> IndexingPipeline:
    """Production wiring from Settings (app session factory, OpenAI provider, ES)."""
    settings = get_settings()
    return IndexingPipeline(
        session_factory=SessionFactory,
        embedding_provider=OpenAIEmbeddingProvider.from_settings(settings),
        es_client=get_es_client(settings),
        es_index=settings.ES_INDEX,
    )


async def run_indexing(doc_id: UUID) -> None:
    """BackgroundTasks entry point after document create/update."""
    pipeline = build_default_pipeline()
    try:
        await pipeline.process_document(doc_id)
    except Exception:
        # process_document already swallows stage errors; this guards only
        # its own failure paths so the background task can never crash.
        logger.exception("indexing_crashed", document_id=str(doc_id))
    finally:
        await _aclose_quietly(pipeline, doc_id)


async def run_indexing_raw(doc_id: UUID) -> IndexStatus | None:
    """Queue-mode entry point (raising): the pipeline core for the ARQ worker.

    Like `run_indexing`, one pipeline is built and closed per run; unlike it,
    stage errors propagate (typed provider/search/connection classes) so the
    worker can retry them — settling `failed` is the worker's decision, not
    the pipeline's. `None` is the missing-document skip outcome.
    """
    pipeline = build_default_pipeline()
    try:
        return await pipeline.process_document_raising(doc_id)
    finally:
        await _aclose_quietly(pipeline, doc_id)


async def _aclose_quietly(pipeline: IndexingPipeline, doc_id: UUID) -> None:
    """Close owned clients; a failing close (e.g. dead ES transport) must not
    mask the pipeline's real outcome."""
    try:
        await pipeline.aclose()
    except Exception:
        logger.exception("indexing_close_failed", document_id=str(doc_id))
