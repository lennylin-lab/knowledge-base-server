"""Elasticsearch access: client construction, index lifecycle, chunk writes.

Every ES query lives in this package (see directory-structure.md); failures
surface as `SearchIndexError` with the index and error class only — never
document content — in the details.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Any, NamedTuple
from uuid import UUID

import structlog
from elasticsearch import ApiError, AsyncElasticsearch, TransportError
from elasticsearch.helpers import BulkIndexError, async_bulk

from app.core.config import Settings, get_settings
from app.core.exceptions import SearchIndexError
from app.rag.chunker import Chunk

logger = structlog.get_logger(__name__)

# Everything the ES client (or its bulk helper) can raise; there is no common
# base class shared by the API, transport, and bulk-helper error families.
_ES_ERRORS = (ApiError, TransportError, BulkIndexError)

# Index settings: the `code` analyzers for the programming-documentation
# corpus (see search-guidelines.md). IK drops English stopwords
# (if/for/not/with are content words in code) and never splits identifiers, so
# `chunk_text` carries a `code` subfield analyzed with a whitespace tokenizer
# (no stopword list) plus a word_delimiter_graph filter: camelCase/snake_case
# split into parts, originals and catenations preserved (`connection pool`
# matches ConnectionPool; utf8/int64 stay intact).
#
# Index and search analyzers are deliberately SPLIT here. The index-time
# `preserve_original` + `catenate_words` expansion stacks alternative tokens at
# one position; used at search time that stacked graph compiles into an
# adjacency-constrained PHRASE query, so an identifier query like
# `ConnectionPool` silently matches nothing. The search-side `code_search`
# analyzer only splits (flat token stream, plain OR). The same expansion also
# emits the same string twice for a pure camelCase identifier (original ==
# catenation after lowercase), so `remove_duplicates` — AFTER `lowercase`, so
# casing variants collapse first — drops the duplicate index token and its
# BM25 tf inflation. `flatten_graph` stays index-time only: it exists because
# index-time analyzers cannot emit token graphs, and the flat search stream
# needs none.
_CHUNK_SETTINGS: dict[str, dict[str, dict[str, dict[str, object]]]] = {
    "analysis": {
        "filter": {
            "code_delimiter": {
                "type": "word_delimiter_graph",
                "preserve_original": True,
                "split_on_case_change": True,
                "catenate_words": True,
                "split_on_numerics": False,
                "stem_english_possessive": False,
            },
            "code_delimiter_search": {
                "type": "word_delimiter_graph",
                "preserve_original": False,
                "split_on_case_change": True,
                "catenate_words": False,
                "split_on_numerics": False,
                "stem_english_possessive": False,
            },
        },
        "analyzer": {
            "code": {
                "tokenizer": "whitespace",
                "filter": ["code_delimiter", "flatten_graph", "lowercase", "remove_duplicates"],
            },
            "code_search": {
                "tokenizer": "whitespace",
                "filter": ["code_delimiter_search", "lowercase"],
            },
        },
    }
}

# Explicit mapping beats dynamic: schema drift becomes visible, keyword fields
# stay filterable, text fields stay analyzed. Text fields use the IK analyzers
# (analysis-ik plugin baked into the compose image): CJK needs word-level
# segmentation — ik_max_word at index time (fine-grained, maximizes recall),
# ik_smart at search time (coarse-grained, avoids query-term explosion).
# `chunk_text.code` is the programming-term escape hatch; `heading_path`
# carries each chunk's heading breadcrumb into the BM25 leg.
_CHUNK_MAPPINGS: dict[str, dict[str, dict[str, object]]] = {
    "properties": {
        "document_id": {"type": "keyword"},
        # Tenant filter field (Stage 5): every search carries a term filter on
        # it, so a stale index can never surface another tenant's chunks.
        "tenant_id": {"type": "keyword"},
        "title": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "tags": {"type": "keyword"},
        "heading_path": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "chunk_text": {
            "type": "text",
            "analyzer": "ik_max_word",
            "search_analyzer": "ik_smart",
            "fields": {
                "code": {"type": "text", "analyzer": "code", "search_analyzer": "code_search"}
            },
        },
        "chunk_index": {"type": "integer"},
    }
}


def get_es_client(settings: Settings) -> AsyncElasticsearch:
    """Client from Settings — the only construction site for ES clients."""
    return AsyncElasticsearch(hosts=[settings.ELASTICSEARCH_URL], request_timeout=30)


@lru_cache(maxsize=1)
def get_shared_es_client() -> AsyncElasticsearch:
    """Process-lifetime client for request paths: one transport pool per app
    lifetime, never one per request. (The indexing pipeline deliberately
    keeps its own short-lived client — it runs detached from requests.)"""
    return get_es_client(get_settings())


async def ensure_index(client: AsyncElasticsearch, index: str) -> None:
    """Create the chunk index with explicit settings + mapping if missing."""
    try:
        exists = await client.indices.exists(index=index)
        if not bool(exists):
            await client.indices.create(
                index=index, mappings=_CHUNK_MAPPINGS, settings=_CHUNK_SETTINGS
            )
    except _ES_ERRORS as exc:
        raise _wrap("ensure_index", index, exc) from exc


async def replace_document_chunks(
    client: AsyncElasticsearch,
    *,
    index: str,
    tenant_id: str,
    document_id: UUID,
    title: str,
    tags: Sequence[str],
    chunks: Sequence[Chunk],
) -> None:
    """Idempotently replace one document's ES docs: delete-by-document, bulk-index.

    Doc ids are deterministic (`{document_id}:{chunk_index}`), so shrinking a
    document never leaves orphan docs behind. Both steps `refresh` on
    completion: ES is near-real-time, and a re-index arriving inside the
    refresh interval must still see (and delete) the previous version's docs —
    without this, replace would leak orphans. Each doc stores the chunk's
    `heading_path` breadcrumb alongside the text (retrieval signal only —
    content is hydrated from PG) and the owning `tenant_id` (the search-side
    term filter's field).
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
                            "tenant_id": tenant_id,
                            "title": title,
                            "tags": list(tags),
                            "chunk_index": chunk_index,
                            "chunk_text": chunk.text,
                            "heading_path": chunk.heading_path,
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


class EsChunkHit(NamedTuple):
    """One ranked ES hit: the chunk key (parsed from the doc id) + BM25 score."""

    document_id: UUID
    chunk_index: int
    score: float


async def search_chunks(
    client: AsyncElasticsearch, *, index: str, body: dict[str, Any]
) -> list[EsChunkHit]:
    """Run one prepared chunk query (see `queries.py`); hits as keys + scores.

    Source retrieval stays disabled by the caller: content lives in PG, and
    the deterministic doc id `{document_id}:{chunk_index}` carries the key.
    """
    try:
        response = await client.search(index=index, **body)
    except _ES_ERRORS as exc:
        raise _wrap("search_chunks", index, exc) from exc
    hits: list[EsChunkHit] = []
    for hit in response["hits"]["hits"]:
        document_id, _, chunk_index = hit["_id"].rpartition(":")
        hits.append(EsChunkHit(UUID(document_id), int(chunk_index), float(hit["_score"] or 0.0)))
    return hits
