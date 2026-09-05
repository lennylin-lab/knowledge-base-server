"""BM25 query builders for the chunk index — pure dict construction, no I/O.

Bodies are shaped for the elasticsearch-py 8.x keyword arguments (hence
`source`, the SDK spelling of the API's `_source`), so `search.es` can pass
them straight through: `client.search(index=..., **body)`.
"""

from __future__ import annotations

from typing import Any

# A chunk whose *document title* matches the query outranks a body-only match.
_TITLE_BOOST = 2


def bm25_chunk_query(
    q: str, *, size: int, tag: str | None = None, min_score: float = 0.0
) -> dict[str, Any]:
    """Build the ES request for one BM25 leg.

    Multi-match over chunk text with a boosted title field, an optional
    `tags` keyword filter (a `filter` clause: applied without affecting BM25
    scoring), bounded `size`, and source retrieval disabled — ES is a ranking
    index only, PG hydrates content (see `rag/retriever.py`). A positive
    `min_score` prunes sub-threshold hits ES-side (the retriever re-checks
    the same floor Python-side so gate behavior never depends on ES scoring
    quirks); `0.0` (the default) omits it entirely.
    """
    boolean: dict[str, Any] = {
        "must": [
            {
                "multi_match": {
                    "query": q,
                    "fields": ["chunk_text", f"title^{_TITLE_BOOST}"],
                }
            }
        ]
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
