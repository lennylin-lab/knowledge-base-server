"""Elasticsearch access: client construction, index lifecycle, chunk writes.

Every ES query lives in this package (see directory-structure.md); failures
surface as `SearchIndexError` with the index and error class only — never
document content — in the details.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

import structlog
from elasticsearch import ApiError, AsyncElasticsearch, TransportError
from elasticsearch.helpers import BulkIndexError, async_bulk

from app.core.config import Settings
from app.core.exceptions import SearchIndexError

logger = structlog.get_logger(__name__)

# Everything the ES client (or its bulk helper) can raise; there is no common
# base class shared by the API, transport, and bulk-helper error families.
_ES_ERRORS = (ApiError, TransportError, BulkIndexError)

# Explicit mapping beats dynamic: schema drift becomes visible, keyword fields
# stay filterable, text fields stay analyzed.
_CHUNK_MAPPINGS: dict[str, dict[str, dict[str, str]]] = {
    "properties": {
        "document_id": {"type": "keyword"},
        "title": {"type": "text"},
        "tags": {"type": "keyword"},
        "chunk_text": {"type": "text"},
        "chunk_index": {"type": "integer"},
    }
}


def get_es_client(settings: Settings) -> AsyncElasticsearch:
    """Client from Settings — the only construction site for ES clients."""
    return AsyncElasticsearch(hosts=[settings.ELASTICSEARCH_URL], request_timeout=30)


async def ensure_index(client: AsyncElasticsearch, index: str) -> None:
    """Create the chunk index with an explicit mapping if it is missing."""
    try:
        exists = await client.indices.exists(index=index)
        if not bool(exists):
            await client.indices.create(index=index, mappings=_CHUNK_MAPPINGS)
    except _ES_ERRORS as exc:
        raise _wrap("ensure_index", index, exc) from exc


async def replace_document_chunks(
    client: AsyncElasticsearch,
    *,
    index: str,
    document_id: UUID,
    title: str,
    tags: Sequence[str],
    chunks: Sequence[str],
) -> None:
    """Idempotently replace one document's ES docs: delete-by-document, bulk-index.

    Doc ids are deterministic (`{document_id}:{chunk_index}`), so shrinking a
    document never leaves orphan docs behind. Both steps `refresh` on
    completion: ES is near-real-time, and a re-index arriving inside the
    refresh interval must still see (and delete) the previous version's docs —
    without this, replace would leak orphans.
    """
    try:
        await client.delete_by_query(
            index=index,
            query={"term": {"document_id": str(document_id)}},
            conflicts="proceed",
            refresh=True,
        )
        if chunks:
            await async_bulk(
                client,
                [
                    {
                        "_op_type": "index",
                        "_index": index,
                        "_id": f"{document_id}:{chunk_index}",
                        "_source": {
                            "document_id": str(document_id),
                            "title": title,
                            "tags": list(tags),
                            "chunk_index": chunk_index,
                            "chunk_text": chunk,
                        },
                    }
                    for chunk_index, chunk in enumerate(chunks)
                ],
                refresh=True,
            )
    except _ES_ERRORS as exc:
        raise _wrap("replace_document_chunks", index, exc) from exc


def _wrap(operation: str, index: str, exc: Exception) -> SearchIndexError:
    """Classify an ES failure; error class + index only, no content."""
    logger.warning(
        "search_index_error",
        operation=operation,
        index=index,
        error_class=type(exc).__name__,
    )
    return SearchIndexError(
        "Search index operation failed",
        details={"operation": operation, "index": index, "error_class": type(exc).__name__},
    )
