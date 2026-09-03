# ARQ Task Queue for the Indexing Pipeline

## Goal

Upgrade the indexing trigger from FastAPI BackgroundTasks (lost on
restart, tied to the web process) to an ARQ + Redis task queue with a
dedicated worker process — the "ARQ+Redis later" step the specs reserved.

User decisions: **dual-mode adaptive** (Redis URL configured ⇒ ARQ;
empty ⇒ today's BackgroundTasks, local dev stays zero-dependency) and
**automatic retries** (transient failures retry at the queue level with
backoff; permanent exhaustion still lands in the existing
`index_status=failed` + CLI sweep semantics).

## Requirements

1. **Settings**: `REDIS_URL: str = ""` (env `KB_REDIS_URL`; empty =
   BackgroundTasks mode, the default) + `INDEX_JOB_MAX_TRIES: int = 3`
   and `INDEX_JOB_RETRY_MIN_DELAY_S: int = 5` (retry policy;
   `.env.example` entries per the completeness rule). Compose gains a
   `redis` service (redis:7-alpine, port 6379, healthcheck) for dev/ops
   use — no service depends on it.
2. **Enqueuer adapter switch** (`api/deps.py`): the existing enqueuer
   seam picks the mode once at construction — `REDIS_URL` empty ⇒
   exactly today's `BackgroundTasks.add_task(run_indexing, doc_id)`;
   configured ⇒ an ARQ-backed enqueuer that enqueues
   `"index_document"` with the doc id. Redis unavailability at enqueue
   time degrades LOUDLY but does not fail the write: warn
   `index_enqueue_failed`, document stays `pending` (CLI sweep covers
   it) — the CRUD response must never 5xx over a queue hiccup.
3. **Worker** (`rag/worker.py`): `WorkerSettings` (functions,
   redis_settings from Settings, `max_tries`, retry backoff via
   `retry_delay`, `on_startup`/`on_shutdown` configuring logging and
   closing shared clients); task function `index_document(ctx, doc_id)`
   — retry-aware: transient errors (provider/ES/connection classes)
   RAISE so ARQ retries with backoff; on the final attempt (or
   non-transient error) the pipeline marks `index_status=failed` and
   the task completes without raising. Missing/soft-deleted doc ⇒ clean
   skip (no retry). The "background entry never crashes the caller"
   contract is preserved — ARQ is now the boundary that catches.
4. **CLI**: `python -m app.cli worker` runs the ARQ worker
   programmatically (worker settings from Settings).
5. **Reindex CLI unchanged**: `python -m app.cli reindex` stays the
   sweep for `pending`/`failed` (drains both enqueue-mode leftovers).
6. **Logging**: `index_job_started`/`index_job_finished` (worker scope:
   document_id, try number, latency, outcome) and
   `index_job_retry` (warning: document_id, try, error_class,
   next_delay); enqueue events `index_enqueued`/`index_enqueue_failed`
   (api scope). No content.
7. **Tests**: worker function with fake ctx (transient ⇒ raises for
   retry; final-attempt ⇒ marks failed, no raise; missing doc ⇒ skip;
   success ⇒ done) — reuse the fake-embedding pipeline doubles;
   enqueuer mode selection (empty/configured; enqueue-failure
   degradation); `reindex` regression untouched; offline default suite
   needs NO Redis (fakes only); optional live smoke under a new
   `live_redis` marker (real redis via compose, one enqueue → drain →
   done round trip).

## Out of Scope

- Queueing other job types (only indexing moves)
- Scheduled/cron jobs, job priority, dead-letter UI
- Job result persistence beyond ARQ's own bookkeeping
- Redis HA/clustering; worker supervision (systemd/compose restart
  policy is deployment's job — document in compose comment)
- Moving the MCP manager or agents onto the worker

## Acceptance Criteria

- [ ] No `KB_REDIS_URL`: behavior byte-identical to today (BackgroundTasks
      path; hard regression test asserts the same enqueuer semantics;
      no Redis client ever constructed)
- [ ] With Redis URL: document create/update enqueues an ARQ job
      (tested with a stubbed pool; live marker does the real round
      trip: write → drain worker → `index_status=done`)
- [ ] Transient provider failure ⇒ task raises on tries < max ⇒ ARQ
      retries; on final attempt marks `failed` without raising; missing
      doc skips without retry (fake-ctx tests)
- [ ] Enqueue failure (Redis down at request time): write still 201s,
      `index_enqueue_failed` warning, doc remains `pending`, CLI
      reindex completes it
- [ ] `python -m app.cli worker` boots the worker (smoke: settings
      load, function registered — no Redis needed to assert wiring)
- [ ] Compose `redis` service healthy; `.env.example` documents
      KB_REDIS_URL (empty default) + retry knobs
- [ ] Offline (no Redis, compose PG/ES up): full suite green, zero
      Redis connections; live_redis smoke deselected by default
- [ ] Gates green: ruff check / ruff format --check / mypy src / pytest
