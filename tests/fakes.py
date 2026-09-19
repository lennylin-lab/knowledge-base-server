"""Offline test doubles shared across test modules (importable as `fakes`)."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

import structlog
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.mcp.manager import McpToolResult
from app.models.document_chunk import EMBEDDING_DIM
from app.rag.chunker import Chunk
from app.rag.retriever import ChunkKey, RetrievedChunk, SearchOutcome

if TYPE_CHECKING:
    from app.core.config import Settings


def basis_vector(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """One-hot unit vector: basis(0) and basis(1) are orthogonal (cosine 1)."""
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


def vector_at_distance(base: list[float], other: list[float], distance: float) -> list[float]:
    """Unit vector at exactly `distance` cosine from `base`, in the plane of
    `base` and `other` (both unit-norm and orthogonal).

    Lets scripted worlds place a query at a precise gate-relevant distance
    from the corpus vectors without disturbing orthogonal defaults: the
    result is `(1 - distance) * base + sqrt(1 - (1 - distance)**2) * other`.
    """
    cos = 1.0 - distance
    sin = math.sqrt(max(0.0, 1.0 - cos * cos))
    return [cos * b + sin * o for b, o in zip(base, other, strict=True)]


logger = structlog.get_logger(__name__)

# Retriever constructor kwargs turning every relevance gate off (the
# pre-gates behavior) for tests that exercise legacy retrieval semantics
# rather than the gates themselves.
GATES_OFF: dict[str, float] = {
    "bm25_min_score": 0.0,
    "vector_max_distance": 2.0,
    "rrf_min_relative": 0.0,
}


class FakeEmbeddingProvider:
    """Deterministic offline stand-in: hash-seeded, unit-norm vectors.

    Identical texts embed to identical vectors; `error` (if set) is raised on
    the next call to script provider-failure paths.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim
        self.calls: list[list[str]] = []
        self.error: Exception | None = None

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)
        vector = [rng.uniform(-1.0, 1.0) for _ in range(self._dim)]
        norm = math.sqrt(sum(component * component for component in vector))
        return [component / norm for component in vector]


class ScriptedEmbeddingProvider:
    """Preset vector map (text → vector); unmapped texts get `default`.

    Lets tests plant known neighbors: index-time chunk texts and the search
    query can be scripted onto the same vector (distance 0) or orthogonal
    ones (distance 1). `error`, when set, raises on the next call — for
    provider-failure paths.
    """

    def __init__(self, default: list[float] | None = None) -> None:
        self.vectors: dict[str, list[float]] = {}
        self.default: list[float] = default if default is not None else basis_vector(0)
        self.error: Exception | None = None
        self.calls: list[list[str]] = []

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [self.vectors.get(text, self.default) for text in texts]


class RecordingEsStore:
    """Stands in for search.es.ensure_index / replace_document_chunks."""

    def __init__(self) -> None:
        self.ensure_calls: list[str] = []
        self.replace_calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def ensure_index(self, client: object, index: str) -> None:
        self.ensure_calls.append(index)
        if self.error is not None:
            raise self.error

    async def replace_chunks(
        self,
        client: object,
        *,
        index: str,
        tenant_id: str,
        document_id: UUID,
        title: str,
        tags: list[str],
        chunks: list[Chunk],
    ) -> None:
        self.replace_calls.append(
            {
                "index": index,
                "tenant_id": tenant_id,
                "document_id": document_id,
                "title": title,
                "tags": list(tags),
                "chunks": list(chunks),
            }
        )
        if self.error is not None:
            raise self.error


