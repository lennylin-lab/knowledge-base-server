# Implement: ARQ Task Queue

Ordered checklist. Gates after each group; single commit at the end.

## Q1 — Core split + worker

- [ ] `uv add arq`; verify installed API names (ctx try key, retry
      delay control, run_worker entry) — note deviations
- [ ] `rag/indexer.py`: split `run_indexing` into raising core +
      swallowing wrapper (BackgroundTasks semantics unchanged; existing
      indexer tests stay green untouched)
- [ ] `rag/worker.py`: transient classification table,
      `index_document(ctx, doc_id)` retry logic, `WorkerSettings`
      (on_startup logging / on_shutdown closes), poison-message guard
- [ ] `tests/test_arq_worker.py` (offline, fake ctx + pipeline doubles)
- [ ] Validation: ruff + format + mypy; targeted pytest

## Q2 — Enqueuer switch + CLI + compose + env

- [ ] `core/config.py`: REDIS_URL / INDEX_JOB_MAX_TRIES /
      INDEX_JOB_RETRY_MIN_DELAY_S + `.env.example` block
- [ ] `api/deps.py`: mode selection at construction, shared lazy ARQ
      pool, lifespan close, enqueue-failure degradation
- [ ] `app/cli.py`: `worker` subcommand
- [ ] `docker-compose.yml`: redis service + healthcheck
- [ ] `tests/test_arq_enqueuer.py` (mode regression + stubbed pool +
      degradation), `test_cli.py` wiring smoke
- [ ] Validation: full suite offline (no Redis anywhere); gates

## Q3 — Live smoke

- [ ] `live_redis` marker (pyproject, deselected); `test_arq_live.py`
      with redis probe skip: real enqueue → worker drain → doc done
- [ ] Manual: compose redis up, KB_REDIS_URL set, uvicorn + worker
      running, document write → indexed via worker
- [ ] Validation: full gates

## Review gates

- trellis-check dispatch after Q3: five spec files, PRD acceptance
  sweep, dual-mode regression, retry semantics, gates re-run.

## Rollback

Single revert; REDIS_URL empty restores BackgroundTasks-only. Redis
ephemeral; compose service removable; no schema changes.
