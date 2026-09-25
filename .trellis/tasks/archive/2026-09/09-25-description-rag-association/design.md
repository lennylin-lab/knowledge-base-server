# Design: description as a first-class retrieval, agent, and association signal

## Scope

Layers touched (layering matrix: `router → service → agent → (rag | mcp |
repository)`):

- `services/document.py` — parse-layer length cap (no schema change).
- `search/es.py`, `search/queries.py` — mapping + BM25 body shape.
- `rag/indexer.py` — embedding inputs (chunk + document level), ES write.
- `rag/retriever.py`, `repositories/document_chunk.py`, `services/search.py`,
  `schemas/search.py`, `agents/qa.py`, `schemas/session.py` —
  `document_description` through the read models.
- `repositories/document.py`, `models/document.py` + one Alembic migration —
  `documents.description_embedding` column, index, and the association read.
- `services/agents.py`, `agents/summarize.py`, `agents/association.py`,
  `agents/prompts/*.md` — agent surfaces.
- README runbook.

Association gains **no ES dependency** (user decision: document-level
embedding leg, PG-only). No new Settings keys: the description boost is a
module constant like `_TITLE_BOOST`; the 500-char cap is a module constant in
`services/document.py`.

## 1. Parse layer: length cap (R1)

`services/document.py`:

```python
DESCRIPTION_MAX_CHARS = 500

# in _parse_front_matter, after the strip():
if len(description) > DESCRIPTION_MAX_CHARS:
    raise ValidationError(
        "Invalid front matter: 'description' exceeds "
        f"{DESCRIPTION_MAX_CHARS} characters",
        details={"field": "description", "max_chars": DESCRIPTION_MAX_CHARS},
    )
```

- Rejection (not truncation): matches the established front-matter validation
  style (tags type check raises the same way) and never mutates user data
  silently.
- Char count on the stripped value (`str` length, not UTF-8 bytes): the cap
  exists to bound prompt/index bloat, and prompt/index consumers count chars.
- Save-time only: rows stored before this change keep long descriptions until
  their next save. The `update_document` content-hash guard needs no change —
  description is a pure function of content, exactly like tags, so the hash
  already covers it.

## 2. ES mapping + BM25 (R2)

### Mapping (`search/es.py::_CHUNK_MAPPINGS`)

```python
"description": {"type": "text", "analyzer": "ik_max_word", "search_analyzer": "ik_smart"},
```

Same analyzer pair as `title`/`heading_path` (index-time recall, search-time
precision); declared only in `_CHUNK_MAPPINGS`, per the single-declaration-site
convention. `replace_document_chunks` gains a required `description: str`
parameter and writes it into every chunk doc's `_source` (document-level field
repeated per chunk — the index is chunk-shaped by design). The
`ReplaceChunksFn` protocol in `rag/indexer.py` grows the same parameter.

### BM25 body (`search/queries.py`)

`description` joins the **identity group**:

```python
_DESCRIPTION_BOOST = 1.5   # calibrated; hypothesis, see Calibration below
_IDENTITY_FIELDS = [f"title^{_TITLE_BOOST}", f"heading_path^{_HEADING_BOOST}",
                    f"description^{_DESCRIPTION_BOOST}"]
```

Why the identity group (not its own should-group): description is
IK-tokenized like title/heading_path, so the existing group-level
`minimum_should_match` coverage stays well-defined; the group is
`best_fields` (max within), so a description that repeats the title text
cannot double-count the title the way an additive group would. The one
textual-overlap caveat (heading_path contains title) does not hold for
description, but max-semantics still makes overlap harmless.

Boost hypothesis: **1.5** — below title (2.0, the strongest document-level
identity signal), equal to heading_path. Title outranks description because a
title match is a deliberate naming signal; a description match is a paraphrase
of content. The value must be confirmed (or adjusted) by the live-ES probes in
§7 before the PR merges; `tests/test_es_queries.py` pins whatever wins.

## 3. Embedding inputs (R3)

### Chunk input (`rag/indexer.py::embedding_input`)