class StubEsClient:
    """Just enough client for pipeline construction and aclose()."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


# --- SSE wire helper (shared by every streaming-endpoint test) ---


def parse_sse(body: str) -> list[tuple[str, Any]]:
    """Parse an SSE body into (event_name, decoded_data) pairs.

    Tolerant of line-ending style, keepalive comments, and multi-line data —
    only complete event blocks with both fields are returned. Lives here (not
    in one endpoint's test module) because every SSE endpoint streams the one
    chat event vocabulary: one parser, one home.
    """
    events: list[tuple[str, Any]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        name: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line.strip() or line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if name is not None and data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


# --- chat doubles (offline; no live LLM anywhere near these) ---


def retrieved_chunk(
    document_id: UUID | None = None,
    *,
    chunk_index: int = 0,
    content: str = "chunk content",
    document_title: str = "Notes",
    document_tags: list[str] | None = None,
    score: float = 0.5,
    es_rank: int | None = 1,
    vector_rank: int | None = 1,
) -> RetrievedChunk:
    """A hydrated retrieval chunk with sane defaults; ids are random."""
    return RetrievedChunk(
        key=ChunkKey(document_id or UUID(int=0), chunk_index),
        score=score,
        es_rank=es_rank,
        vector_rank=vector_rank,
        content=content,
        document_title=document_title,
        document_tags=document_tags or [],
    )


class StubRetriever:
    """Stands in for `rag.retriever.Retriever`: scripted outcome or a raise.

    Records `(query, limit, tenant_id)` per call so tests assert what the
    agent's tool actually asked for — including that the tenant scope is
    threaded (a run without a scope must fail loudly, not search globally).
    """

    def __init__(
        self, outcome: SearchOutcome | None = None, error: Exception | None = None
    ) -> None:
        self.outcome = outcome if outcome is not None else empty_outcome()
        self.error = error
        self.calls: list[tuple[str, int, UUID | None]] = []

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 10,
        tenant_id: UUID | None = None,
        tag: str | None = None,
    ) -> SearchOutcome:
        self.calls.append((query, limit, tenant_id))
        if self.error is not None:
            raise self.error
        return self.outcome


def empty_outcome(mode: Literal["hybrid", "bm25"] = "hybrid") -> SearchOutcome:
    """A retrieval outcome with no hits."""
    return SearchOutcome(mode=mode, items=[], es_hits=0, vector_hits=0)


class FakeMcpManager:
    """Stands in for `mcp.manager.McpManager`: scripted result or a raise.

    Records `(server, tool, arguments)` per call so tests assert what a
    wrapped agent tool actually asked the manager for. `build_agent_tools`
    only touches `call_tool`, so no other surface is needed.
    """

    def __init__(
        self,
        result: McpToolResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result if result is not None else McpToolResult(text="42", structured=None)
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> McpToolResult:
        self.calls.append((server, tool, dict(arguments)))
        if self.error is not None:
            raise self.error
        return self.result


def _tool_return_count(messages: list[ModelMessage]) -> int:
    """Completed tool calls visible in the run's message history."""
    return sum(
        isinstance(part, ToolReturnPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


def scripted_chat_model(
    *,
    tool_calls: Sequence[str] = (),
    answer_parts: Sequence[str] = (),
    fail_before_run: Exception | None = None,
    fail_during_answer: Exception | None = None,
    fail_after_parts: int = 0,
    tool_results: list[str] | None = None,
    prompts: list[str] | None = None,
    histories: list[list[ModelMessage]] | None = None,
    tool_name: str = "search_knowledge",
    tool_args: Sequence[dict[str, Any]] | None = None,
) -> FunctionModel:
    """FunctionModel scripting a streaming agent turn: tool calls, then text.

    Phase detection is behavioral, like a real model: while fewer tool results
    are visible in the history than scripted `tool_calls`, the next model
    request asks for the tool; after that, `answer_parts` stream as text
    deltas (one yield each — matching `debounce_by=None` streaming).

    `tool_name`/`tool_args` script calls to a non-retrieval tool (e.g. a
    wrapped MCP tool): each scripted call sends `tool_args[i]` (defaulting to
    `{"query": tool_calls[i]}` for `search_knowledge`).

    `fail_before_run` raises on every model request (provider down at start);
    `fail_during_answer` raises after `fail_after_parts` text parts were
    yielded (provider died mid-stream).

    `tool_results`, when given, accumulates the tool return values as the
    model saw them (one entry per completed call, in order) — the observable
    for citation-numbering and MCP-error-string assertions.

    `prompts`, when given, records the user prompt seen by each model request
    (the same text repeats per request within one run, so its length doubles
    as the model-request count) — the observable for what the orchestrator
    actually put in the prompt.

    `histories`, when given, records the FULL message list of each model
    request — the observable for multi-turn assertions (prior turns arriving
    as `message_history` appear here ahead of the current prompt).
    """
    queries = list(tool_calls)
    parts = list(answer_parts)
    args = list(tool_args) if tool_args is not None else None

    async def stream_function(messages: list[ModelMessage], info: object) -> Any:
        if fail_before_run is not None:
            raise fail_before_run
        if prompts is not None:
            prompts.append(_first_user_prompt(messages))
        if histories is not None:
            histories.append(list(messages))
        if tool_results is not None:
            fresh = [
                str(part.content)
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == tool_name
            ]
            # History grows per model request; record only returns not seen yet.
            tool_results.extend(fresh[len(tool_results) :])
        returned = _tool_return_count(messages)
        if returned < len(queries):
            payload = args[returned] if args is not None else {"query": queries[returned]}
            yield {
                0: DeltaToolCall(
                    name=tool_name,
                    json_args=json.dumps(payload),
                    tool_call_id=f"call_{returned}",
                )
            }
            return
        for index, part in enumerate(parts):
            if fail_during_answer is not None and index == fail_after_parts:
                raise fail_during_answer
            yield part

    return FunctionModel(stream_function=stream_function, model_name="scripted-qa")


def _first_user_prompt(messages: list[ModelMessage]) -> str:
    """Text of the run's user prompt (single-turn runs: the first one found)."""
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


def _last_user_prompt(messages: list[ModelMessage]) -> str:
    """Text of the run's user prompt when history may be present.

    The current run's prompt is appended after `message_history`, so the
    LAST user prompt is the run's own input (the first would be the oldest
    prior turn)."""
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    return part.content
    return ""


def scripted_summarize_model(
    outputs: Sequence[str],
    *,
    prompts: list[str] | None = None,
) -> FunctionModel:
    """FunctionModel scripting tool-free summarize passes (plain text out).

    The i-th model request returns `outputs[i]`; the last entry repeats if a
    run makes more requests than scripted, so unexpected extra passes surface
    through `prompts`-length assertions instead of an opaque framework error.
    `prompts`, when given, collects each request's user prompt in order — the
    observable for direct-vs-map-reduce phase checks.

    Built on `function` (not `stream_function`): the summarize path runs
    `Agent.run`, which FunctionModel only supports via a non-streaming
    `function`.
    """
    scripted = list(outputs)
    seen = prompts if prompts is not None else []

    async def function(messages: list[ModelMessage], info: object) -> ModelResponse:
        seen.append(_first_user_prompt(messages))
        index = min(len(seen) - 1, len(scripted) - 1)
        return ModelResponse(parts=[TextPart(content=scripted[index])])

    return FunctionModel(function, model_name="scripted-summarize")


def scripted_rewrite_model(
    outputs: Sequence[str],
    *,
    prompts: list[str] | None = None,
    histories: list[list[ModelMessage]] | None = None,
    fail: Exception | None = None,
) -> FunctionModel:
    """FunctionModel scripting tool-free rewrite passes (plain text out).

    Mirrors `scripted_summarize_model`: the i-th rewrite request returns
    `outputs[i]` (the last entry repeats if more requests arrive), built on
    `function` because the rewrite path runs `Agent.run` (non-streaming).
    `prompts`, when given, collects each request's own prompt — the question
    handed to the rewriter (the last user prompt, since prior turns ride in
    as history); `histories`, when given, records the FULL message list of
    each request — the observable that the rewriter actually saw the prior
    turns. `fail` raises instead, scripting a provider failure for the
    service's degrade-to-raw path.
    """
    scripted = list(outputs)
    seen = prompts if prompts is not None else []

    async def function(messages: list[ModelMessage], info: object) -> ModelResponse:
        if fail is not None:
            raise fail
        seen.append(_last_user_prompt(messages))
        if histories is not None:
            histories.append(list(messages))
        index = min(len(seen) - 1, len(scripted) - 1)
        return ModelResponse(parts=[TextPart(content=scripted[index])])

    return FunctionModel(function, model_name="scripted-rewrite")


def scripted_summary_model(
    outputs: Sequence[str],
    *,
    prompts: list[str] | None = None,
    fail: Exception | None = None,
) -> FunctionModel:
    """FunctionModel scripting tool-free rolling-summary fold passes.

    Mirrors `scripted_rewrite_model`: the i-th fold request returns
    `outputs[i]` (the last entry repeats if more requests arrive), built on
    `function` because the chat service folds with `Agent.run`
    (non-streaming). `prompts`, when given, collects each request's rendered
    fold prompt in order — the observable for what was folded (its length
    doubles as the fold-call count, the AC3 incremental-fold assertion).
    `fail` raises instead of answering, scripting a provider failure for
    the service's best-effort path (AC5). The prompt recording happens
    BEFORE the raise so a failed fold still counts as an attempt.
    """
    scripted = list(outputs)
    seen = prompts if prompts is not None else []

    async def function(messages: list[ModelMessage], info: object) -> ModelResponse:
        seen.append(_first_user_prompt(messages))
        if fail is not None:
            raise fail
        index = min(len(seen) - 1, len(scripted) - 1)
        return ModelResponse(parts=[TextPart(content=scripted[index])])

    return FunctionModel(function, model_name="scripted-summary")


def scripted_association_model(
    picks: Sequence[dict[str, Any]],
    *,
    prompts: list[str] | None = None,
    fail: Exception | None = None,
) -> FunctionModel:
    """FunctionModel scripting one structured-output association run.

    The model responds by calling the agent's single output tool with
    `{"associations": [...]}` — pydantic-ai validates the payload exactly as
    it would a real provider's tool call, so valid pick dicts surface as the
    agent's output type and invalid ones exercise the framework retry path.
    `prompts`, when given, collects the run's user prompt (candidates and all)
    so tests can assert what the deterministic gathering actually surfaced;
    its length doubles as the model-call count. `fail` raises instead, for
    provider-failure mapping.

    The output tool's name is read from `AgentInfo` (not hardcoded) so the
    fake survives framework renaming of the tool.
    """
    payload = {"associations": [dict(pick) for pick in picks]}

    async def function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if fail is not None:
            raise fail
        if prompts is not None:
            prompts.append(_first_user_prompt(messages))
        assert info.output_tools, "association agent must use structured output"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args=json.dumps(payload),
                )
            ]
        )

    return FunctionModel(function, model_name="scripted-association")


