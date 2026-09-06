"""ES store: index lifecycle and idempotent document-chunk replacement.

Runs against the local Elasticsearch node (auto-skipped when unreachable);
every test owns a unique disposable index.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from elasticsearch import NotFoundError

from app.core.exceptions import SearchIndexError
from app.search.es import ensure_index, replace_document_chunks

pytestmark = pytest.mark.es


async def _count_for(es_client, index: str, document_id: str) -> int:
    await es_client.indices.refresh(index=index)
    response = await es_client.count(index=index, query={"term": {"document_id": document_id}})
    return int(response["count"])


async def test_ensure_index_creates_explicit_mapping_and_is_idempotent(es_client, es_index_name):
    await ensure_index(es_client, es_index_name)
    # Idempotent: creating over an existing index must not raise.
    await ensure_index(es_client, es_index_name)

    mapping = await es_client.indices.get_mapping(index=es_index_name)
    properties = mapping[es_index_name]["mappings"]["properties"]
    assert properties["document_id"] == {"type": "keyword"}
    assert properties["title"] == {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
    }
    assert properties["tags"] == {"type": "keyword"}
    assert properties["chunk_text"] == {
        "type": "text",
        "analyzer": "ik_max_word",
        "search_analyzer": "ik_smart",
    }
    assert properties["chunk_index"] == {"type": "integer"}


async def test_analyze_with_ik_max_word_segments_chinese_into_words(es_client):
    # Plugin contract: without analysis-ik installed this raises
    # analyzer_not_found. The standard analyzer emits ONLY single characters
    # for CJK, so a multi-character token proves word-level segmentation.
    # ik_max_word is fine-grained and also emits single characters (库 below).
    response = await es_client.indices.analyze(analyzer="ik_max_word", text="智能知识库检索")

    tokens = [entry["token"] for entry in response["tokens"]]
    assert "智能" in tokens and "知识库" in tokens
    assert any(len(token) >= 2 for token in tokens)


async def test_replace_indexes_chunks_with_deterministic_ids(es_client, es_index_name):
    await ensure_index(es_client, es_index_name)
    doc_id = uuid4()

    await replace_document_chunks(
        es_client,
        index=es_index_name,
        document_id=doc_id,
        title="Indexed Note",
        tags=["kotlin", "fp"],
        chunks=["first chunk", "second chunk"],
    )

    assert await _count_for(es_client, es_index_name, str(doc_id)) == 2
    stored = await es_client.get(index=es_index_name, id=f"{doc_id}:1")
    assert stored["_source"]["chunk_text"] == "second chunk"
    assert stored["_source"]["chunk_index"] == 1
    assert stored["_source"]["title"] == "Indexed Note"
    assert stored["_source"]["tags"] == ["kotlin", "fp"]
    assert stored["_source"]["document_id"] == str(doc_id)


async def test_replace_shrinks_without_leaving_orphans(es_client, es_index_name):
    await ensure_index(es_client, es_index_name)
    doc_id = uuid4()
    other_id = uuid4()

    await replace_document_chunks(
        es_client,
        index=es_index_name,
        document_id=doc_id,
        title="A",
        tags=[],
        chunks=["a", "b", "c"],
    )
    await replace_document_chunks(
        es_client, index=es_index_name, document_id=other_id, title="B", tags=[], chunks=["other"]
    )

    await replace_document_chunks(
        es_client, index=es_index_name, document_id=doc_id, title="A", tags=[], chunks=["only a"]
    )

    assert await _count_for(es_client, es_index_name, str(doc_id)) == 1
    with pytest.raises(NotFoundError):
        await es_client.get(index=es_index_name, id=f"{doc_id}:2")
    # Other documents are untouched by a document-scoped replace.
    assert await _count_for(es_client, es_index_name, str(other_id)) == 1


async def test_replace_with_no_chunks_clears_the_document(es_client, es_index_name):
    await ensure_index(es_client, es_index_name)
    doc_id = uuid4()

    await replace_document_chunks(
        es_client, index=es_index_name, document_id=doc_id, title="A", tags=[], chunks=["a", "b"]
    )
    await replace_document_chunks(
        es_client, index=es_index_name, document_id=doc_id, title="A", tags=[], chunks=[]
    )

    assert await _count_for(es_client, es_index_name, str(doc_id)) == 0


async def test_missing_index_failure_is_wrapped_as_search_index_error(es_client, es_index_name):
    with pytest.raises(SearchIndexError) as exc_info:
        await replace_document_chunks(
            es_client,
            index=f"{es_index_name}-never-created",
            document_id=uuid4(),
            title="A",
            tags=[],
            chunks=["x"],
        )

    assert exc_info.value.details["operation"] == "replace_document_chunks"
    assert exc_info.value.details["error_class"] == "NotFoundError"