Signature grows a keyword argument, default-empty so the corpus/tests that
don't care stay valid:

```python
def embedding_input(title: str, chunk: Chunk, *, description: str = "") -> str:
```

Golden-string shape (non-empty description; empty collapses exactly like the
breadcrumb branch):

```
{title}
{description}
{heading_path}

{chunk.text}
```

Description sits between title and breadcrumb: identity first, then blurb,
then section context. `process_document` passes the stored
`document.description`.

### Document-level description embedding

- `models/document.py`: new nullable column on `Document`:

  ```python
  description_embedding = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
  ```

  plus an HNSW cosine index in `__table_args__`
  (`ix_documents_description_embedding_hnsw`, `vector_cosine_ops`) — same
  shape as `document_chunks.embedding` (convention: `EMBEDDING_DIM` from
  `models/document_chunk.py`).
- Alembic migration `0013_documents_description_embedding` (follows 0012's
  style): `add_column` + `CREATE INDEX ... USING hnsw (... vector_cosine_ops)`
  in upgrade; reverse in downgrade. NULLable — every existing row reads
  "no description vector yet".
- Pipeline (`process_document_raising`): one embed batch computes everything —

  ```python
  texts = ([description] if description else []) + [embedding_input(title, c, description=description) for c in chunks]
  vectors = await provider.embed_texts(texts)
  # vectors[0] -> documents.description_embedding (None when description empty)
  ```

  Replace semantics on every re-index: empty description **clears** the column
  (a description edited away must not leave a stale vector). The document row
  write rides the same session as the chunk staging (commit before the ES
  stage, same transaction discipline as today).
- Embedded text is the **description alone**, no title prefix. Both sides of
  the association query embed symmetrically (source blurb ↔ candidate blurbs);
  adding a title prefix would weight the title twice whenever a blurb repeats
  its title, and blurbs referencing their own title is the common case. A
  one-word generic blurb ("笔记") is weak signal for either choice — accepted.

### Reindex consequences

Both stores change shape, so the existing migration runbook applies unchanged:
drop ES index, reset `index_status` to `pending`, `python -m app.cli reindex`.
The sweep re-embeds chunks (new input) and descriptions (new column) in the
same pass. README's migrations list gains the entry.

## 4. Read models & API (R4)

Data flow: `Document.description` (PG) → `ChunkRow.document_description`
(via `_LIVE_CHUNK_SELECT`, a projection like `document_title`) →
`RetrievedChunk.document_description` (default `""`) → `SearchHit` /
`SourcesEvent` items → session `SourceRef`.

- `repositories/document_chunk.py`: `ChunkRow` gains
  `document_description: str = ""`; `_LIVE_CHUNK_SELECT` adds
  `Document.description.label("document_description")`; `_as_chunk_row` maps
  it.
- `rag/retriever.py`: `RetrievedChunk` gains `document_description: str = ""`;
  `SearchOutcome.to_json` / `from_json` include it (cache round-trip test
  updated); the hydration constructor passes it through.
- `schemas/search.py::SearchHit`: `document_description: str = ""` — old
  cached JSON (missing key) still validates, old stored session sources
  project fine.
- `services/search.py` and `agents/qa.py::to_search_hit`: map the field.
- `schemas/session.py::SourceRef`: `document_description: str = ""` (history
  replay shows the blurb; the tolerant `_project_sources` validator already
  skips nothing here — the default covers old rows).

## 5. Agent surfaces (R5)

### QA / Writing context blocks (`agents/qa.py::format_context_blocks`)

```
[1] {title} (chunk {i}; tags: {tags})
Summary: {description}          <- only when description non-empty
{content}
```

`SearchHit.document_description` default keeps every existing call site and
test byte-identical when the field is absent/empty. `qa.md` grounding
contract (§2) and `writing.md` (§4) gain one sentence: a block's optional
`Summary:` line is the document's own front-matter blurb — usable for
orientation and citation decisions, still subordinate to the block content.

### Summarize (`agents/summarize.py`)

