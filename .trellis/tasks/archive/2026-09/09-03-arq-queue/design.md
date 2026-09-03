# Design: ARQ Task Queue

## Dependency

`uv add arq` (pulls redis-py asyncio). Version: current stable; verify
API names against the installed version (ctx keys, retry hooks) before
coding — flag deviations.

## Mode selection (the seam already exists)

```python
# api/deps.py — get_document_service today:
#   enqueuer=lambda doc_id: background_tasks.add_task(run_indexing, doc_id)
def make_index_enqueuer(background_tasks: BackgroundTasks) -> ReindexEnqueuer:
    if not settings.REDIS_URL:
        return lambda doc_id: background_tasks.add_task(run_indexing, doc_id)
    return _ArqEnqueuer(settings.REDIS_URL, background_tasks)
```

- `_ArqEnqueuer` holds ONE shared `arq.create_pool` (built lazily at
  first use, process-lifetime like `get_shared_es_client` — never a pool
  per request; app lifespan closes it).
- Enqueue failure: catch, warn `index_enqueue_failed` (document_id,
  error_class), leave `pending`. **The write path never raises over a
  queue problem** — mirrors the search-degradation philosophy: the
  CRUD contract is intact, CLI sweep is the safety net.
- Why keep `background_tasks` in the ARQ branch at all: it doesn't —
  the ARQ branch only uses the pool. `BackgroundTasks` remains solely
  the in-process fallback's transport.

## Worker (`rag/worker.py`)

```python
async def index_document(ctx: dict, doc_id: str) -> None:
    # transient classification decides raise-for-retry vs settle-failed
    try:
        outcome = await run_indexing_raw(uuid.UUID(doc_id))
    except _TransientIndexError as exc:      # see classification below
        if ctx.get("job_try", 1) < max_tries:
            log index_job_retry (warn)
            raise                              # ARQ retries w/ backoff
        await mark_failed(doc_id); log index_job_failed_final
    ...
```

- `run_indexing_raw` = today's `run_indexing` pipeline split so the
  raising behavior is selectable: refactor `rag/indexer.py` so the core
  `process_document` raises typed errors (it already raises internally —
  the current `run_indexing` swallows at the boundary; extract the core
  and keep `run_indexing` as the BackgroundTasks-mode swallowing
  wrapper, unchanged semantics).
- **Transient classification**: `LLMRateLimitedError`,
  `LLMProviderError` (5xx/connect/timeout class — 4xx-ish config
  errors won't fix themselves but one retry is cheap; classify 401/403
  as PERMANENT — check the openai exception shape), `SearchIndexError`,
  `ConnectionError`/`OSError`/asyncio timeout. Everything else =
  permanent (settle `failed` immediately, no retry burn).
- `WorkerSettings`: `functions = [index_document]`,
  `redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)`,
  `max_tries = settings.INDEX_JOB_MAX_TRIES`, exponential backoff via
  arq's retry mechanism (verify: arq retries raising jobs with
  exponential delay starting at `base`? If arq's delay curve isn't
  configurable per-job, expose `_defer_by` at enqueue — adapt and
  note), `on_startup` → `configure_logging`, `on_shutdown` → close
  shared clients.
- The worker task opens its own sessions/clients per run (existing
  pattern) — worker process never imports FastAPI/app factory.
- ctx key for the try number: verify installed arq (`job_try`) —
  adapt if named differently.
- `doc_id` crosses the queue as **str** (JSON serialization); parse to
  UUID inside the task; invalid id ⇒ log + skip (poison-message guard).

## CLI (`app/cli.py`)

`reindex` subcommand exists; add `worker`:

```python
sub.add_parser("worker", help="Run the ARQ indexing worker")
# → from arq import run_worker? or arq.worker.run_worker(WorkerSettings)
```

Programmatic start (verify arq's entry API: `arq.worker.run_worker`);
Ctrl-C graceful stop is arq's own.

## Compose + env

```yaml
redis:
  image: redis:7-alpine
  ports: ["6379:6379"]
  healthcheck: redis-cli ping
```

`.env.example`: `KB_REDIS_URL=` (empty default + comment: set to
`redis://localhost:6379` for the ARQ worker mode), retry knobs.

## Logging

| Event | Level | Fields |
|---|---|---|
| `index_enqueued` | info | document_id, queue mode |
| `index_enqueue_failed` | warning | document_id, error_class |
| `index_job_started` | info | document_id, job_try |
| `index_job_retry` | warning | document_id, job_try, error_class |
| `index_job_finished` | info | document_id, job_try, outcome, latency_ms |

## Tests

| Suite | World | Cases |
|---|---|---|
| `test_arq_worker.py` | offline, fake ctx + fake pipeline deps | transient ⇒ raise when job_try < max; final-attempt ⇒ failed, no raise; permanent error ⇒ failed immediately, no retry; missing doc ⇒ skip; success ⇒ done; str→UUID parse + poison guard |
| `test_arq_enqueuer.py` | offline | empty REDIS_URL ⇒ BackgroundTasks path unchanged (hard regression: no pool constructed); configured ⇒ stubbed pool receives enqueue_job("index_document", str(doc_id)); enqueue failure ⇒ warn + write 201 + pending |
| `test_cli.py` extend | offline | `worker` wiring smoke (settings → WorkerSettings fields; no Redis connection) |
| `test_arq_live.py` | `live_redis` marker (deselected by default) | real compose redis: enqueue → run worker once (`arq.worker.run_worker` with check_abort or MainProcess drain) → doc done; skip when redis unreachable (probe, mirror PG/ES pattern) |

- Default suite: zero Redis imports executed against network; the pool
  stub is a plain object recording calls.
- Transient-classification unit table (openai.RateLimitError → retry;
  401 APIStatusError → permanent; SearchIndexError → retry; KeyError →
  permanent …).

## Layering

`rag/worker.py` imports `rag/indexer` + `core` (allowed). Enqueuer
adapter lives in `api/deps.py` (the sanctioned composition bridge — the
only place that may know both BackgroundTasks and arq pools).
`arq` import in `rag/worker.py` is infrastructure-infra — allowed as
llm/-style provider import (add a line to directory-structure spec).

## Tradeoffs / Rejected

- **Redis hard dependency** (rejected, user decision): dual-mode keeps
  zero-config dev; the enqueuer seam makes the switch one construction.
- **RQ/Celery/dramatiq** (rejected): ARQ is asyncio-native, tiny, and
  already the spec's reserved choice.
- **Retry in the pipeline instead of the queue** (rejected): queue-level
  retry survives worker crashes between attempts; pipeline-level retry
  only covers in-process transient errors.
- **Job deduplication keying** (rejected v1): ARQ's `_job_id` dedupe
  (e.g. `index:{doc_id}`) would collapse legitimate re-enqueues of an
  edited doc; accept duplicate runs — `process_document` is idempotent
  by design (replace semantics).
- **Moving chat/agents to the queue** (rejected): out of scope.

## Rollback

Single commit. Revert ⇒ BackgroundTasks-only (set REDIS_URL empty in
deployment). Redis data is ephemeral job bookkeeping; compose service
removable. No schema changes.
