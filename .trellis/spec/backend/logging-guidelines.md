# Logging Guidelines

> Structured logging, log levels, what to log.

---

## Overview

- **Library**: structlog, configured in `core/logging.py`, called from
  `create_app()` before routes are added.
- **Style**: key-value structured events; one log call = one event.
  No f-string message interpolation into free text.
- Output is JSON in production (`LOG_FORMAT=json`), pretty console in dev
  (`LOG_FORMAT=console`, set via `Settings`).

## Setup

```python
# core/logging.py (canonical shape)
import logging

import structlog


def configure_logging(log_level: str, log_format: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer()
            if log_format == "console"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(log_level)
        ),
    )
```

## Usage

Get a module-scoped logger; bind context once per unit of work:

```python
import structlog

logger = structlog.get_logger(__name__)
```

- **Request context**: middleware binds `request_id` (uuid4 per request) and
  `path` into `structlog.contextvars` — every log line inside the request
  carries them automatically. `X-Request-ID` is echoed in responses.
- **Service context**: at the start of a service call, bind entity ids:
  `logger = logger.bind(document_id=str(doc_id))`.
- Event names: `snake_case` verb phrases — `"document_created"`,
  `"document_deleted"`, `"chunk_embedded"`.

```python
logger.info("document_created", document_id=str(doc.id), title=doc.title)
logger.warning("app_error", code=exc.code, path=request.url.path)
logger.exception("embedding_failed", document_id=str(doc_id))  # inside except
```

## AI Stack Logging

- **LLM calls** — one `info` event per completed call:
  `model`, `provider` (host only), `input_tokens`, `output_tokens`,
  `latency_ms`, `agent` name. Prompt/completion **content is never logged at
  `info`** (user knowledge may be sensitive); `debug` may log truncated
  previews with an explicit setting.
- **Embedding calls** (`llm/embeddings.py`) — `embeddings_completed` with
  `model`, `text_count`, `total_tokens`, `latency_ms`. The text payloads
  themselves are never logged (same sensitivity as prompts). "Provider
  alias from Settings" is not yet actionable — no alias field exists in
  `Settings`; until one is added, log `model` only, never `base_url`.
- **Agent runs** — bind `run_id` (uuid4) at run start via `contextvars`,
  same mechanism as `request_id`; emit `agent_run_started`
  (agent, question length) and `agent_run_finished` (tool calls count,
  outcome). Chat requests already carry `request_id`; `run_id` links a
  request to its possibly-multiple agent runs.
- **Retrieval** — `search_executed` (service level, one per search) with
  `q_length` (**never the query text** — queries may contain sensitive
  phrasing), `limit`, `tag`, `mode` (`hybrid`/`bm25`), `hit_count`,
  `es_hits`, `vector_hits`, `latency_ms`. Vector-leg problems are
  warnings, not errors: `vector_search_disabled` (no API key, once per
  process at wiring time) and `vector_search_degraded` (mid-search
  provider failure — `error_class` only, search continues BM25-only).
- **MCP tools** — `mcp_tool_called` (tool, server, duration) and
  `mcp_tool_failed` (tool, error code). Never log full tool payloads at
  `info`; sizes are enough.
- **Token usage** is a metric, not a log line — but the per-call events above
  make aggregation possible.

## Log Levels

| Level | Use for | Examples |
|-------|---------|----------|
| `debug` | Developer diagnostics, disabled in prod by default | query parameters, cache hits |
| `info` | Normal lifecycle events worth an audit trail | entity created/deleted, job started/finished |
| `warning` | Recoverable or client-caused failures | 4xx handled errors, retries scheduled |
| `error` | Operation failed; needs attention | 5xx, exception escaped a service, DB unavailable |
| `exception` | Same as error + traceback | always inside `except` blocks |

Rules:

- `exception()` (with traceback) inside except blocks; never log the
  traceback via `str(exc)` into `message`.
- No secrets in logs: passwords, tokens, API keys, full request bodies.
  Redact or log ids instead. **API keys and `base_url` query params of LLM
  providers are secrets** — log the provider alias from Settings, not the URL.
- **Full document contents and full prompts/completions are not logged**
  at `info`/`warning` — knowledge-base content is user data. Lengths, ids,
  and truncated previews (≤200 chars, `debug` only) are the ceiling.
- Don't log-and-reraise the same error at multiple layers — log once at the
  boundary that handles it (usually the exception handler or job wrapper).
- Metrics/counters don't belong in logs; keep them separate.