- `SummarizeDeps` gains `description: str = ""`; `SummarizeService` passes
  `description=document.description`.
- `render_document_prompt` / `render_reduce_prompt` render
  `Description: {description}` under the Tags line, only when non-empty.
- `summarize.md` §3 ("Stand alone") gains a clause: the description line is
  author-provided context, not part of the content to summarize.

### Association (`agents/association.py`)

- `AssociationDeps` gains `description: str = ""` (source blurb);
  `AssociationCandidate` gains `description: str = ""`.
- Prompt: source header gains `Description: ...` when non-empty; each
  candidate line gains `; description: {description}` when non-empty.
- `association.md` §1/§3 gain: the description signal (`similar description`)
  means vector-similar blurbs, and candidate descriptions may be compared to
  the source's directly; a vocabulary-only overlap in descriptions is not
  sufficient (mirrors the existing signal caution).

## 6. Association third leg (R6)

### Repository (`repositories/document.py`)

```python
class DescriptionNeighborRow(NamedTuple):
    document_id: UUID
    title: str
    tags: list[str]
    description: str
    distance: float

async def find_by_description_similarity(
    self, embedding: Sequence[float], *, tenant_id: UUID,
    exclude_id: UUID, limit: int,
) -> Sequence[DescriptionNeighborRow]:
```

Live documents, `description_embedding IS NOT NULL`, tenant filter, exclude
source, `ORDER BY distance, id` (deterministic tie-break, same convention as
the vector leg), `LIMIT :limit`. HNSW serves the cosine order. Empty-blur
candidates cannot match (NULL), which is the desired no-op semantics.

### Service (`services/agents.py::AssociationService._gather`)

```python
description_rows = (
    await documents.find_by_description_similarity(
        document.description_embedding,
        tenant_id=tenant_id, exclude_id=doc_id, limit=CANDIDATE_LIMIT,
    )
    if document.description_embedding is not None else []
)
```

- Source embedding is already on the loaded `Document` row — no embed call at
  query time, fully deterministic given the stored vectors.
- No ES client, no new service dependency, no new failure mode: the leg is
  pure PG. If the pipeline has not yet re-embedded a document (NULL column),
  the leg simply finds nothing — the same "leg unavailable" reading as an
  unindexed vector leg.
- `_merge_candidates(vector_rows, description_rows, tag_rows)` — deterministic
  union order: vector-ranked first, then description-ranked, then tag-only.
  A document hit by multiple legs accumulates signals (`"; "`-joined), one
  candidate per document. New signal helper:

  ```python
  def _description_signal(distance: float) -> str:
      return f"similar description (cosine distance {distance:.4f})"
  ```

  Mirror of `_vector_signal`, distinguishable in prompts and API responses.
- Candidates' `description` values flow into `AssociationCandidate` from the
  description leg rows; documents that arrived via the other two legs only
  need their description for the prompt — hydrate it in `_gather` with a
  single follow-up read for ids still missing (bounded by `3 * CANDIDATE_LIMIT`;
  one new small repository read `get_descriptions(ids, tenant_id)` or fold
  into the tag-leg projection). Decision: fold a `description` column into
  `find_by_tag_overlap`'s projection and give `find_neighbor_documents` the
  same (both already join `Document`), so `_merge_candidates` always has the
  field and no extra query exists.

## 7. Calibration (R8)

Live-ES (`es`-marked) probes on a disposable synthetic corpus (C1-style,
`replace_document_chunks` writes descriptions), one index per run:

1. **Description-only recall**: term present only in a document's
   description, absent from title/breadcrumb/body → the document's chunk is
   recalled (≥1 hit) and outranks unrelated docs.
2. **No over-boost on duplication**: doc A's description repeats its title
   verbatim; a title-term query must still rank A's title evidence ≤
   description evidence (max-group), and a body-only relevant doc must not be
   displaced below A on body-term queries — pins that 1.5 doesn't drown
   prose. If it does, lower to 1.25 and re-record.
