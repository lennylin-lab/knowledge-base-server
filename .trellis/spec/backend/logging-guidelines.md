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
  Redact or log ids instead.
- Don't log-and-reraise the same error at multiple layers — log once at the
  boundary that handles it (usually the exception handler or job wrapper).
- Metrics/counters don't belong in logs; keep them separate.
