"""BM25 query builders: DSL dict shapes (pure, offline)."""

from __future__ import annotations

from app.search.queries import bm25_chunk_query


def test_query_shape_without_tag():
    body = bm25_chunk_query("zorblat notes", size=25)

    assert body == {
        "size": 25,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": "zorblat notes",
                            "fields": ["chunk_text", "title^2"],
                        }
                    }
                ]
            }
        },
        # ES is a ranking index only; PG hydrates content.
        "source": False,
    }


def test_query_with_tag_adds_keyword_filter_without_touching_must():
    body = bm25_chunk_query("notes", size=50, tag="kotlin")

    boolean = body["query"]["bool"]
    assert boolean["filter"] == [{"term": {"tags": "kotlin"}}]
    # The filter clause must not perturb BM25 scoring.
    assert boolean["must"] == bm25_chunk_query("notes", size=50)["query"]["bool"]["must"]


def test_title_field_is_boosted_over_chunk_text():
    fields = bm25_chunk_query("x", size=1)["query"]["bool"]["must"][0]["multi_match"]["fields"]

    assert fields == ["chunk_text", "title^2"]


def test_size_is_passed_through_untouched():
    assert bm25_chunk_query("x", size=1)["size"] == 1
    assert bm25_chunk_query("x", size=50)["size"] == 50
