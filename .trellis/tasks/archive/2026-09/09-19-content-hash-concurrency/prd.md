# PRD: Expose content_hash + PATCH expected_content_hash optimistic concurrency

Source: GitHub issue #2 (backend support for knowledge-base-flutter#5 — local editor draft/restore flow).

## Requirements

1. `GET /documents/{id}` detail response adds `content_hash: str | null`.
   - Detail only (`DocumentReadDetail`); list `DocumentRead` unchanged.
   - `null` = pre-backfill row (unknown hash); frontend skips validation.
2. `PATCH /documents/{id}` accepts optional `expected_content_hash: str` on `DocumentUpdate`.
   - Provided and matches stored `content_hash` → proceed as today (200).
   - Provided and mismatches stored hash → `ConflictError` (409 `conflict`) with details; document must not be modified.
   - Provided but stored hash is NULL (pre-backfill) → no 409; save proceeds normally.
   - Omitted → behavior identical to today (last-write-wins).
   - Check happens before any mutation, mirroring the `expected_base_document_version` precedent in `src/app/services/operation.py`.

## Acceptance criteria

- [ ] Detail response contains `content_hash` matching the stored hash; pre-backfill rows return `null`.
- [ ] PATCH with matching `expected_content_hash` → 200, behavior unchanged.
- [ ] PATCH with mismatched `expected_content_hash` → 409 `conflict` + details, document unmodified (no reindex enqueue, no commit).
- [ ] PATCH without the field → unchanged behavior; all existing tests stay green.
- [ ] Non-null `expected_content_hash` against NULL stored hash → saves normally.

## Non-goals

- Server-side draft entity; editor autosave; frontend changes (flutter repo).
