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
                            "fields": [
                                "chunk_text",
                                "chunk_text.code^1.5",
                                "heading_path^1.5",
                                "title^2",
                            ],
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

    boosts = {
        field.split("^")[0]: float(field.split("^")[1]) if "^" in field else 1.0 for field in fields
    }
    assert boosts["title"] == max(boosts.values())


def test_bm25_query_targets_code_subfield_and_heading_path():
    # The code subfield adds recall for terms IK drops or never splits
    # (stopwords, identifier parts); heading_path carries the breadcrumb into
    # the BM25 leg. Both are mild boosts under the best_fields max.
    fields = bm25_chunk_query("x", size=1)["query"]["bool"]["must"][0]["multi_match"]["fields"]

    assert fields == ["chunk_text", "chunk_text.code^1.5", "heading_path^1.5", "title^2"]


def test_size_is_passed_through_untouched():
    assert bm25_chunk_query("x", size=1)["size"] == 1
    assert bm25_chunk_query("x", size=50)["size"] == 50


def test_min_score_enters_body_only_when_positive():
    assert "min_score" not in bm25_chunk_query("x", size=1)
    assert "min_score" not in bm25_chunk_query("x", size=1, min_score=0.0)

    body = bm25_chunk_query("x", size=1, min_score=1.0)

    assert body["min_score"] == 1.0
