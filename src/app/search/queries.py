"""BM25 query builders for the chunk index — dict construction, no I/O.

Bodies are shaped for the elasticsearch-py 8.x keyword arguments (hence
`source`, the SDK spelling of the API's `_source`), so `search.es` can pass
them straight through: `client.search(index=..., **body)`.
"""

from __future__ import annotations

from typing import Any

# A chunk whose *document title* matches the query outranks a body-only match.
_TITLE_BOOST = 2
# The code subfield (no stopwords, identifier-aware) adds recall for terms IK
# drops or never splits; the heading breadcrumb carries section context. The
# breadcrumb and the title overlap textually (see the identity group below), so
# both live in one group; the code subfield is an independent group whose score
# SUMS with the others — a mild boost lets identifier evidence matter without
# drowning prose.
_CODE_BOOST = 1.5
_HEADING_BOOST = 1.5

# Term-coverage gate default, mirroring `Settings.SEARCH_BM25_MIN_COVERAGE`
# (a drift-guard unit test keeps the two in sync). The retriever passes the
# configured value explicitly (constructor-injected from Settings via
# `api/deps.py`); this default covers direct construction (tests, tooling).
DEFAULT_BM25_MIN_COVERAGE = "70%"

# Fields stay analyzer-free: every query inherits each field's analyzers from
# the mapping (`search/es.py::_CHUNK_MAPPINGS` is the single declaration site).
_IDENTITY_FIELDS = [f"title^{_TITLE_BOOST}", f"heading_path^{_HEADING_BOOST}"]


def bm25_chunk_query(
    q: str,
    *,
    size: int,
    tag: str | None = None,
    min_score: float = 0.0,
    min_coverage: str = DEFAULT_BM25_MIN_COVERAGE,
) -> dict[str, Any]:
    """Build the ES request for one BM25 leg.

    Additive cross-field evidence in three `bool.should` groups (a
    single `multi_match` best_fields takes the max across ALL fields and
    discards the rest — a chunk matching both the Chinese topic and the code
    identifier scored no higher than one matching only the strongest single
    field):

    - document identity — `title` and `heading_path` as one `best_fields`
      group (max within the group): `heading_path` is the markdown ancestor
      breadcrumb and textually CONTAINS the title (each document's H1 is its
      title), so additive scoring would count a title match two or three
      times;
    - prose body — `chunk_text` (IK-analyzed);
    - identifiers / code keywords — `chunk_text.code` (no stopword list,
      identifier-aware).

    Independent groups sum, so evidence combines across them; the outer
    `minimum_should_match: 1` keeps the bool a plain OR (the tag filter must
    not silently require `should` matches).

    `min_coverage` applies an ES `minimum_should_match` to the `chunk_text`
    leaf and to the title/heading identity group — both IK-tokenized, so a
    percentage of the query's terms is well-defined there. On the identity
    group it stops a single ubiquitous function word (a lone "的" title hit)
    from satisfying the outer `minimum_should_match: 1` and activating the
    whole BM25 leg above genuine prose evidence; ES rounds the percentage
    down per field, so single-term identifier queries (`redis`) are
    unaffected — the lone term is always required. On `chunk_text.code` a
    percentage would mean something different (that analyzer keeps the
    English stopwords IK drops), so that group stays uncovered. This term-
    coverage gate is the scale-free noise guard: BM25 `_score` is query-
    dependent, so no absolute floor separates weak-but-real hits from noise.
    Defaults to `DEFAULT_BM25_MIN_COVERAGE`; an empty string omits the key
    entirely (gate disabled, identity group included).

    An optional `tags` keyword filter rides as a `filter` clause: applied
    without affecting BM25 scoring. Bounded `size`, source retrieval
    disabled — ES is a ranking index only, PG hydrates content (see
    `rag/retriever.py`). A positive `min_score` prunes sub-threshold hits
    ES-side (the retriever re-checks the same floor Python-side so gate
    behavior never depends on ES scoring quirks); `0.0` (the default) omits
    it entirely.
    """
    prose_match: dict[str, Any] = {"query": q}
    if min_coverage:
        prose_match["minimum_should_match"] = min_coverage
    identity_match: dict[str, Any] = {
        "query": q,
        "fields": _IDENTITY_FIELDS,
        "type": "best_fields",
    }
    if min_coverage:
        identity_match["minimum_should_match"] = min_coverage
    boolean: dict[str, Any] = {
        "should": [
            {"multi_match": identity_match},
            {"match": {"chunk_text": prose_match}},
            {"match": {"chunk_text.code": {"query": q, "boost": _CODE_BOOST}}},
        ],
        "minimum_should_match": 1,
    }
    if tag is not None:
        boolean["filter"] = [{"term": {"tags": tag}}]
    body: dict[str, Any] = {
        "size": size,
        "query": {"bool": boolean},
        "source": False,
    }
    if min_score > 0:
        body["min_score"] = min_score
    return body
