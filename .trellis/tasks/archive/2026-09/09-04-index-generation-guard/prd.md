# Indexing Generation Guard: Skip Stale Jobs

## Goal

Repeated saves of one document currently each trigger a FULL indexing
pass (chunk + embedding API + dual-store replace) — only the last
version's pass has value; the rest is wasted embedding cost and queue
work. Add a **generation guard**: the enqueue carries the document's
`updated_at`; a job that starts after the document changed again skips
itself with a single SELECT, so only the job matching the latest
version does real work.

Lightweight task (PRD-only). No schema change, no new endpoints.

## Requirements

1. **Enqueue carries the version**: `ReindexEnqueuer` becomes
   `Callable[[UUID, datetime], None]` (document id + the `updated_at`
   observed at commit time). `DocumentService` enqueues the pair after
   commit on create/update (rollback paths still enqueue nothing).
   Existing enqueue-capture fakes/tests updated to the new signature —
   their assertions gain the timestamp.
2. **Guard in the pipeline core**: `IndexingPipeline.process_document`
   (and the `run_indexing` / `run_indexing_raw` / ARQ task entries)
   gain `expected_updated_at: datetime | None = None`. After loading
   the document, if `expected_updated_at is not None` and differs from
   the current `updated_at`, the job logs `index_job_skipped_stale`
   (info: document_id) and returns WITHOUT touching `index_status`
   (a newer job owns the doc; if that job was lost, the pending status
   + CLI sweep is the net). Matching version or `None` ⇒ run exactly
   as today. **CLI reindex keeps `None`** (it scans current state by
   definition).
3. **ARQ integration**: the task payload carries both values
   (str(doc_id) + iso timestamp); the retry path inherits the guard —
   a retry whose document has since been edited skips instead of
   re-indexing an obsolete version (the newer edit's job owns it).
4. **Poison-safety**: an unparseable/absent timestamp in the ARQ
   payload degrades to `None` (no guard), never crashes the job.
5. **No behavior change** when the guard does not fire: single-save
   flows, BackgroundTasks mode, live round trip — all byte-identical
   semantics.

## Out of Scope

- Debounce/coalescing at enqueue time (arq has no replace semantics;
  the guard achieves the value with O(1) stale jobs)
- Per-document serialization locks (the guard narrows the concurrent
  different-version window; full mutual exclusion is not needed)
- Guarding the CLI path, queue priority, dead-lettering

## Acceptance Criteria

- [ ] Rapid consecutive updates: stale jobs skip before any chunking,
      embedding, or store writes — fake provider call count == 1 for a
      burst of saves; only the latest version's job works (tested)
- [ ] Skipped job leaves `index_status` untouched (stays pending for
      the newer job); `index_job_skipped_stale` logged with document_id
- [ ] Matching version runs fully; `None` version (CLI, legacy payload)
      runs fully — existing indexer/CLI tests stay green
- [ ] ARQ retry after a newer edit skips (tested); malformed timestamp
      payload degrades to no-guard (tested)
- [ ] Service enqueues `(id, updated_at)` after commit on
      create/update only; rollback still enqueues nothing
- [ ] Full gates green: ruff check / ruff format --check / mypy src /
      pytest; live_redis round trip still passes
