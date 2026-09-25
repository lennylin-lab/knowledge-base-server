# Front matter description joins retrieval, agents, and association recall

Source issue: remote GitHub issue #4 ("Feature: front matter description 参与检索、Agent 与 Association 召回").

## Problem

`description` is parsed from front matter and stored in
`documents.description`, returned by the document API — but it is invisible to
everything downstream: not indexed in Elasticsearch, not part of the BM25
query, absent from chunk embedding inputs, never shown to the QA / Writing /
Summarize agents, and not a recall signal for association. Title and tags have
clear jobs (title → ES + embedding prefix; tags → filtering, agent metadata,
tag-overlap recall); `description` is semantically a short blurb and should act
as a document-level retrieval and association signal, not just a UI field.

## Goal

1. **Hybrid retrieval**: `description` participates as a document-level field
   (like `title`) in ES indexing and the BM25 leg — it is never written into
   the stripped `chunk_text`.
2. **Agents**: retrieval hits, QA/Writing context blocks, and the Summarize
   prompt expose a non-empty `description` to the model (symmetric with
   `document_title` / `document_tags`).
3. **Association**: a third deterministic recall leg based on **document-level
   description embeddings** joins vector-nearest and tag-overlap; the LLM still
   only curates given candidates.
4. **Verifiable retrieval parameters**: BM25 field boost / coverage placement
   are calibrated against a fixed corpus with automated tests / probe queries —
   no untested defaults.
5. **Length constraint**: front-matter `description` gets a documented maximum
   enforced in the parse layer.

## Decisions (user-confirmed 2026-09-25)

- **Over-long description: reject the write (422).** `ValidationError` on
  document create/update, same pattern as the `tags` type check. No silent
  truncation. Cap: **500 characters** (measured after strip, Python `str`
  length).
- **Association description leg: document-level description embedding**
  (not ES description matching, not pg_trgm). New nullable
  `documents.description_embedding` vector column populated by the indexing
  pipeline; association query is pgvector cosine over that column.

## Requirements

### R1 — Parsing & storage (length cap)

- `_parse_front_matter` rejects a `description` longer than 500 characters
  with a `ValidationError` (`details.field = "description"`); boundary 500 is
  accepted. Absent / empty / whitespace-only behavior is unchanged (`""`).
- Non-string `description` keeps its existing validation failure.
- The cap is enforced at save time only: pre-existing stored rows with longer
  descriptions are untouched until their next save (documented behavior).

### R2 — ES indexing & BM25

- ES mapping gains a `description` text field with the same IK analyzers as
  `title` (`ik_max_word` / `ik_smart`); mapping remains the only analyzer
  declaration site.
- `replace_document_chunks` writes the document-level `description` into every
  chunk document (retrieval signal only; never into `chunk_text`).
- The BM25 identity group (`best_fields` + coverage) expands to include
  `description` with a boost **lower than title's** (`title^2`); the exact
  boost value is calibrated via live-ES probes and recorded (calibration
  record in this task's design.md + PR).
- Term-coverage (`minimum_should_match`) semantics for the identity group are
  unchanged (description is IK-tokenized like title/heading_path).

### R3 — Embedding inputs (chunk + document level)

- Chunk embedding input gains a `description` prefix line between title and
  heading breadcrumb, only when description is non-empty (empty collapses —
  golden strings must not gain blank lines).
- PG still stores plain chunk text; nothing user-visible changes shape.
- New: document-level description embedding stored in
  `documents.description_embedding` (`vector(1536)`, nullable, HNSW cosine
  index), computed by the indexing pipeline in the same embed batch as the
  chunks. Empty description → column set to NULL (replace semantics on every
  re-index). The embedded text is the description alone (symmetric source and
  candidate side; no title prefix — see design.md tradeoff).

### R4 — Read models & API surface

- `RetrievedChunk` / `SearchHit` / chat·writing `sources` events gain
  `document_description` (default `""`), backward compatible (old cached
  outcomes and stored session sources read fine).
- Session `SourceRef` gains `document_description` (default `""`) for history
  replay.
- The search result cache payload round-trips the new field.

### R5 — Agents & prompts

- QA / Writing: `format_context_blocks` renders a summary line under the
  block header when description is non-empty; omitted otherwise. `qa.md` /
  `writing.md` mention the summary line in the grounding contract.
- Summarize: `SummarizeDeps` carries `description`; both document and reduce
  prompts render a `Description:` line when non-empty; `summarize.md` notes it
  as context.
- Association: source header and each candidate carry the description line
  when non-empty; `association.md` explains how to weigh the description leg's
  signal alongside description text.
- Rewrite / conversation_summary are **out of scope**.

### R6 — Association recall (third leg)

- New repository query: live documents whose non-null
  `description_embedding` is nearest (cosine) to the source's, tenant-scoped,
  excluding the source, bounded to the existing `CANDIDATE_LIMIT`.
- Source description empty (no embedding) → the leg is a **no-op**; the
  existing two legs are unaffected. Candidates without an embedding are
  naturally invisible to this leg.
- `_merge_candidates` merges three legs deterministically (vector, then
  description by distance, then tag-only), one entry per document, combined
  signals joined; the description leg's signal is human-readable
  (`similar description (cosine distance X.XXXX)`).
- Association stays PG-only (no ES dependency added).

### R7 — Reindex & runbook

- Mapping change + new embedding column require a full reindex: drop index,
  reset `index_status` to `pending`, `python -m app.cli reindex` — README
  runbook documents this migration (the migrations list gains this entry).

### R8 — Calibration & drift guards

- Live-ES probe cases (fixed corpus): a query term that appears **only** in
  `description` still recalls the document; description duplicating the title
  does not over-boost (rank sanity); empty-description documents behave as
  before.
- The final identity-field list, description boost, and its rationale are
  recorded (calibration record) and pinned by `tests/test_es_queries.py`.
- Embedding golden-string tests cover both chunk inputs (with/without
  description) and the description-only input.

## Non-goals

- No `?description=` HTTP filter.
- Description is never concatenated into stored chunk text (PG `content`).
- Rewrite / conversation_summary prompts unchanged.
- No `minimum_should_match` on any code subfield; no new Settings keys for the
  boost (module constants like `_TITLE_BOOST`).

## Acceptance Criteria

- [ ] AC1: `description` > 500 chars → 422 on create and update with a
      field-scoped validation error; 500 exactly passes; absent/empty/whitespace
      behavior unchanged (unit + API tests).
- [ ] AC2: ES mapping declares `description` (IK analyzers) and every indexed
      chunk doc carries it; live mapping test updated.
- [ ] AC3: BM25 identity group includes the calibrated description boost;
      query-body pin tests and the no-analyzer-key walk stay green.
- [ ] AC4: Chunk embedding input prefixes non-empty description (golden
      strings); description-only document embedding is produced in the same
      embed batch and stored (or cleared) on `documents.description_embedding`.
- [ ] AC5: Search / chat / writing sources and session `SourceRef` expose
      `document_description`; cache round-trip test covers it.
- [ ] AC6: QA / Writing context blocks and Summarize prompts show the summary
      line iff description is non-empty (agent tests pin both branches).
- [ ] AC7: Association merges three legs deterministically; description-leg
      signal strings are readable; empty source description → no-op leg with
      unchanged two-leg behavior (service tests, real DB, scripted vectors).
- [ ] AC8: Live-ES calibration probes recorded: description-only-term recall,
      no over-boost on title duplication, empty-description unchanged.
- [ ] AC9: README runbook documents the reindex migration; full `ruff` +
      `mypy` + offline pytest suite green.
