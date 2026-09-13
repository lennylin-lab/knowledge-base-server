"""Chat orchestration: run_id binding, session persistence, agent streaming,
SSE event mapping.

Multi-turn with session persistence when a `session_factory` is wired: the
user message is persisted before the run (durable even if the run fails),
the assistant message on success (full streamed text + run_id), and the
recent conversation returns to the agent as `message_history` within a
token budget. A single oversized turn (e.g. a long pasted document) is
admitted truncated-with-marker instead of silently evicting all other
history; only the assembled history copy is bounded — persisted rows always
keep full content. Without a factory the service is exactly the original
stateless single-turn chat — no database access at all.

When a rewrite model is wired and history exists, a best-effort rewrite
turns a follow-up question into a self-contained retrieval query before the
run; the persisted message and the rebuilt history always keep the original
text.

When a summary model is wired, turns that scroll out of the token window are
folded incrementally into a per-session rolling summary (watermark =
`summarized_through_id`) that is injected ahead of the in-window turns as a
labeled synthetic exchange, so older context degrades gradually instead of
vanishing at the window edge. Maintenance runs best-effort after the answer
streamed and was persisted: it never raises into the stream, never holds a
transaction across the LLM call, and never rewrites message rows.

Failures after the first event become a terminal `error` event — nothing may
escape `ask` once streaming has started (error-handling spec, streaming rule).
The one exception is the session prelude: it runs before the first event, so
a missing/soft-deleted session raises `NotFoundError` eagerly and the caller
answers with a clean 404 envelope instead of a broken stream.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from uuid import UUID, uuid4

import openai
import structlog
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.conversation_summary import (
    build_conversation_summary_agent,
    render_fold_prompt,
)
from app.agents.qa import (
    ChatDeps,
    SourceCollector,
    build_qa_agent,
    format_context_blocks,
)
from app.agents.rewrite import build_rewrite_agent
from app.core.exceptions import (
    AppError,
    LLMProviderError,
    LLMRateLimitedError,
    NotFoundError,
)
from app.llm.tokens import build_token_counter
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.rag.retriever import Retriever
from app.repositories.chat import ChatMessageRepository, ChatSessionRepository
from app.schemas.chat import (
    AnswerDeltaEvent,
    ChatStreamEvent,
    DoneEvent,
    ErrorEvent,
    QueryRewrittenEvent,
    RunStartedEvent,
    SearchMode,
    SourcesEvent,
    StatusEvent,
)
from app.schemas.search import SearchHit
from app.services.session import derive_title
from app.services.stream_bridge import RunEventBridge

logger = structlog.get_logger(__name__)

# DB-read bound for the history window: enough newest messages that the token
# budget — not the read — is what limits what reaches the model.
HISTORY_READ_LIMIT = 200

# Visible elision marker appended to a bounded turn's history copy: the model
# sees that content was elided instead of silently losing the middle of a
# long pasted document. Part of the assembled-history contract (numbers-only
# observability counts occurrences of it); never persisted.
TRUNCATION_MARKER = "\n\n…[truncated: long message elided]"


def _complete_turns_newest_first(
    messages: Sequence[ChatMessage],
) -> list[tuple[ChatMessage, ChatMessage]]:
    """Group a newest-first message list into complete (user, assistant) turns.

    `user` is the older message of each pair. A leading user message (a failed
    run's unanswered question) is not a complete turn and is skipped; any
    shape that stops pairing cleanly (e.g. two users in a row) ends the walk.
    History is made of complete exchanges only — never an orphan half-turn.
    """
    turns: list[tuple[ChatMessage, ChatMessage]] = []
    index = 1 if messages and messages[0].role is MessageRole.USER else 0
    while index + 1 < len(messages):
        assistant, user = messages[index], messages[index + 1]
        if assistant.role is not MessageRole.ASSISTANT or user.role is not MessageRole.USER:
            break
        turns.append((user, assistant))
        index += 2
    return turns


def _trim_to_tokens(content: str, token_cap: int, measure: Callable[[str], int]) -> str:
    """Longest prefix of `content` measuring at most `token_cap` tokens.

    Binary search on the prefix length (the measure is monotone across
    prefixes); character-boundary exactness is not required — the truncation
    marker tells the model content was elided, and being at or under the cap
    is what matters.
    """
    if token_cap <= 0:
        return ""
    if measure(content) <= token_cap:
        return content
    low, high = 0, len(content)
    # Invariant: content[:low] fits, content[:high] does not.
    while high - low > 1:
        mid = (low + high) // 2
        if measure(content[:mid]) <= token_cap:
            low = mid
        else:
            high = mid
    return content[:low]


def _detached_copy(message: ChatMessage, content: str) -> ChatMessage:
    """A fresh unsaved carrier with the bounded content.

    Persistence keeps full content: bounded copies are only assembled into
    `message_history` and are never added to a session — mutating the ORM
    rows themselves would flush back with the session's next commit."""
    return ChatMessage(session_id=message.session_id, role=message.role, content=content)


def _bound_turn(
    user: ChatMessage,
    assistant: ChatMessage,
    cap: int,
    measure: Callable[[str], int],
) -> tuple[ChatMessage, ChatMessage, int]:
    """Bound one oversized turn to at most `cap` tokens, returning copies.

    The costlier side is truncated first (longest prefix fitting the room the
    other side leaves), then the other side only if it alone still overflows;
    every truncated copy carries `TRUNCATION_MARKER` so the model sees that
    content was elided. The returned cost measures the bounded copies — at or
    under `cap` whenever feasible. The ORM rows are never mutated (they ARE
    the persisted record); with an extremely small cap (below two markers)
    the bounded turn may still exceed `cap` and is then simply not admitted
    by the caller's fit check.
    """
    marker_cost = measure(TRUNCATION_MARKER)
    allowance = max(cap - 2 * marker_cost, 0)  # content tokens for both sides

    def bound(message: ChatMessage, room: int) -> tuple[ChatMessage, int]:
        trimmed = _trim_to_tokens(message.content, room, measure)
        if trimmed == message.content:
            return message, measure(message.content)
        content = trimmed + TRUNCATION_MARKER
        return _detached_copy(message, content), measure(content)

    user_cost, assistant_cost = measure(user.content), measure(assistant.content)
    if user_cost >= assistant_cost:
        bounded_user, user_cost = bound(user, max(allowance - assistant_cost, 0))
        bounded_assistant, assistant_cost = bound(assistant, max(cap - marker_cost - user_cost, 0))
    else:
        bounded_assistant, assistant_cost = bound(assistant, max(allowance - user_cost, 0))
        bounded_user, user_cost = bound(user, max(cap - marker_cost - assistant_cost, 0))
    return bounded_user, bounded_assistant, user_cost + assistant_cost


def select_history_window(
    messages: Sequence[ChatMessage],
    *,
    budget: int,
    measure: Callable[[str], int],
    per_turn_cap: int,
) -> list[ChatMessage]:
    """Pick the newest complete turns whose combined token cost fits `budget`.

    Input is newest-first (the repository's bounded read); the walk takes
    whole turns while they fit and stops at the first one that does not —
    no orphan half-turns. `measure` (text -> tokens) is injected so the
    selection stays a pure, offline-testable function. A turn costing more
    than `per_turn_cap` is bounded first (truncated copies with a visible
    marker, see `_bound_turn`), so one oversized turn cannot evict the whole
    window — older turns still share the remaining budget. `per_turn_cap
    <= 0` disables that guardrail (every turn stands whole). If even the
    bounded newest turn exceeds `remaining`, the result is empty: the
    question stands alone. Output is oldest-first, ready to become
    `message_history`.
    """
    window: list[ChatMessage] = []
    remaining = budget
    for user, assistant in _complete_turns_newest_first(messages):
        turn_cost = measure(user.content) + measure(assistant.content)
        if 0 < per_turn_cap < turn_cost:
            user, assistant, turn_cost = _bound_turn(user, assistant, per_turn_cap, measure)
        if turn_cost > remaining:
            break
        # Newest-first append (assistant, then its user); reversed at the end.
        window.append(assistant)
        window.append(user)
        remaining -= turn_cost
    window.reverse()
    return window


def to_message_history(messages: Sequence[ChatMessage]) -> list[ModelMessage]:
    """Rebuild pydantic-ai `message_history` from stored role/content rows.

    Stored messages are plain (role, content) pairs — tool traffic and
    citations are per-run, not conversation, state — so each row maps to one
    ModelRequest (user) or ModelResponse (assistant) carrying its text.
    """
    history: list[ModelMessage] = []
    for message in messages:
        if message.role is MessageRole.USER:
            history.append(ModelRequest(parts=[UserPromptPart(content=message.content)]))
        else:
            history.append(ModelResponse(parts=[TextPart(content=message.content)]))
    return history


# Label opening the injected rolling summary so the model reads it as a
# compressed memory of earlier turns, never as a verbatim user question. Part
# of the assembled-history contract (tests pin it); never persisted.
SUMMARY_PREFIX_LABEL = "[Summary of earlier conversation]"
_SUMMARY_ACKNOWLEDGEMENT = (
    "Understood. I will treat this summary as context from earlier in our conversation."
)


def summary_prefix(summary: str) -> list[ModelMessage]:
    """The rolling summary as a labeled synthetic exchange leading the history.

    A request/response pair (not a `SystemPromptPart`, which could collide
    with the agent's own instructions) keeps the alternation clean and makes
    the summary's role explicit to the model.
    """
    return [
        ModelRequest(parts=[UserPromptPart(content=f"{SUMMARY_PREFIX_LABEL}\n{summary}")]),
        ModelResponse(parts=[TextPart(content=_SUMMARY_ACKNOWLEDGEMENT)]),
    ]


# Label opening the injected carried-sources preamble so the model reads the
# numbered blocks under it as the previous answer's citable context. Part of
# the assembled-history contract (tests pin it); never persisted.
CARRIED_SOURCES_LABEL = "[Sources cited in the previous answer]"
_CARRIED_ACKNOWLEDGEMENT = "Noted — I'll treat these numbered sources as citable context."


def carried_prefix(hits: list[SearchHit]) -> list[ModelMessage]:
    """The previous run's sources as a labeled synthetic exchange leading the
    history.

    Same mechanism as `summary_prefix`: a request/response pair carrying the
    blocks numbered `[1..k]` — the exact numbers the previous answer cited —
    so a follow-up can read and re-cite them. Pure function of the hits;
    never persisted and not part of history budget accounting (same class as
    tool results).
    """
    blocks = format_context_blocks(hits, start=1)
    return [
        ModelRequest(parts=[UserPromptPart(content=f"{CARRIED_SOURCES_LABEL}\n{blocks}")]),
        ModelResponse(parts=[TextPart(content=_CARRIED_ACKNOWLEDGEMENT)]),
    ]


def select_turns_to_fold(
    messages: Sequence[ChatMessage],
    *,
    watermark: UUID | None,
    budget: int,
    measure: Callable[[str], int],
    per_turn_cap: int,
) -> list[tuple[ChatMessage, ChatMessage]]:
    """Complete turns that scrolled out of the window and are not yet summarized.

    Input is newest-first (the repository's bounded read). The window walk is
    the very one `select_history_window` performs with the same parameters,
    so "evicted" means exactly what the next prelude would not show the
    model. Of those evicted turns, only the ones newer than `watermark` (the
    id of the newest message already folded; ids are uuid7, time-ordered)
    still need folding — the comparison is an ordering, not a membership
    test, so widening the budget later never re-folds a turn that re-enters
    the window. Output is oldest-first original rows (never copies), so the
    caller can advance the watermark to the last assistant id. Turns that
    scrolled past the bounded read before folding are not summarized (the
    documented MVP bound).
    """
    turns = _complete_turns_newest_first(messages)
    retained = (
        len(
            select_history_window(
                messages, budget=budget, measure=measure, per_turn_cap=per_turn_cap
            )
        )
        // 2
    )
    to_fold = [
        (user, assistant)
        for user, assistant in turns[retained:]
        if watermark is None or assistant.id > watermark
    ]
    to_fold.reverse()
    return to_fold


@dataclass(frozen=True, slots=True)
class RewriteOutcome:
    """Result of the best-effort rewrite resolution for one run.

    `query` is the resolved run prompt (rewritten or original); `event` is
    the wire event to emit — `None` when no `query_rewritten` is warranted
    (rewrite skipped, degraded, empty, or unchanged text)."""

    query: str
    event: QueryRewrittenEvent | None = None


@dataclass(slots=True)
class _ChatTurn:
    """Persistence context for one `ask` run (absent in stateless mode).

    `carried` is the previous run's persisted sources (empty when there is
    nothing to carry or the feature is off) — read in the prelude, re-emitted
    as the run's first `sources` batch and injected as leading context.
    """

    session_id: UUID
    history: list[ModelMessage]
    carried: list[SearchHit] = dataclasses_field(default_factory=list)


class ChatService:
    """Streams one knowledge-grounded answer per question."""

    def __init__(
        self,
        retriever: Retriever,
        model: Model,
        *,
        mode: SearchMode,
        extra_tools: Sequence[Tool[ChatDeps]] = (),
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        history_token_budget: int = 2000,
        history_max_turn_fraction: float = 0.5,
        token_counter: Callable[[str], int] | None = None,
        rewrite_model: Model | None = None,
        rewrite_history_turns: int = 3,
        summary_model: Model | None = None,
        summary_max_tokens: int = 400,
        carry_sources_forward: bool = False,
    ) -> None:
        self._retriever = retriever
        self._mode = mode
        self._model = model
        # None = stateless mode: exactly the pre-session service, no DB writes.
        self._session_factory = session_factory
        # History budget in TOKENS (the retired char budget mis-measured
        # mixed CJK/English); the fraction caps one turn's share so a long
        # pasted document cannot evict the rest of history (see
        # `_per_turn_cap`).
        self._history_token_budget = history_token_budget
        self._history_max_turn_fraction = history_max_turn_fraction
        # Token counter: injected (deterministic in tests) or built once on
        # first history assembly from the chat model's name — lazy so
        # stateless services never construct a tokenizer (tiktoken's first
        # use may need its BPE data; `_token_measure` never raises).
        self._token_counter = token_counter
        # History-aware query rewriting (multi-turn follow-ups): a separate
        # injected model lets tests script the rewrite call independently of
        # the stream-only QA fake; None disables the step entirely (first
        # turns and stateless mode never rewrite regardless).
        self._rewrite_agent = build_rewrite_agent(rewrite_model) if rewrite_model else None
        self._rewrite_history_turns = rewrite_history_turns
        # Rolling summary of evicted turns (same injection pattern): None
        # disables it entirely — no summary column consulted, no fold call,
        # no injection — which is byte-identical to the cliff-eviction
        # service. The cap bounds both the stored summary the fold prompt
        # asks for and the budget slice reserved for injecting it.
        self._summary_agent = (
            build_conversation_summary_agent(summary_model) if summary_model else None
        )
        self._summary_max_tokens = summary_max_tokens
        # Carry the previous run's sources into follow-up turns: persisted
        # sources are re-emitted as the run's FIRST `sources` batch (seeding
        # the collector so fresh numbering continues after them) and injected
        # as a labeled leading context pair. False (the constructor default)
        # is byte-identical to the pre-carry service — no write, no emission,
        # no preamble, no seeding.
        self._carry_sources_forward = carry_sources_forward
        # Wrapped MCP tools arrive here (wired once per process in deps.py);
        # the default empty sequence keeps the agent identical to pre-MCP.
        self._agent = build_qa_agent(model, extra_tools=extra_tools)

    def _token_measure(self) -> Callable[[str], int]:
        """The injected counter, or the tiktoken-backed one built once from
        the chat model's name (never raises; offline-safe heuristic fallback
        in `app.llm.tokens`)."""
        if self._token_counter is None:
            self._token_counter = build_token_counter(self._model.model_name)
        return self._token_counter

    def _per_turn_cap(self) -> int:
        """Max tokens one history turn may contribute before bounding.

        A fraction of the budget so the two knobs scale together; a fraction
        `>= 1.0` returns 0, the sentinel that disables the guardrail in
        `select_history_window` (turns stand whole — the old all-or-nothing
        walk)."""
        if self._history_max_turn_fraction >= 1.0:
            return 0
        return round(self._history_token_budget * self._history_max_turn_fraction)

    def _reserved_summary_tokens(self, summary: str) -> int:
        """Budget slice reserved for the injected summary: its measured cost,
        capped at `summary_max_tokens`.

        Reserving (rather than counting the summary as an ordinary leading
        entry) is what keeps turn growth from evicting the summary itself —
        the older context stays represented, which is the point. The cap
        bounds the reservation even if a stored summary overshoots the fold
        prompt's limit."""
        return min(self._token_measure()(summary), self._summary_max_tokens)

    def _turn_budget(self, summary: str | None) -> int:
        """Token budget left for in-window turns once the summary is reserved
        (the full budget when no summary is present)."""
        if not summary:
            return self._history_token_budget
        return max(self._history_token_budget - self._reserved_summary_tokens(summary), 0)

    async def ask(
        self, question: str, *, limit: int = 8, session_id: UUID | None = None
    ) -> AsyncIterator[ChatStreamEvent]:
        """Run one QA turn, yielding typed stream events in contract order.

        `run_id` is bound into structlog contextvars for the whole generator,
        so every log line of the run (including retrieval legs inside tool
        calls) carries it; a resolved session adds its id. The question text
        itself is never logged. With `session_id`: the turn is persisted and
        history informs the model (it never enters `sources` — citations stay
        run-local in numbering; the one deliberate exception is the carried
        FIRST `sources` batch, the previous run's persisted sources, emitted
        right after `run_started` when carry-sources-forward is on). Without
        a wired session factory, nothing here touches the database.
        """
        run_id = uuid4().hex
        structlog.contextvars.bind_contextvars(run_id=run_id)
        started = time.perf_counter()

        # Session prelude — BEFORE the first event: a missing session must
        # surface as a 404 envelope, never as a mid-stream failure.
        turn = await self._prepare_turn(question, session_id)
        if turn is not None:
            structlog.contextvars.bind_contextvars(session_id=str(turn.session_id))

        # The service owns the flush hook: the agent appends, the service
        # drains. Keeps `agents/` free of any streaming knowledge.
        pending: list[list[SearchHit]] = []
        collector = SourceCollector(on_append=pending.append)
        deps = ChatDeps(retriever=self._retriever, limit=limit, collector=collector)

        logger.info("agent_run_started", agent="qa", question_length=len(question))
        yield RunStartedEvent(
            run_id=run_id, mode=self._mode, session_id=turn.session_id if turn else None
        )

        # Carried sources (previous run's persisted hits) go out as the FIRST
        # `sources` batch, before any fresh retrieval — and seeding the
        # collector here makes every fresh batch number after them, so prompt
        # numbering == client numbering for the whole run.
        carried = turn.carried if turn is not None and self._carry_sources_forward else []
        if carried:
            collector.append(carried)
            for batch in _drain(pending):
                yield SourcesEvent(items=batch)

        # Best-effort history-aware rewrite of the run prompt (`_resolve_rewrite`
        # never raises): a follow-up question reaches the agent — and through
        # it the retrieval — as a self-contained query, while the persisted
        # message and the rebuilt history keep the original text. The silent
        # rewrite work is now observable: `status(rewriting_query)` before the
        # rewrite call, `query_rewritten` when it changed the prompt.
        will_rewrite = self._rewrite_agent is not None and bool(turn and turn.history)
        if will_rewrite:
            yield StatusEvent(phase="rewriting_query")
        outcome = await self._resolve_rewrite(question, turn.history if turn else [])
        if outcome.event is not None:
            yield outcome.event
        retrieval_question = outcome.query

        usage_input_tokens: int | None = None
        usage_output_tokens: int | None = None
        answer_parts: list[str] = []
        generating_sent = False
        # The bridge buffers tool lifecycle events from the agent run; the
        # loop below drains them alongside the sources batches. Per-run state
        # only — a fresh bridge every call.
        bridge = RunEventBridge()
        try:
            async with self._agent.run_stream(
                retrieval_question,
                deps=deps,
                # Empty history passes None: a first turn behaves exactly like
                # the stateless service (no empty-sequence edge cases).
                message_history=turn.history if turn and turn.history else None,
                event_stream_handler=bridge.on_agent_event,
            ) as result:
                async for delta in result.stream_text(delta=True, debounce_by=None):
                    # Tool calls (and their sources) can land between parts;
                    # drain before the part so sources always precede the text
                    # they ground. Per tool call the honest progress order is
                    # started → sources → finished (the bridge keeps separate
                    # started/finished queues so the sources flush, which
                    # happens mid-call, slots between them).
                    for started_event in bridge.drain_started():
                        yield started_event
                    for batch in _drain(pending):
                        yield SourcesEvent(items=batch)
                    for finished_event in bridge.drain_finished():
                        yield finished_event
                    if delta:
                        if not generating_sent:
                            # Marks the transition from tool/retrieval work to
                            # visible answer streaming — once per run.
                            yield StatusEvent(phase="generating")
                            generating_sent = True
                        answer_parts.append(delta)
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
            # Inside the try on purpose: the stream has started, so a persist
            # failure must become the terminal `error` event (never escape the
            # generator) — and it leaves the honest record: user message kept,
            # no assistant message, session continuable.
            if turn is not None:
                await self._persist_assistant_message(
                    turn,
                    content="".join(answer_parts),
                    run_id=run_id,
                    sources=collector.hits if self._carry_sources_forward else [],
                )
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
            **({"carried_sources": len(carried)} if self._carry_sources_forward else {}),
        )
        # Rolling-summary maintenance runs only once the turn has fully
        # succeeded (answer streamed AND persisted) and outside the try above:
        # it never raises (own guard), so it can neither become a terminal
        # error event nor escape the generator — a fold failure merely defers
        # to a later turn. `latency_ms` was fixed before it so `done` keeps
        # reporting the answer's own latency.
        if turn is not None:
            await self._maybe_update_rolling_summary(turn.session_id)
        yield DoneEvent(
            run_id=run_id,
            outcome="success",
            tool_calls=collector.tool_calls,
            latency_ms=latency_ms,
            session_id=turn.session_id if turn else None,
        )

    async def _resolve_rewrite(self, question: str, history: list[ModelMessage]) -> RewriteOutcome:
        """Best-effort standalone-question rewrite; never raises.

        Runs only when a rewrite model was injected AND history is non-empty
        (a first turn and stateless mode return the question as-is, byte
        identical to the pre-rewrite service). The last `rewrite_history_turns`
        complete turns back the rewrite run (a turn = user + assistant, so 2N
        messages; `<= 0` uses the full assembled window), and a non-empty
        output replaces the run prompt. Any failure — provider error, empty
        output — degrades to the raw question: this runs after the first event
        is out, so by the streaming rule nothing may escape `ask`, and a
        rewrite problem must never turn a would-be answer into an error event.
        The original question stays the persisted message and the history
        entry; only the run prompt is rewritten.

        The wire event is emitted only when the rewrite actually changed the
        prompt (non-empty output differing from the original) — a skipped or
        degraded or unchanged rewrite is silent, exactly as before.
        """
        if self._rewrite_agent is None or not history:
            return RewriteOutcome(query=question)
        recent = (
            history
            if self._rewrite_history_turns <= 0
            else history[-2 * self._rewrite_history_turns :]
        )
        rewritten: str | None = None
        try:
            result = await self._rewrite_agent.run(question, message_history=recent)
            rewritten = result.output.strip() or None
        except Exception as exc:
            # Error class only — question and rewritten text stay out of logs.
            logger.warning(
                "query_rewrite_failed",
                agent="rewrite",
                error_class=type(exc).__name__,
            )
        resolved = rewritten or question
        logger.info(
            "query_rewrite",
            agent="rewrite",
            applied=rewritten is not None,
            changed=resolved != question,
            original_length=len(question),
            rewritten_length=len(resolved),
        )
        event: QueryRewrittenEvent | None = None
        if rewritten is not None and resolved != question:
            event = QueryRewrittenEvent(
                original=question,
                rewritten=resolved,
                applied=True,
                changed=True,
            )
        return RewriteOutcome(query=resolved, event=event)

    async def _prepare_turn(self, question: str, session_id: UUID | None) -> _ChatTurn | None:
        """Resolve the session, assemble history, persist the user message.

        One transaction: new sessions are created here (title derived from
        the question, no LLM call), existing ones are loaded or raise
        `NotFoundError` (missing/soft-deleted). The user message is durable
        before the run starts — a failed run leaves it as the honest record.
        History is read BEFORE the user message is added: the current
        question reaches the model as the run prompt, not as history. The
        window is bounded in tokens; an oversized turn is admitted
        truncated-with-marker (copies only — rows keep full content). With a
        summary agent wired, a non-empty rolling summary reserves its slice
        of the budget and leads the assembled history as a labeled synthetic
        exchange; without one the summary column is never consulted. With
        carry-sources-forward on, the newest assistant row's persisted
        `sources` (if any) ride on the turn: re-emitted by `ask` as the
        first `sources` batch and injected as a labeled leading pair after
        the summary prefix — pure local computation on rows already read,
        no new I/O, cannot raise into the stream.
        """
        if self._session_factory is None:
            return None
        async with self._session_factory() as session:
            sessions = ChatSessionRepository(session)
            messages = ChatMessageRepository(session)
            summary: str | None = None
            carried: list[SearchHit] = []
            if session_id is None:
                chat_session = await sessions.create(ChatSession(title=derive_title(question)))
                logger.info(
                    "session_created", session_id=str(chat_session.id), title=chat_session.title
                )
                history_rows: Sequence[ChatMessage] = ()
            else:
                existing = await sessions.get_by_id(session_id)
                if existing is None:
                    raise NotFoundError(f"Chat session {session_id} not found")
                chat_session = existing
                if self._summary_agent is not None:
                    summary = existing.rolling_summary or None
                rows = await messages.list_recent_for_session(session_id, limit=HISTORY_READ_LIMIT)
                history_rows = select_history_window(
                    rows,
                    budget=self._turn_budget(summary),
                    measure=self._token_measure(),
                    per_turn_cap=self._per_turn_cap(),
                )
                # Carried sources: ONLY the immediately previous assistant
                # turn's (newest-first scan, first assistant row wins); NULL
                # or empty carries nothing. Old sessions and failed runs have
                # no such row — nothing to carry.
                if self._carry_sources_forward:
                    for row in rows:
                        if row.role is MessageRole.ASSISTANT:
                            if row.sources:
                                carried = [SearchHit.model_validate(item) for item in row.sources]
                            break
                if summary:
                    # Numbers only — the summary is user-derived content.
                    logger.info(
                        "rolling_summary_injected",
                        session_id=str(session_id),
                        summary_length=len(summary),
                        reserved_tokens=self._reserved_summary_tokens(summary),
                    )
                if history_rows:
                    # Numbers only — history contents are user data. The
                    # truncated count is what makes an oversized-turn
                    # bounding observable in ops, not silent.
                    measure = self._token_measure()
                    logger.info(
                        "history_selected",
                        session_id=str(session_id),
                        history_turns=len(history_rows) // 2,
                        history_tokens=sum(measure(message.content) for message in history_rows),
                        truncated_messages=sum(
                            1 for message in history_rows if TRUNCATION_MARKER in message.content
                        ),
                    )
            await messages.add(
                ChatMessage(session_id=chat_session.id, role=MessageRole.USER, content=question)
            )
            await sessions.touch(chat_session.id)
            await session.commit()
            logger.info(
                "chat_message_persisted",
                session_id=str(chat_session.id),
                role=MessageRole.USER.value,
                content_length=len(question),
            )
            history = to_message_history(history_rows)
            if carried:
                # Leading context, after the summary prefix and before the
                # in-window turns: the previous run's blocks numbered [1..k].
                history = carried_prefix(carried) + history
            if summary:
                history = summary_prefix(summary) + history
            return _ChatTurn(session_id=chat_session.id, history=history, carried=carried)

    async def _persist_assistant_message(
        self, turn: _ChatTurn, *, content: str, run_id: str, sources: list[SearchHit]
    ) -> None:
        """Persist the complete assistant answer (own transaction, on done).

        `sources` is the run's collected hits in retrieval order — stored as
        a JSON list so the next turn can re-cite them; empty stores NULL
        (no retrieval, or the carry feature disabled). User messages and
        failed runs never write the column.
        """
        if self._session_factory is None:
            # Unreachable from `ask` (a turn exists only with a factory); the
            # guard keeps the method total rather than asserting.
            return
        async with self._session_factory() as session:
            messages = ChatMessageRepository(session)
            await messages.add(
                ChatMessage(
                    session_id=turn.session_id,
                    role=MessageRole.ASSISTANT,
                    content=content,
                    run_id=UUID(hex=run_id),
                    sources=[hit.model_dump(mode="json") for hit in sources] or None,
                )
            )
            await ChatSessionRepository(session).touch(turn.session_id)
            await session.commit()
            logger.info(
                "chat_message_persisted",
                session_id=str(turn.session_id),
                role=MessageRole.ASSISTANT.value,
                content_length=len(content),
            )

    async def _maybe_update_rolling_summary(self, session_id: UUID) -> None:
        """Fold newly evicted turns into the session's rolling summary; never raises.

        Best-effort maintenance after a successful turn: a failure here must
        not turn an already-streamed answer into an error event, so every
        `Exception` is caught and logged (error class only) — the watermark
        then stays put and the same turns are folded on a later turn.
        `BaseException` (cancellation) propagates as it must.

        Three phases with NO transaction held across the model call:
        (1) a read session loads the summary/watermark and the bounded
        message read, then closes; (2) the turns to fold are computed
        offline and, if any, the summary agent runs; (3) a fresh write
        session stores summary + watermark in one statement. The fold
        boundary mirrors the prelude's window (same budget reservation for
        the summary as it stands now), so a turn is folded as soon as the
        next prelude would stop showing it; because the summary used on turn
        N reflects folds through turn N-1, a summary that grows between
        fold and prelude can leave a turn briefly in neither for one turn.
        Oversized turns are bounded with the same per-turn guardrail before
        folding (cost bound on the fold prompt; the rows stay whole). The
        message rows are never written — only the session row changes.
        """
        if self._summary_agent is None or self._session_factory is None:
            return
        try:
            async with self._session_factory() as session:
                chat_session = await ChatSessionRepository(session).get_by_id(session_id)
                if chat_session is None:
                    return
                rows = await ChatMessageRepository(session).list_recent_for_session(
                    session_id, limit=HISTORY_READ_LIMIT
                )
                existing_summary = chat_session.rolling_summary or None
                watermark = chat_session.summarized_through_id
            measure = self._token_measure()
            cap = self._per_turn_cap()
            folds = select_turns_to_fold(
                rows,
                watermark=watermark,
                budget=self._turn_budget(existing_summary),
                measure=measure,
                per_turn_cap=cap,
            )
            if not folds:
                return
            pairs: list[tuple[str, str]] = []
            for user, assistant in folds:
                if 0 < cap < measure(user.content) + measure(assistant.content):
                    user, assistant, _ = _bound_turn(user, assistant, cap, measure)
                pairs.append((user.content, assistant.content))
            through_id = folds[-1][1].id
            result = await self._summary_agent.run(
                render_fold_prompt(existing_summary, pairs, max_tokens=self._summary_max_tokens)
            )
            new_summary = result.output.strip()
            if not new_summary:
                # An empty fold would erase the memory it was meant to extend;
                # keep the watermark so these turns are retried later.
                logger.warning(
                    "rolling_summary_update_failed",
                    agent="conversation_summary",
                    session_id=str(session_id),
                    error_class="EmptySummary",
                )
                return
            async with self._session_factory() as session:
                await ChatSessionRepository(session).update_rolling_summary(
                    session_id, summary=new_summary, through_id=through_id
                )
                await session.commit()
            # Numbers only — summary and turn contents are user data.
            logger.info(
                "rolling_summary_updated",
                agent="conversation_summary",
                session_id=str(session_id),
                folded_turns=len(folds),
                summary_length=len(new_summary),
                summary_tokens=measure(new_summary),
            )
        except Exception as exc:
            logger.warning(
                "rolling_summary_update_failed",
                agent="conversation_summary",
                session_id=str(session_id),
                error_class=type(exc).__name__,
            )


def _drain(pending: list[list[SearchHit]]) -> list[list[SearchHit]]:
    """Take all pending retrieval batches, leaving the buffer empty."""
    batches = list(pending)
    pending.clear()
    return batches


def _as_app_error(exc: Exception) -> AppError:
    """Map a streaming failure onto the error taxonomy for the error event.

    Provider failures surface from the agent run wrapped in pydantic-ai's own
    types (`ModelHTTPError` for HTTP >= 400, `ModelAPIError` for
    connection/timeout — the production model never lets raw SDK exceptions
    through) or, on paths that bypass pydantic-ai, as raw SDK exceptions;
    both are wrapped here. `AppError` subclasses (e.g. `SearchIndexError`
    raised inside the retrieval tool) already carry the right code/message.
    Unknown exceptions get the generic internal error — details go to logs,
    never to the stream.
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