3. **Empty-description parity**: the corpus keeps empty-description docs;
   their rank behavior on existing probe-style queries is unchanged (no
   synthetic-blank-line artifacts — covered offline by golden strings too).

Record in this file (§ Calibration Record, filled during execution): final
identity-field list, chosen boost, probe matrix summary, and the reason.
`tests/test_es_queries.py` then pins the final body dict.

## Tradeoffs & rejected alternatives

- **Reject vs truncate at 500 chars** → reject (user decision): no silent
  data mutation; consistent with tags validation. Cost: legacy long rows
  surface only on next save.
- **Association leg**: ES description match rejected (adds ES dependency +
  failure surface to a PG-only service); pg_trgm rejected (new extension,
  lexical-only, weak for CJK short text); **document-level embedding chosen**
  (user decision) at the cost of a schema migration, one extra embed per
  re-index, and re-run of the sweep — justified by semantic recall quality on
  short blurbs and a trivially deterministic PG query.
- **Title prefix in description embedding** rejected: symmetric both-side
  embedding with the blurb alone; avoids double-weighting when the blurb
  repeats the title.
- **Description in the BM25 identity group** vs a separate should-group:
  group chosen — coverage stays well-defined (IK), max-semantics bounds
  duplication inflation, and the body shape change stays minimal.
- **`_gather` description hydration**: folded into existing leg projections
  (tag overlap + neighbor rows) instead of a third query — one read set, no
  N+1, and `first_chunk_content` stays untouched.

## Compatibility & rollback

- Wire changes are additive with defaults (`document_description = ""`):
  old clients, old cached outcomes, old stored session sources all keep
  working.
- Feature has no flag; rollback = revert commit + reindex (mapping/embedding
  shapes revert with the code). The `documents.description_embedding` column
  is inert when unpopulated — the down migration drops it cleanly.
- Failure containment: the description leg cannot error independently (no
  I/O of its own beyond the PG query shared with `_gather`'s existing
  session); a NULL embedding degrades to the two-leg behavior by construction.

## Calibration Record

*(filled during execution — see §7)*

- Identity fields: `title^2`, `heading_path^1.5`, `description^1.5`
- Final description boost: **1.5** (the hypothesis held; no ladder walk
  needed)
- Probe matrix (live ES, compose node, disposable synthetic corpora per the
  C1 pattern — `tests/test_es_relevance.py`, all three green at 1.5):

  | Probe | Corpus | Assertion | Result |
  |---|---|---|---|
  | Description-only recall | doc with term `bluefin` ONLY in `description`, plus an unrelated doc | query `bluefin` recalls the described doc, unrelated docs absent | PASS |
  | No over-boost on duplication | doc A: title `delta` + description `delta` (verbatim repeat, 1-token fields); doc B: `delta` in body+code only — identical per-field stats | query `delta` ranks B (body 1.0x + code 1.5x = 2.5x) ABOVE A (max(title^2, description^1.5) = 2.0x) — max-group holds; an additive description or boost ≥ 2.5 would flip it | PASS |
  | Empty-description parity | two structurally identical docs, one `description: ""`, one `description: "papaya"` (non-matching) | query on the shared body term recalls both with equal scores (±1e-6) | PASS |

- Reason: 1.5 keeps the blurb below title (a title match is a deliberate
  naming signal; a blurb match is an author paraphrase), equal to the
  heading breadcrumb, and the max-group best_fields semantics bound
  title-duplication inflation (probe 2). Lowering to 1.25/1.0 was not
  warranted: no probe showed the blurb drowning prose evidence.
- Pins: `tests/test_es_queries.py` pins the final body dict
  (`description^1.5` in the identity fields list) and the module constant
  (`_DESCRIPTION_BOOST`); `tests/test_es_store.py` pins the mapping field.
- Environment note: probes were run after applying the runbook migration to
  the local dev stack (drop index → migration 0013 → reset `pending` →
  `python -m app.cli reindex`, 16 documents re-embedded, 0 failures), so the
  dev-corpus regressions (D1/D2/G3) were re-verified against the new
  mapping + identity fields too.
