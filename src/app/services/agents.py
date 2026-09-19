"""Agent orchestration: summarize passes, association candidates, writing runs.

Summarize and association compute one result per run, never persisted, and
stream it as typed SSE events (design: agent stream) — summarize runs
chunk-aware passes — long documents map-reduce style with the production
chunker (one model pass per chunk, sequentially, then one combine pass) with
a progress event per pass; association gathers its deterministic candidates
(pgvector neighbors + tag overlap) BEFORE any model call, so the LLM only
curates what the database surfaced, and its structured output stays atomic.
Plain-coroutine wrappers drain the streams for sync callers. Writing streams
suggestions over chat's SSE event vocabulary, with `ChatService.ask`'s stream
discipline: run_id binding, sources flushed per tool call, and nothing may
escape `suggest` once the first event is yielded.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from uuid import UUID, uuid4

import openai
import structlog
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import Tool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.association import (
    AssociationCandidate,
    AssociationDeps,
    AssociationsOutput,
    build_association_agent,
    render_association_prompt,
)
from app.agents.qa import SourceCollector
from app.agents.summarize import (
    SummarizeDeps,
    build_summarize_agent,
    render_document_prompt,
    render_reduce_prompt,
)
from app.agents.writing import WritingDeps, build_writing_agent, render_writing_prompt
from app.core.cache import Cache, cache_key
from app.core.exceptions import (
    AppError,
    LLMProviderError,
    LLMRateLimitedError,
    NotFoundError,
)
from app.models.document import Document
from app.rag.chunker import chunk_markdown
from app.rag.retriever import Retriever
from app.repositories.document import DocumentRepository, TagOverlapRow
from app.repositories.document_chunk import DocumentChunkRepository, NeighborDocumentRow
from app.schemas.agent_stream import (
    AgentDoneEvent,
    AgentRunStartedEvent,
    AgentStreamEvent,
    AssociationsResultEvent,
    SummaryProgressEvent,
    SummaryResultEvent,
)
from app.schemas.agents import AssociationItem, AssociationsResult, SummaryResult
from app.schemas.chat import (
    AnswerDeltaEvent,
    ChatStreamEvent,
    DoneEvent,
    ErrorEvent,
    RunStartedEvent,
    SearchMode,
    SourcesEvent,
    StatusEvent,
)
from app.schemas.search import SearchHit
from app.services.stream_bridge import RunEventBridge

logger = structlog.get_logger(__name__)


class SummarizeService:
    """Computes (never persists) one document summary per call."""

    def __init__(
        self,
        model: Model,
        model_name: str,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cache: Cache | None = None,
        cache_ttl_seconds: int = 0,
        summary_max_tokens: int = 250,
        summary_chunk_target: int = 2400,
        summary_chunk_max_size: int = 4800,
    ) -> None:
        self._model_name = model_name
        self._session_factory = session_factory
        self._summary_max_tokens = summary_max_tokens
        self._summary_chunk_target = summary_chunk_target
        self._summary_chunk_max_size = summary_chunk_max_size
        # Optional best-effort result cache (None = today's behavior): the
        # computed summary is keyed on the document's content_hash, so an
        # edit self-invalidates — no active deletion needed. A NULL hash
        # (pre-backfill document) simply never caches.
        self._cache = cache
        self._cache_ttl_seconds = cache_ttl_seconds
        # Built once per process (the service itself is process-lifetime);
        # per-request state rides in SummarizeDeps, never on the agent.
        self._agent = build_summarize_agent(model)

    async def summarize_document(self, doc_id: UUID, *, tenant_id: UUID) -> SummaryResult:
        """Summarize one live document; missing/soft-deleted raise NotFoundError.

        Draining wrapper over `summarize_document_stream`: the stream's errors
        (which the stream itself reports as terminal `error` events only after
        its first yield) surface here as their `AppError` — the internal
        generator raises them straight through — so the sync envelope behavior
        is unchanged. The result event IS the `SummaryResult`.
        """
        async for event in self._summarize_events(doc_id, tenant_id=tenant_id):
            if isinstance(event, SummaryResultEvent):
                return event
        raise AssertionError("summarize stream ended without a result event")

    async def summarize_document_stream(
        self, doc_id: UUID, *, tenant_id: UUID
    ) -> AsyncIterator[AgentStreamEvent]:
        """Stream one summarize run as typed events (design: agent stream).

        Contract order: `run_started` → `summary_progress` per map pass plus
        the reduce pass (fixed grammar, even for single-pass summaries) →
        `summary` → `done`. A cache hit skips all progress events. The
        document load (404 gate on missing/soft-deleted) happens BEFORE the
        first yield, so pre-stream failures keep their HTTP envelopes;
        everything after that becomes a terminal `error` event — nothing
        raises out of this generator once it has yielded.
        """
        yielded = False
        try:
            async for event in self._summarize_events(doc_id, tenant_id=tenant_id):
                yielded = True
                yield event
        except AppError as failure:
            if not yielded:
                raise  # pre-stream (document load): the HTTP envelope still applies
            yield ErrorEvent(code=failure.code, message=failure.message)

    async def _summarize_events(
        self, doc_id: UUID, *, tenant_id: UUID
    ) -> AsyncIterator[AgentStreamEvent]:
        """Internal event generator; raises `AppError` on any failure."""
        started = time.perf_counter()
        # The load happens before anything else: a missing document must 404
        # without a model call, and the run log needs the id bound.
        document = await self._load_document(doc_id, tenant_id=tenant_id)
        run_id = uuid4().hex
        log = logger.bind(document_id=str(doc_id), run_id=run_id)
        yield AgentRunStartedEvent(run_id=run_id, kind="summary", document_id=doc_id)
        # The document load above is the correctness gate (404 on missing);
        # the cache only ever short-circuits the expensive LLM run. Lookup
        # happens before chunking so a hit skips all model passes.
        cached = await self._cache_get(document, started=started)
        if cached is not None:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            log.info(
                "cache_hit",
                domain="summary",
                model=self._model_name,
                latency_ms=latency_ms,
            )
            yield SummaryResultEvent(**cached.model_dump())
            yield AgentDoneEvent(run_id=run_id, outcome="success", latency_ms=latency_ms)
            return
        deps = SummarizeDeps(title=document.title, tags=list(document.tags))
        # Front-matter-only (or whitespace-only) bodies chunk to nothing; the
        # degenerate path summarizes the RAW stored content in one pass. Raw is
        # deliberate: the stripped body would be empty, and the title/tags
        # inside the YAML only duplicate the header the prompt already renders.
        chunks = chunk_markdown(
            document.content,
            target=self._summary_chunk_target,
            max_size=self._summary_chunk_max_size,
        ) or [document.content]

        log.info("agent_run_started", agent="summarize", content_length=len(document.content))

        input_tokens = 0
        output_tokens = 0
        # Fixed progress grammar: one event per map pass plus the reduce pass,
        # so the reduce pass is always pass_index == passes_total.
        passes_total = len(chunks) + 1
        map_max_tokens = max(100, self._summary_max_tokens * 2 // passes_total)
        try:
            if len(chunks) == 1:
                yield SummaryProgressEvent(
                    phase="map_pass", pass_index=1, passes_total=passes_total
                )
                summary, tokens_in, tokens_out = await self._run_pass(
                    deps,
                    render_document_prompt(deps, chunks[0], max_tokens=self._summary_max_tokens),
                    max_tokens=self._summary_max_tokens,
                )
                # Fixed grammar: the reduce phase is announced even when the
                # whole document was one pass — no separate model call behind it.
                yield SummaryProgressEvent(
                    phase="reduce_pass", pass_index=passes_total, passes_total=passes_total
                )
                runs = 1
            else:
                section_summaries: list[str] = []
                for index, chunk in enumerate(chunks, start=1):
                    yield SummaryProgressEvent(
                        phase="map_pass", pass_index=index, passes_total=passes_total
                    )
                    text, tokens_in, tokens_out = await self._run_pass(
                        deps,
                        render_document_prompt(
                            deps,
                            chunk,
                            section=(index, len(chunks)),
                            max_tokens=map_max_tokens,
                        ),
                        max_tokens=map_max_tokens,
                    )
                    section_summaries.append(text)
                    input_tokens += tokens_in
                    output_tokens += tokens_out
                yield SummaryProgressEvent(
                    phase="reduce_pass", pass_index=passes_total, passes_total=passes_total
                )
                summary, tokens_in, tokens_out = await self._run_pass(
                    deps,
                    render_reduce_prompt(
                        deps, section_summaries, max_tokens=self._summary_max_tokens
                    ),
                    max_tokens=self._summary_max_tokens,
                )
                runs = len(chunks) + 1
            input_tokens += tokens_in
            output_tokens += tokens_out
        except Exception as exc:
            failure = _as_app_error(exc)
            # Run-level audit event, not a boundary log: it closes the
            # agent_run_started trail with run-scoped context (run_id, latency,
            # error_class, traceback). Re-raising lets the stream wrapper turn
            # it into the terminal `error` event (or the sync wrapper's
            # envelope) — one audit trail, no duplicated payload.
            log.exception(
                "agent_run_failed",
                agent="summarize",
                outcome=failure.code,
                error_class=type(exc).__name__,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise failure from exc

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info(
            "agent_run_finished",
            agent="summarize",
            model=self._model_name,
            outcome="success",
            runs=runs,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        result = SummaryResult(
            document_id=doc_id,
            summary=summary,
            model=self._model_name,
            latency_ms=latency_ms,
        )
        await self._cache_put(document, result)
        yield SummaryResultEvent(**result.model_dump())
        yield AgentDoneEvent(run_id=run_id, outcome="success", latency_ms=latency_ms)

    async def _cache_get(self, document: Document, *, started: float) -> SummaryResult | None:
        """Cached summary lookup; only successful results are ever stored.
        `None` content_hash (pre-backfill) or no cache = always a miss."""
        if self._cache is None or document.content_hash is None:
            return None
        key = cache_key(
            "summary",
            document.id,
            document.content_hash,
            self._model_name,
            self._summary_max_tokens,
        )
        raw = await self._cache.get(key)
        if raw is None:
            return None
        try:
            result = SummaryResult.model_validate_json(raw)
        except ValueError:
            return None  # unusable payload (e.g. pre-bump format): recompute
        # latency_ms reflects the (cheap) cache path, not the original run.
        return result.model_copy(
            update={"latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        )

    async def _cache_put(self, document: Document, result: SummaryResult) -> None:
        if self._cache is None or document.content_hash is None:
            return
        key = cache_key(
            "summary",
            document.id,
            document.content_hash,
            self._model_name,
            self._summary_max_tokens,
        )
        await self._cache.set(
            key, result.model_dump_json().encode("utf-8"), ttl_seconds=self._cache_ttl_seconds
        )

    async def _load_document(self, doc_id: UUID, *, tenant_id: UUID) -> Document:
        """Fetch one live document in the tenant scope; soft-deleted counts as
        missing (one non-leaky 404 for both cases)."""
        async with self._session_factory() as session:
            document = await DocumentRepository(session).get_by_id(doc_id, tenant_id=tenant_id)
        if document is None:
            raise NotFoundError(f"Document {doc_id} not found")
        return document

    async def _run_pass(
        self, deps: SummarizeDeps, prompt: str, *, max_tokens: int
    ) -> tuple[str, int, int]:
        """One non-streaming model pass: (summary text, input tokens, output tokens)."""
        result = await self._agent.run(
            prompt,
            deps=deps,
            model_settings=ModelSettings(max_tokens=max_tokens),
        )
        usage = result.usage
        return result.output, usage.input_tokens or 0, usage.output_tokens or 0


def _as_app_error(exc: Exception) -> AppError:
    """Map a failed run onto the error taxonomy — same mapping as chat's.

    Chat converts the mapping into a terminal SSE event because its stream is
    already open; sync endpoints re-raise instead so the shared handler
    returns the matching HTTP envelope. Details stay in logs either way.

    The production model (OpenAIChatModel) wraps provider SDK failures in
    pydantic-ai's own types before a service ever sees them: HTTP >= 400
    becomes `ModelHTTPError`, connection/timeout becomes `ModelAPIError` —
    so the taxonomy keys off the wrapped status, and the raw-SDK branches
    below only serve paths that bypass pydantic-ai (FunctionModel tests).
    """
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, ModelHTTPError):
        if exc.status_code == 429:
            return LLMRateLimitedError("LLM provider rate limit exceeded")
        return LLMProviderError("LLM provider request failed")
    if isinstance(exc, ModelAPIError):
        return LLMProviderError("LLM provider request failed")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitedError("LLM provider rate limit exceeded")
    if isinstance(exc, openai.APIError):
        return LLMProviderError("LLM provider request failed")
    return AppError("Internal server error")


# Both candidate legs are bounded to this size (PRD: ~10 per leg) — enough
# signal for the model without an unbounded prompt.
CANDIDATE_LIMIT = 10
# Fallback excerpt bound when the source has no chunks yet: matches the
# chunker's per-chunk ceiling, so indexed and unindexed sources feed the
# prompt similarly sized excerpts.
EXCERPT_CHAR_LIMIT = 1600


class AssociationService:
    """Computes (never persists) one document's related documents per call."""

    def __init__(
        self,
        model: Model,
        model_name: str,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cache: Cache | None = None,
        cache_ttl_seconds: int = 0,
    ) -> None:
        self._model_name = model_name
        self._session_factory = session_factory
        # Optional best-effort result cache (None = today's behavior). The
        # short TTL is the staleness bound: association depends on OTHER
        # documents (tag overlap + vector neighbors), so the source's
        # content_hash cannot capture every invalidation.
        self._cache = cache
        self._cache_ttl_seconds = cache_ttl_seconds
        # Built once per process (the service itself is process-lifetime);
        # per-request state rides in AssociationDeps, never on the agent.
        self._agent = build_association_agent(model)

    async def associate_document(self, doc_id: UUID, *, tenant_id: UUID) -> AssociationsResult:
        """Curate related documents for one live document via the LLM.

        Draining wrapper over `associate_document_stream` (see the summarize
        twin for the error-surfacing rationale). The result event IS the
        `AssociationsResult`.
        """
        async for event in self._association_events(doc_id, tenant_id=tenant_id):
            if isinstance(event, AssociationsResultEvent):
                return event
        raise AssertionError("association stream ended without a result event")

    async def associate_document_stream(
        self, doc_id: UUID, *, tenant_id: UUID
    ) -> AsyncIterator[AgentStreamEvent]:
        """Stream one association run as typed events (design: agent stream).

        Contract order: `run_started` → `associations` → `done`; the no-
        candidates and cache-hit shortcuts keep the same shape (no progress
        events — the structured output is atomic). The candidate gather (404
        gate on missing/soft-deleted) happens BEFORE the first yield, so
        pre-stream failures keep their HTTP envelopes; everything after that
        becomes a terminal `error` event.
        """
        yielded = False
        try:
            async for event in self._association_events(doc_id, tenant_id=tenant_id):
                yielded = True
                yield event
        except AppError as failure:
            if not yielded:
                raise  # pre-stream (gather/document load): HTTP envelope applies
            yield ErrorEvent(code=failure.code, message=failure.message)

    async def _association_events(
        self, doc_id: UUID, *, tenant_id: UUID
    ) -> AsyncIterator[AgentStreamEvent]:
        """Internal event generator; raises `AppError` on any failure.

        Deterministic candidates are gathered before any model call: a
        missing/soft-deleted source raises NotFoundError and a source with no
        candidates at all returns an empty result — neither ever reaches the
        model. The model's selection is joined back onto the candidate
        metadata; ids it returned that were not candidates (hallucinated or
        duplicated) are dropped, so the response only ever carries
        deterministic metadata plus the LLM-written reasons. Reasons and
        content are never logged — ids and counts only.
        """
        started = time.perf_counter()
        document, candidates, excerpt = await self._gather(doc_id, tenant_id=tenant_id)
        run_id = uuid4().hex
        log = logger.bind(document_id=str(doc_id), run_id=run_id)
        yield AgentRunStartedEvent(run_id=run_id, kind="associations", document_id=doc_id)
        # The gather above is the correctness gate (404 on missing); the
        # cache only ever short-circuits the expensive LLM run.
        cached = await self._cache_get(document, started=started)
        if cached is not None:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            log.info(
                "cache_hit",
                domain="association",
                model=self._model_name,
                latency_ms=latency_ms,
            )
            yield AssociationsResultEvent(**cached.model_dump())
            yield AgentDoneEvent(run_id=run_id, outcome="success", latency_ms=latency_ms)
            return
        if not candidates:
            # No signal to curate: skip the run (and its lifecycle events)
            # rather than logging a run that never happened.
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            log.info(
                "agent_run_skipped",
                agent="association",
                reason="no_candidates",
                latency_ms=latency_ms,
            )
            yield AssociationsResultEvent(
                document_id=doc_id,
                associations=[],
                model=self._model_name,
                latency_ms=latency_ms,
            )
            yield AgentDoneEvent(run_id=run_id, outcome="success", latency_ms=latency_ms)
            return

        deps = AssociationDeps(title=document.title, tags=list(document.tags))
        prompt = render_association_prompt(deps, excerpt, candidates)
        log.info("agent_run_started", agent="association", candidate_count=len(candidates))
        try:
            result = await self._agent.run(prompt, deps=deps)
        except Exception as exc:
            failure = _as_app_error(exc)
            # Run-level audit event closing the agent_run_started trail (see
            # the summarize twin); re-raising feeds the terminal `error` event
            # or the sync wrapper's envelope.
            log.exception(
                "agent_run_failed",
                agent="association",
                outcome=failure.code,
                error_class=type(exc).__name__,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise failure from exc

        items, dropped = _join_selections(result.output, candidates)
        usage = result.usage
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info(
            "agent_run_finished",
            agent="association",
            model=self._model_name,
            outcome="success",
            latency_ms=latency_ms,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            candidate_count=len(candidates),
            selected_count=len(items),
            dropped_count=dropped,
        )
        associations_result = AssociationsResult(
            document_id=doc_id,
            associations=items,
            model=self._model_name,
            latency_ms=latency_ms,
        )
        await self._cache_put(document, associations_result)
        yield AssociationsResultEvent(**associations_result.model_dump())
        yield AgentDoneEvent(run_id=run_id, outcome="success", latency_ms=latency_ms)

    async def _cache_get(self, document: Document, *, started: float) -> AssociationsResult | None:
        """Cached association lookup; only successful non-empty results are
        stored (the no-candidates path stays a cheap always-miss). `None`
        content_hash (pre-backfill) or no cache = always a miss."""
        if self._cache is None or document.content_hash is None:
            return None
        key = cache_key("assoc", document.id, document.content_hash, self._model_name)
        raw = await self._cache.get(key)
        if raw is None:
            return None
        try:
            result = AssociationsResult.model_validate_json(raw)
        except ValueError:
            return None  # unusable payload (e.g. pre-bump format): recompute
        # latency_ms reflects the (cheap) cache path, not the original run.
        return result.model_copy(
            update={"latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        )

    async def _cache_put(self, document: Document, result: AssociationsResult) -> None:
        if self._cache is None or document.content_hash is None:
            return
        key = cache_key("assoc", document.id, document.content_hash, self._model_name)
        await self._cache.set(
            key, result.model_dump_json().encode("utf-8"), ttl_seconds=self._cache_ttl_seconds
        )

    async def _gather(
        self, doc_id: UUID, *, tenant_id: UUID
    ) -> tuple[Document, list[AssociationCandidate], str]:
        """Load the live source, both candidate legs, and a bounded excerpt.

        One session for the whole read set. The document load comes first: a
        missing or soft-deleted source must 404 before any other work. The
        vector leg needs the source's own chunks — no chunks means it returns
        nothing and the run continues on tag candidates alone.
        """
        async with self._session_factory() as session:
            documents = DocumentRepository(session)
            chunks = DocumentChunkRepository(session)
            document = await documents.get_by_id(doc_id, tenant_id=tenant_id)
            if document is None:
                raise NotFoundError(f"Document {doc_id} not found")
            vector_rows = await chunks.find_neighbor_documents(
                doc_id, tenant_id=tenant_id, limit=CANDIDATE_LIMIT
            )
            tag_rows = await documents.find_by_tag_overlap(
                document.tags, tenant_id=tenant_id, exclude_id=doc_id, limit=CANDIDATE_LIMIT
            )
            excerpt = await chunks.first_chunk_content(doc_id, tenant_id=tenant_id)
        if excerpt is None:
            excerpt = document.content[:EXCERPT_CHAR_LIMIT]
        return document, _merge_candidates(vector_rows, tag_rows), excerpt


def _vector_signal(distance: float) -> str:
    """Human-readable vector-leg signal for prompts and responses."""
    return f"similar content (cosine distance {distance:.4f})"


def _tag_signal(shared_tags: Sequence[str]) -> str:
    """Human-readable tag-leg signal for prompts and responses."""
    return f"shared tags: {', '.join(shared_tags)}"


def _merge_candidates(
    vector_rows: Sequence[NeighborDocumentRow], tag_rows: Sequence[TagOverlapRow]
) -> list[AssociationCandidate]:
    """Union both legs into one candidate list, vector-ranked first.

    A document surfaced by both legs appears once with both signals; a
    document only the tag leg found keeps its own signal. Leg order is
    deterministic (distance then id; recency then id), so the prompt order is
    too.
    """
    tag_shared = {tag_row.document_id: tag_row.shared_tags for tag_row in tag_rows}
    candidates: dict[UUID, AssociationCandidate] = {}
    for vector_row in vector_rows:
        shared = tag_shared.get(vector_row.document_id)
        signal = _vector_signal(vector_row.distance)
        if shared:
            signal = f"{signal}; {_tag_signal(shared)}"
        candidates[vector_row.document_id] = AssociationCandidate(
            document_id=vector_row.document_id,
            title=vector_row.title,
            tags=list(vector_row.tags),
            signal=signal,
        )
    for tag_row in tag_rows:
        if tag_row.document_id in candidates:
            continue
        candidates[tag_row.document_id] = AssociationCandidate(
            document_id=tag_row.document_id,
            title=tag_row.title,
            tags=list(tag_row.tags),
            signal=_tag_signal(tag_row.shared_tags),
        )
    return list(candidates.values())


def _join_selections(
    output: AssociationsOutput, candidates: Sequence[AssociationCandidate]
) -> tuple[list[AssociationItem], int]:
    """Join the LLM's picks back onto candidate metadata.

    Every returned item carries the deterministic title/tags/signal gathered
    before the run — the model contributes only the selection and the reason.
    Picks whose id was not a candidate, or repeats an already-joined one, are
    dropped; the count (never the content) is returned for logging.
    """
    by_id = {candidate.document_id: candidate for candidate in candidates}
    items: list[AssociationItem] = []
    seen: set[UUID] = set()
    dropped = 0
    for pick in output.associations:
        candidate = by_id.get(pick.document_id)
        if candidate is None or pick.document_id in seen:
            dropped += 1
            continue
        seen.add(pick.document_id)
        items.append(
            AssociationItem(
                document_id=candidate.document_id,
                title=candidate.title,
                tags=list(candidate.tags),
                reason=pick.reason,
                signal=candidate.signal,
            )
        )
    return items, dropped


def _drain(pending: list[list[SearchHit]]) -> list[list[SearchHit]]:
    """Take all pending retrieval batches, leaving the buffer empty.

    Twin of `services.chat._drain`, kept local rather than imported across
    service modules' private names: four trivial lines, and chat's module
    stays untouched.
    """
    batches = list(pending)
    pending.clear()
    return batches


class WritingService:
    """Streams one writing-suggestion run per draft (chat's event vocabulary).

    Mirrors `ChatService.ask`'s discipline: run_id bound into structlog
    contextvars for the whole generator, sources flushed as soon as each
    retrieval tool call returns, and every failure after the first event
    becoming a terminal `error` event — nothing may escape `suggest`.
    Retrieval is OPTIONAL (the model decides), so a zero-tool run simply has
    no `sources` event and reports `tool_calls=0`. Draft and instruction text
    are never logged; lengths only.
    """

    def __init__(
        self,
        retriever: Retriever,
        model: Model,
        *,
        mode: SearchMode,
        extra_tools: Sequence[Tool[WritingDeps]] = (),
    ) -> None:
        self._retriever = retriever
        self._mode = mode
        self._model = model
        # Built once per process (the service itself is process-lifetime);
        # per-request state rides in WritingDeps, never on the agent. The
        # extra_tools seam stays open for wrapped MCP tools in a later task.
        self._agent = build_writing_agent(model, extra_tools=extra_tools)

    async def suggest(
        self, draft: str, instruction: str | None = None, *, tenant_id: UUID, limit: int = 8
    ) -> AsyncIterator[ChatStreamEvent]:
        """Run one writing-assistance turn, yielding chat-contract events.

        The draft (plus optional instruction) is rendered into the user
        prompt; the model then streams its suggestion, calling
        `search_knowledge` only when it judges the knowledge base helpful.
        Retrieval is scoped to `tenant_id` — both legs filter on it.
        """
        run_id = uuid4().hex
        structlog.contextvars.bind_contextvars(run_id=run_id)
        started = time.perf_counter()

        # The service owns the flush hook: the agent appends, the service
        # drains (same hand-off as chat, so `agents/` stays stream-free).
        pending: list[list[SearchHit]] = []
        collector = SourceCollector(on_append=pending.append)
        deps = WritingDeps(
            retriever=self._retriever, limit=limit, collector=collector, tenant_id=tenant_id
        )

        logger.info(
            "agent_run_started",
            agent="writing",
            draft_length=len(draft),
            instruction_length=len(instruction) if instruction is not None else None,
        )
        yield RunStartedEvent(run_id=run_id, mode=self._mode)

        usage_input_tokens: int | None = None
        usage_output_tokens: int | None = None
        generating_sent = False
        # Same bridge + drain idiom as chat: tool lifecycle events buffered
        # by the handler, drained here around the sources batches (per tool
        # call: started → sources → finished). No rewrite step exists in
        # writing, so no rewrite/status(rewriting_query) events ever appear.
        bridge = RunEventBridge()
        try:
            async with self._agent.run_stream(
                render_writing_prompt(draft, instruction),
                deps=deps,
                event_stream_handler=bridge.on_agent_event,
            ) as result:
                async for delta in result.stream_text(delta=True, debounce_by=None):
                    for started_event in bridge.drain_started():
                        yield started_event
                    for batch in _drain(pending):
                        yield SourcesEvent(items=batch)
                    for finished_event in bridge.drain_finished():
                        yield finished_event
                    if delta:
                        if not generating_sent:
                            # Same phase signal as chat: answer streaming begins.
                            yield StatusEvent(phase="generating")
                            generating_sent = True
                        yield AnswerDeltaEvent(text=delta)
                usage = result.usage
                usage_input_tokens = usage.input_tokens or None
                usage_output_tokens = usage.output_tokens or None
            for started_event in bridge.drain_started():
                yield started_event
            for batch in _drain(pending):
                yield SourcesEvent(items=batch)
            for finished_event in bridge.drain_finished():
                yield finished_event
        except Exception as exc:
            failure = _as_app_error(exc)
            logger.exception(
                "agent_run_failed",
                agent="writing",
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
            agent="writing",
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
