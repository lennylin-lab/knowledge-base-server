"""Chat orchestration: run_id binding, agent streaming, SSE event mapping.

Single-turn and stateless: one question in, one grounded answer streamed out.
Failures after the first event become a terminal `error` event — nothing may
escape `ask` once streaming has started (error-handling spec, streaming rule).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from uuid import uuid4

import openai
import structlog
from pydantic_ai.models import Model

from app.agents.qa import ChatDeps, SourceCollector, build_qa_agent
from app.core.exceptions import (
    AppError,
    LLMProviderError,
    LLMRateLimitedError,
)
from app.rag.retriever import Retriever
from app.schemas.chat import (
    AnswerDeltaEvent,
    ChatStreamEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SearchMode,
    SourcesEvent,
)
from app.schemas.search import SearchHit

logger = structlog.get_logger(__name__)


class ChatService:
    """Streams one knowledge-grounded answer per question."""

    def __init__(self, retriever: Retriever, model: Model, *, mode: SearchMode) -> None:
        self._retriever = retriever
        self._mode = mode
        self._model = model
        self._agent = build_qa_agent(model)

    async def ask(self, question: str, *, limit: int = 8) -> AsyncIterator[ChatStreamEvent]:
        """Run one QA turn, yielding typed stream events in contract order.

        `run_id` is bound into structlog contextvars for the whole generator,
        so every log line of the run (including retrieval legs inside tool
        calls) carries it. The question text itself is never logged.
        """
        run_id = uuid4().hex
        structlog.contextvars.bind_contextvars(run_id=run_id)
        started = time.perf_counter()

        # The service owns the flush hook: the agent appends, the service
        # drains. Keeps `agents/` free of any streaming knowledge.
        pending: list[list[SearchHit]] = []
        collector = SourceCollector(on_append=pending.append)
        deps = ChatDeps(retriever=self._retriever, limit=limit, collector=collector)

        logger.info("agent_run_started", agent="qa", question_length=len(question))
        yield RunStartedEvent(run_id=run_id, mode=self._mode)

        usage_input_tokens: int | None = None
        usage_output_tokens: int | None = None
        try:
            async with self._agent.run_stream(question, deps=deps) as result:
                async for delta in result.stream_text(delta=True, debounce_by=None):
                    # Tool calls (and their sources) can land between parts;
                    # drain before the part so sources always precede the text
                    # they ground.
                    for batch in _drain(pending):
                        yield SourcesEvent(items=batch)
                    if delta:
                        yield AnswerDeltaEvent(text=delta)
                usage = result.usage
                usage_input_tokens = usage.input_tokens or None
                usage_output_tokens = usage.output_tokens or None
            for batch in _drain(pending):
                yield SourcesEvent(items=batch)
        except Exception as exc:
            failure = _as_app_error(exc)
            logger.exception(
                "agent_run_failed",
                agent="qa",
                outcome=failure.code,
                error_class=type(exc).__name__,
                tool_calls=collector.tool_calls,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            yield ErrorEvent(code=failure.code, message=failure.message)
            return

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "agent_run_finished",
            agent="qa",
            model=self._model.model_name,
            outcome="success",
            tool_calls=collector.tool_calls,
            latency_ms=latency_ms,
            input_tokens=usage_input_tokens,
            output_tokens=usage_output_tokens,
        )
        yield DoneEvent(
            run_id=run_id,
            outcome="success",
            tool_calls=collector.tool_calls,
            latency_ms=latency_ms,
        )


def _drain(pending: list[list[SearchHit]]) -> list[list[SearchHit]]:
    """Take all pending retrieval batches, leaving the buffer empty."""
    batches = list(pending)
    pending.clear()
    return batches


def _as_app_error(exc: Exception) -> AppError:
    """Map a streaming failure onto the error taxonomy for the error event.

    Provider SDK exceptions surface as-is from the agent run (error-handling
    spec) and are wrapped here; `AppError` subclasses (e.g. `SearchIndexError`
    raised inside the retrieval tool) already carry the right code/message.
    Unknown exceptions get the generic internal error — details go to logs,
    never to the stream.
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitedError("LLM provider rate limit exceeded")
    if isinstance(exc, openai.APIError):
        return LLMProviderError("LLM provider request failed")
    return AppError("Internal server error")
