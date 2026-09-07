# Code-Aware Markdown Chunking + Code-Friendly ES Index

> Task `09-06-code-aware-chunking`. Corpus reality: the knowledge base holds
> **programming documentation in Markdown**, mixed Chinese prose and fenced
> code blocks. Both the chunker and the ES analyzer were built for prose and
> break on that corpus.

---

## Problem

### P0-A: fenced code blocks are torn apart and silently mutated

`rag/chunker.py::_is_atx_heading` is a pure line-level test with no fence
state. Line comments in Python / Shell / YAML / Ruby / TOML / Dockerfile
(`# something`) match `^#{1,6} ` exactly, so every such comment is treated as
a section boundary.

Measured on a 4.5 KB document with **one** real heading and one Python fence
containing 30 `# 步骤 N` comments:

| chunk | fence markers | content |
|---|---|---|
| 0 | 1 (opening only) | heading + code head, **fence never closed** |
| 1–3 | 0 | bare code fragments, **no fence, no language, no heading** |
| 4 | 1 (closing only) | code tail + orphan closing fence |

Two distinct defects:

1. **Structure loss** — code fragments reach ES and the embedding model with
   no fence, no language tag, and a code comment masquerading as an `#`
   heading on the first line.
2. **Content mutation** — even when the whole document fits in one chunk, a
   misdetected comment is split into its own section and rejoined with
   `_SEPARATOR = "\n\n"`, **inserting a blank line into the code block**.
   Reproduced with ` ```yaml ` immediately followed by `# Redis 服务配置`.

### P0-B: the IK analyzer drops programming keywords and never splits identifiers

`analysis-ik` ships an English stopword list. Measured against the live node:

```
ik_smart("how to use async_bulk with AsyncElasticsearch for a loop if not null")
  -> [how, use, async_bulk, asyncelasticsearch, loop, null]
  dropped: to, with, for, a, if, not
```

`if` / `for` / `not` / `with` / `in` / `is` / `or` / `as` are content words in
a programming corpus. Queries like "for 循环怎么写", "if not None 判断",
"with 语句" lose their most discriminative terms.

Identifiers are also indexed whole:

```
ik_max_word("ConnectionPool ... async_bulk")
  -> [..., connectionpool, ..., async_bulk, async, bulk, ...]
```

`async_bulk` splits on the underscore, but `ConnectionPool` stays one token —
searching `connection pool` can never match `ConnectionPool`, and
`Elasticsearch` can never match `AsyncElasticsearch`. camelCase is at least as
common as snake_case in this corpus.

### P1: chunks carry no heading context

Only the first chunk of a section keeps its heading. Every later chunk is
anonymous — a bare `async def step_12(...)` fragment has no signal that it
belongs to "Redis 缓存实践 > 完整示例", in either the BM25 leg or the vector
leg. `indexer.py` embeds the raw chunk text with no title/heading context.

---

## Goals

1. Fenced code blocks survive chunking: never split on `#` inside a fence,
   never mutate fence content, and when an oversized fence must be split,
   every piece stays valid Markdown (own opening fence + language, own close).
2. Programming keywords and identifier sub-words are searchable in the BM25
   leg, without losing IK's Chinese segmentation.
3. Every chunk carries its heading breadcrumb into both retrieval legs.

## Non-Goals

- No reranker (separate task; would let the gate/rescue heuristics retire).
- No embedding dimension change (2560 vs 1536 needs a migration + backfill for
  marginal gain).
- No Qwen3 query-instruct prefix (independent, needs no reindex, own task).
- No `markdown-it-py` AST migration: a fence state machine is the smaller
  edit surface and keeps `chunk_markdown` pure and dependency-free.
- No chunk overlap.

## Constraints

- `chunk_markdown(body) -> list[str]` stays available with today's semantics:
  `services/agents.py` uses it for summarize map-reduce and must not change.
- Existing `tests/test_chunker.py` behavior (heading travels with section,
  greedy packing, target/max_size, front-matter stripping) must stay green.
- Analyzer declarations stay in `search/es.py::_CHUNK_MAPPINGS` only;
  `queries.py` stays analyzer-free (`search-guidelines.md` convention).
- Retrieval must keep returning the **unmodified** chunk text as `content`;
  breadcrumbs are retrieval signal, not user-visible payload.
- No PG schema change: enriched text is embedded, plain text is stored.

## Acceptance Criteria

- [ ] A `#`-comment inside a ` ``` ` fence never opens a section; a document
      whose only real heading is `## X` yields chunks split on nothing else.
- [ ] Chunking is byte-preserving for in-fence content: no inserted blank
      line after ` ```yaml ` followed directly by `# comment`.
- [ ] An oversized fence splits into pieces that each open and close their own
      fence carrying the original info string.
- [ ] `~~~` fences and 4+ backtick fences are honored; a closing marker
      shorter than its opening does not close the block.
- [ ] ES mapping exposes a code subfield analyzed with no stopword list and
      camelCase/snake_case splitting; `connection pool` matches
      `ConnectionPool`, and `if` / `for` / `not` survive analysis.
- [ ] IK stays the analyzer for the primary `chunk_text` field — Chinese
      segmentation is unchanged.
- [ ] Each ES chunk doc carries `heading_path`; the indexer embeds
      title + breadcrumb + text while PG stores text alone.
- [ ] `uv run ruff check . && uv run ruff format --check .`, `uv run mypy src`,
      `uv run pytest` all green.
- [ ] Migration runbook documented: drop index → reset `index_status` to
      `pending` → `python -m app.cli reindex`.
