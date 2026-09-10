"""BM25 query builders: DSL dict shapes (pure, offline)."""

from __future__ import annotations

from typing import Any

from app.search.queries import DEFAULT_BM25_MIN_COVERAGE, bm25_chunk_query


def _boolean(body: dict[str, Any]) -> dict[str, Any]:
    return body["query"]["bool"]


def test_query_shape_without_tag():
    # Default call: the coverage gate is ACTIVE (the drift-guard unit test in
    # test_search_gates.py pins DEFAULT_BM25_MIN_COVERAGE to Settings).
    body = bm25_chunk_query("zorblat notes", size=25)

    assert body == {
        "size": 25,
        "query": {
            "bool": {
                "should": [
                    # Group 1 — document identity, max WITHIN the group:
                    # heading_path textually contains title, so additive
                    # scoring here would double-count a title match. Threads
                    # the same term-coverage gate as the prose leaf.
                    {
                        "multi_match": {
                            "query": "zorblat notes",
                            "fields": ["title^2", "heading_path^1.5"],
                            "type": "best_fields",
                            "minimum_should_match": DEFAULT_BM25_MIN_COVERAGE,
                        }
                    },
                    # Group 2 — prose body (IK-analyzed), carrying the
                    # term-coverage gate.
                    {
                        "match": {
                            "chunk_text": {
                                "query": "zorblat notes",
                                "minimum_should_match": DEFAULT_BM25_MIN_COVERAGE,
                            }
                        }
                    },
                    # Group 3 — identifiers / code keywords (no stopword list).
                    {"match": {"chunk_text.code": {"query": "zorblat notes", "boost": 1.5}}},
                ],
                "minimum_should_match": 1,
            }
        },
        # ES is a ranking index only; PG hydrates content.
        "source": False,
    }


def test_groups_sum_across_fields_instead_of_taking_a_single_max():
    # The old single multi_match (best_fields over all four fields) discarded
    # cross-field evidence — the D1 rank inversion. The shape must keep three
    # separate should clauses so their scores add.
    should = _boolean(bm25_chunk_query("notes", size=5))["should"]

    assert [next(iter(group)) for group in should] == ["multi_match", "match", "match"]
    assert list(should[1]["match"]) == ["chunk_text"]
    assert list(should[2]["match"]) == ["chunk_text.code"]


def test_identity_group_takes_max_not_sum():
    group = _boolean(bm25_chunk_query("notes", size=5))["should"][0]

    assert group["multi_match"]["type"] == "best_fields"
    assert group["multi_match"]["fields"] == ["title^2", "heading_path^1.5"]


# --- coverage gate (SEARCH_BM25_MIN_COVERAGE) ---


def test_coverage_gate_applies_to_prose_and_identity_not_code():
    # The percentage is defined by IK tokenization (prose text, titles,
    # breadcrumbs); on chunk_text.code it would mean something different
    # (that analyzer keeps the English stopwords IK drops). The identity
    # group threads the same gate: without it a lone function-word title hit
    # would satisfy the outer minimum_should_match: 1 and activate the leg.
    should = _boolean(bm25_chunk_query("notes", size=5, min_coverage="60%"))["should"]

    assert should[0]["multi_match"]["minimum_should_match"] == "60%"
    assert should[1]["match"]["chunk_text"]["minimum_should_match"] == "60%"
    assert "minimum_should_match" not in should[2]["match"]["chunk_text.code"]


def test_coverage_sentinel_omits_key_and_keeps_outer_or():
    # "" disables the gate everywhere: the key is omitted entirely on the
    # prose leaf AND the identity group (pre-gate hit counts, the "none"
    # column of the D6 table), while the outer bool stays a plain OR across
    # the three groups.
    should = _boolean(bm25_chunk_query("notes", size=5, min_coverage=""))["should"]

    assert "minimum_should_match" not in should[0]["multi_match"]
    assert "minimum_should_match" not in should[1]["match"]["chunk_text"]
    assert _boolean(bm25_chunk_query("notes", size=5, min_coverage=""))["minimum_should_match"] == 1


def test_identity_group_carries_minimum_should_match_when_coverage_set():
    # The identity group must not be activatable by a single function-word
    # title hit inside a multi-token query — it threads the same coverage
    # value as the prose leaf (70% by default).
    identity = _boolean(bm25_chunk_query("notes", size=5, min_coverage="70%"))["should"][0]

    assert identity["multi_match"]["minimum_should_match"] == "70%"


def test_four_token_query_cannot_activate_identity_with_one_stopword_hit():
    # ES rounds percentage coverage DOWN per field: "量子力学薛定谔的猫"
    # tokenizes into 4 terms, so the identity group requires
    # floor(4 * 70%) = 2 of them in title (or in heading_path). A lone "的"
    # title hit is 1 < 2, so the group fails and cannot activate the BM25
    # leg above genuine prose evidence (the 09-10 noise hole).
    identity = _boolean(bm25_chunk_query("量子力学薛定谔的猫", size=5))["should"][0]

    assert identity["multi_match"]["minimum_should_match"] == DEFAULT_BM25_MIN_COVERAGE


def test_single_term_identifier_query_unaffected_by_identity_coverage():
    # A one-term query ("redis") has exactly one term per identity field and
    # that lone term is always required (1 of 1 satisfies any percentage),
    # so the coverage gate cannot suppress identifier searches.
    identity = _boolean(bm25_chunk_query("redis", size=5))["should"][0]

    assert identity["multi_match"]["query"] == "redis"
    assert identity["multi_match"]["minimum_should_match"] == DEFAULT_BM25_MIN_COVERAGE


def test_query_with_tag_adds_keyword_filter_without_touching_should():
    body = bm25_chunk_query("notes", size=50, tag="kotlin")

    boolean = body["query"]["bool"]
    assert boolean["filter"] == [{"term": {"tags": "kotlin"}}]
    # The filter clause must not perturb BM25 scoring.
    assert boolean["should"] == bm25_chunk_query("notes", size=50)["query"]["bool"]["should"]


def test_tag_filter_does_not_lower_outer_minimum_should_match():
    # With a filter clause present, ES defaults an unset
    # minimum_should_match to 0 — the should groups would stop OR-ing. The
    # explicit 1 must survive tag filtering.
    untagged = _boolean(bm25_chunk_query("notes", size=5))
    tagged = _boolean(bm25_chunk_query("notes", size=5, tag="kotlin"))

    assert untagged["minimum_should_match"] == 1
    assert tagged["minimum_should_match"] == 1


def test_query_body_is_analyzer_free():
    # C3 (search-guidelines.md): analyzers are declared ONLY in the mapping;
    # queries inherit each field's search_analyzer.
    body = bm25_chunk_query("notes", size=5, tag="kotlin", min_score=1.0)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            assert "analyzer" not in node
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(body)


def test_size_is_passed_through_untouched():
    assert bm25_chunk_query("x", size=1)["size"] == 1
    assert bm25_chunk_query("x", size=50)["size"] == 50


def test_min_score_enters_body_only_when_positive():
    assert "min_score" not in bm25_chunk_query("x", size=1)
    assert "min_score" not in bm25_chunk_query("x", size=1, min_score=0.0)

    body = bm25_chunk_query("x", size=1, min_score=1.0)

    assert body["min_score"] == 1.0