def scripted_draft_model(
    content: str,
    title: str | None = None,
    *,
    prompts: list[str] | None = None,
    fail: Exception | None = None,
    fail_after_fragments: Exception | None = None,
    fragments_before_fail: int = 1,
) -> FunctionModel:
    """FunctionModel scripting one structured-output draft run (writing agent).

    Same trick as `scripted_association_model`: the model calls the agent's
    single output tool with `{"content": ..., "title": ...}` so pydantic-ai
    validates it into `DraftOutput` exactly as a real provider would. `prompts`
    collects the run's user prompt; `fail` raises instead (provider down at
    start); `fail_after_fragments` raises after `fragments_before_fail`
    fragments were already streamed (provider died mid-stream — deltas stay
    sent).

    Served as a *streamed* response (`stream_function`): the service runs the
    draft via `Agent.iter`, which requires a streaming model. The tool-call
    arguments arrive in THREE fragments (name + first slice, then the rest),
    matching a real provider's streamed tool call and pinning the service's
    `draft_delta` order and concatenation contract.
    """
    payload = {"content": content, "title": title}

    async def stream_function(messages: list[ModelMessage], info: AgentInfo) -> Any:
        if fail is not None:
            raise fail
        if prompts is not None:
            prompts.append(_first_user_prompt(messages))
        assert info.output_tools, "draft agent must use structured output"
        args = json.dumps(payload)
        cut = max(1, len(args) // 3)
        fragments = [args[:cut], args[cut : 2 * cut], args[2 * cut :]]
        # A leading name/id-only chunk completes the tool-call part; the
        # argument JSON then streams as three plain args fragments — sending
        # partial args alongside the name would end the call early.
        # `fragments_before_fail` counts the name chunk too.
        for index, fragment in enumerate([None, *fragments]):
            if fail_after_fragments is not None and index == fragments_before_fail - 1:
                raise fail_after_fragments
            if fragment is None:
                yield {
                    0: DeltaToolCall(
                        name=info.output_tools[0].name,
                        json_args=None,
                        tool_call_id="call_draft",
                    )
                }
            else:
                yield {0: DeltaToolCall(json_args=fragment)}

    return FunctionModel(stream_function=stream_function, model_name="scripted-draft")


def hermetic_settings(**overrides: object) -> Settings:
    """Settings constructed WITHOUT the ambient `.env` file.

    Explicit test constructions must not merge the developer's real
    `.env` values (e.g. a populated KB_EMBEDDING_API_KEY flipping a
    "chat-key-only" wiring test from bm25 to hybrid). Only the
    `live_llm` smoke tests construct Settings WITH the env file on
    purpose.
    """
    from pydantic import SecretStr

    from app.core.config import Settings

    base: dict[str, object] = {
        "OIDC_ISSUER": "",
        "OIDC_AUDIENCE": "",
        "OIDC_JWKS_URL": "",
        "CHAT_API_KEY": SecretStr(""),
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


class FakeCache:
    """In-memory double for the `Cache` Protocol (offline cache tests).

    Stores raw bytes like `RedisCache` and honors the best-effort contract:
    `fail` injects a fault ("get" / "set" / "incr" / "all") that DEGRADES
    exactly like `RedisCache` — get misses, set drops, incr returns the
    sentinel — after logging a `cache_error` warning (error class only).
    The raising-client degradation of `RedisCache` itself is tested with
    `FakeRedisClient` in test_cache.py.
    """

    def __init__(self, *, fail: str | None = None) -> None:
        self.store: dict[str, bytes] = {}
        self.fail = fail
        self.errors: list[str] = []
        self.get_calls = 0
        self.set_calls = 0
        self.incr_calls = 0

    def _fault(self, op: str) -> bool:
        if self.fail not in (op, "all"):
            return False
        self.errors.append(op)
        logger.warning("cache_error", domain="fake", op=op, error_class="ConnectionError")
        return True

    async def get(self, key: str) -> bytes | None:
        self.get_calls += 1
        if self._fault("get"):
            return None
        return self.store.get(key)

    async def set(self, key: str, value: bytes, *, ttl_seconds: int) -> None:
        self.set_calls += 1
        if self._fault("set"):
            return
        self.store[key] = value

    async def incr(self, key: str) -> int:
        self.incr_calls += 1
        if self._fault("incr"):
            return -1
        self.store[key] = str(int(self.store.get(key, b"0")) + 1).encode()
        return int(self.store[key])

    async def aclose(self) -> None:
        return None
