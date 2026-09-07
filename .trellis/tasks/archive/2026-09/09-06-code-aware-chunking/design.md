# Design — Code-Aware Chunking + Code-Friendly ES Index

## 1. Boundaries

| Module | Change |
|---|---|
| `rag/chunker.py` | fence state machine; `Chunk` dataclass with `heading_path`; fence re-open/close when splitting |
| `search/es.py` | `_CHUNK_SETTINGS` (analysis), `chunk_text.code` subfield, `heading_path` field; `replace_document_chunks` takes chunks with breadcrumbs |
| `search/queries.py` | `multi_match` fields gain `chunk_text.code` and `heading_path` |
| `rag/indexer.py` | embeds `title + heading_path + text`, stores `text` in PG, passes breadcrumbs to ES |
| `services/agents.py` | untouched — keeps calling `chunk_markdown` |

No PG schema change, no Alembic revision, no embedding dimension change.

## 2. Chunker contract

`chunk_markdown` keeps its signature and stays the summarize-path entry point.
The structured variant is new:

```python
@dataclass(frozen=True)
class Chunk:
    text: str            # exactly what PG stores and search returns
    heading_path: str    # "Redis 缓存实践 > 完整示例"; "" before the first heading

def chunk_markdown_structured(body, *, target=800, max_size=1600) -> list[Chunk]: ...
def chunk_markdown(body, *, target=800, max_size=1600) -> list[str]:
    return [chunk.text for chunk in chunk_markdown_structured(body, ...)]
```

`heading_path` is the **ancestor breadcrumb** built from a heading stack keyed
by ATX level, so a deeper heading replaces same-or-deeper entries. It is not
prepended to `text`: the first chunk of a section already carries its own
heading line inline (existing behavior, existing tests), and duplicating the
breadcrumb into the stored text would leak into API responses.

### Fence recognition (CommonMark subset)

- Opening: ≤3 leading spaces, then ≥3 `` ` `` or ≥3 `~`, then an info string.
  A backtick fence's info string may not contain a backtick (CommonMark).
- Closing: ≤3 leading spaces, ≥ the opening run length of the **same**
  character, then only whitespace. A shorter run does not close.
- Unclosed fence at EOF: stays open to EOF (CommonMark), which is exactly the
  conservative choice — we would rather keep a tail together than split it on
  a comment.

Fence state gates three places that currently assume prose:

1. `_split_sections` — an ATX heading inside a fence is not a heading.
2. `_split_paragraphs` — a blank line inside a fence is not a paragraph break.
3. `_split_by_lines` — the last resort; sees fence state so it can repair.

### Splitting an oversized fence

When a piece boundary lands inside an open fence, the emitted piece gets the
closing marker appended and the next piece gets `{marker}{info}` prepended, so
every chunk is independently valid Markdown and keeps its language tag. The
repair markers are the only bytes the chunker ever adds; in-fence content is
otherwise byte-preserved.

Budget note: repair markers are added **after** a piece is packed to
`max_size`, so a repaired piece can exceed `max_size` by the marker length.
The alternative — reserving marker space up front — makes the packing loop
depend on lookahead. `max_size` is a chunk-quality target, not a hard external
limit (no tokenizer or column bound depends on it), so overshooting by a few
bytes is the cheaper tradeoff. Tests assert `max_size + margin`.

## 3. ES mapping

```python
_CHUNK_SETTINGS = {
    "analysis": {
        "filter": {
            "code_delimiter": {
                "type": "word_delimiter_graph",
                "preserve_original": True,   # ConnectionPool stays searchable whole
                "split_on_case_change": True,# -> connection, pool
                "catenate_words": True,      # -> connectionpool
                "split_on_numerics": False,  # utf8 / int64 stay intact
                "stem_english_possessive": False,
            }
        },
        "analyzer": {
            "code": {
                "tokenizer": "whitespace",   # no stopword list: if/for/not survive
                "filter": ["code_delimiter", "flatten_graph", "lowercase"],
            }
        },
    }
}

_CHUNK_MAPPINGS = {
    "properties": {
        "document_id": {"type": "keyword"},
        "title": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "tags": {"type": "keyword"},
        "heading_path": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
        "chunk_text": {
            "type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart",
            "fields": {"code": {"type": "text", "analyzer": "code"}},
        },
        "chunk_index": {"type": "integer"},
    }
}
```

`flatten_graph` is required because `word_delimiter_graph` emits a token graph
and this analyzer is used at **index** time, where graphs are not supported.
Using one analyzer for both index and search keeps the declaration single-site
(the `search-guidelines.md` convention) at the cost of no multi-term synonym
expansion — which this analyzer does not produce anyway.

`queries.py` gains the fields but no analyzer:

```python
fields=[
    "chunk_text",
    f"chunk_text.code^{_CODE_BOOST}",   # 1.5
    f"heading_path^{_HEADING_BOOST}",   # 1.5
    f"title^{_TITLE_BOOST}",            # 2
]
```

`chunk_text` and `chunk_text.code` both matching the same term double-counts
it in `best_fields`… except `best_fields` takes the **max**, not the sum, so a
term found by both legs scores once. That is the intended behavior: the code
subfield adds *recall* for terms IK drops or never splits, and only wins the
`max` when it is the field that matched.

## 4. Indexing data flow

```
chunk_markdown_structured(content)
  -> [Chunk(text, heading_path)]
        |- PG:  replace_for_document(id, [c.text], vectors)      # unchanged shape
        |- vec: embed_texts([f"{title}\n{c.heading_path}\n\n{c.text}"])
        `- ES:  replace_document_chunks(..., chunks=[Chunk...])  # text + heading_path
```

Embedding enrichment needs no migration because the vector lives in the same
row as the plain text; only the vector's *input* changes. It does, however,
shift the query-to-chunk distance distribution, so `KB_SEARCH_VECTOR_MAX_DISTANCE`
(0.45) and the rescue window must be re-probed after reindexing —
`tests/test_vector_distance_probe.py` is the existing instrument.

## 5. Rejected alternatives

- **`markdown-it-py` AST** — correct but heavier: `chunk_markdown` becomes
  dependency-bound and the greedy packer has to be rebuilt around tokens. The
  fence state machine fixes the actual defect at a fraction of the edit
  surface. Revisit if nested lists or tables start splitting badly.
- **`code_langs` keyword field** — no consumer. No query filters by language
  today; adding the field now is speculative schema.
- **Prepending the breadcrumb into `chunk.text`** — simplest to implement, but
  it leaks into `content` returned by `/search` and into the summarize path.
- **Dropping IK for `chunk_text` in favor of the code analyzer** — loses
  Chinese word segmentation, which is the majority of prose in this corpus.
- **ES `dense_vector` + native `rrf` retriever** — would collapse two stores
  into one, but the pgvector leg and the Python RRF are working and tested;
  out of scope.
