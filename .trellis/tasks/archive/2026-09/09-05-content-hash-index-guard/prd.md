# Content hash guards duplicate reindexing

## Goal

Saving a document whose content did not actually change must not trigger the
indexing pipeline. Today every `update_document` call resets
`index_status` to PENDING and enqueues a reindex (chunk → embedding API call →
pgvector + ES full replace) even for byte-identical content. Documents gain a
persisted content hash; saves compare the hash and only reindex on a real
change.

## Background (current behavior)

- `DocumentService.update_document` (src/app/services/document.py) always sets
  `index_status = PENDING` and calls the enqueuer on every write.
- The only dedupe is the generation guard in `rag/indexer.py`
  (`expected_updated_at`), which skips *stale* jobs, not *duplicate-content*
  jobs.
- The ES index also stores `title` and `tags`; `title` can change via a
  title-only update (`DocumentUpdate.title` without content).

## Requirements

1. `documents` gains a `content_hash` column: SHA-256 hex digest of the raw
   content (front matter included, exactly what is stored in `content`).
2. `create_document` computes and stores the hash; a new document is always
   indexed (unchanged behavior).
3. `update_document` recomputes the hash when content is supplied and decides:
   - **Skip** (no status reset, no enqueue) only when ALL of:
     - supplied content hashes equal to the stored `content_hash`
       (a stored NULL hash counts as changed — safety net),
     - resolved title is unchanged,
     - current `index_status == DONE` (a `failed`/`pending` document is
       re-saved to retry, so it must re-enqueue).
   - Otherwise: store the new hash (when content supplied), reset status to
     PENDING, enqueue — i.e. today's behavior.
4. Title-only updates (no content in payload): the title feeds the search
   index, so they reindex whenever the resolved title actually changes; a
   title-only save resolving to the identical title on a DONE document takes
   the skip path (nothing index-relevant changed).
5. Existing rows are backfilled by the migration (sha256 over `content`), so
   already-indexed documents do not trigger one spurious reindex on their next
   identical-content save.
6. The pipeline (`rag/indexer.py`), the enqueuer signature
   `(doc_id, updated_at)`, and the CLI retry sweep are unchanged — the guard
   lives entirely in the service layer.
7. `content_hash` is internal state: not exposed in API response schemas.

## Constraints

- Migration follows the backend database guidelines (one revision, real
  downgrade, pgcrypto available in the `pgvector/pgvector:pg16` image for the
  backfill; extension created `IF NOT EXISTS` in the migration).
- Service layer stays framework-free; hash computation is a plain function.
- No changes to the `ReindexEnqueuer` protocol or generation-guard semantics.

## Acceptance Criteria

- [ ] New migration adds nullable `content_hash` to `documents` and backfills
      existing rows; `upgrade head → downgrade -1 → upgrade head` round-trips.
- [ ] Create stores the hash and enqueues exactly once (existing test keeps
      passing).
- [ ] Update with identical content + unchanged title on a DONE document:
      `index_status` stays DONE, updated_at may bump, enqueuer NOT called.
- [ ] Update with identical content but `index_status = FAILED` (or PENDING):
      status reset to PENDING and enqueuer called.
- [ ] Update with changed content: new hash stored, status reset, enqueuer
      called.
- [ ] Title-only update to a different title (content unchanged): status
      reset, enqueuer called; title-only update resolving to the same title on
      a DONE document skips (no enqueue, status stays DONE).
- [ ] A document whose stored hash is NULL (pre-backfill row) re-saved with
      identical content: treated as changed (re-enqueued once), hash stored.
- [ ] Existing suite (`uv run pytest`) passes; new service tests cover the
      skip/retry matrix above.

## Out of Scope

- Exposing `content_hash` via API schemas.
- Hashing in the indexer/worker or deduping jobs at queue level.
- MCP write paths (none exist today; the service is the single funnel).
