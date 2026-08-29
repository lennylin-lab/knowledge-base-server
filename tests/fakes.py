"""Offline test doubles shared across test modules (importable as `fakes`)."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from app.mcp.manager import McpToolResult
from app.models.document_chunk import EMBEDDING_DIM
from app.rag.retriever import ChunkKey, RetrievedChunk, SearchOutcome


def basis_vector(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    """One-hot unit vector: basis(0) and basis(1) are orthogonal (cosine 1)."""
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


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
        document_id: UUID,
        title: str,
        tags: list[str],
        chunks: list[str],
    ) -> None:
        self.replace_calls.append(
            {
                "index": index,
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

    Records `(query, limit)` per call so tests assert what the agent's tool
    actually asked for.
    """

    def __init__(
        self, outcome: SearchOutcome | None = None, error: Exception | None = None
    ) -> None:
        self.outcome = outcome if outcome is not None else empty_outcome()
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def retrieve(
        self, query: str, *, limit: int = 10, tag: str | None = None
    ) -> SearchOutcome:
        self.calls.append((query, limit))
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
    tool_name: str = "search_knowledge",
    tool_args: Sequence[dict[str, Any]] | None = None,
) -> FunctionModel:
    """FunctionModel scripting the QA turn: tool calls first, then streamed text.

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
    """
    queries = list(tool_calls)
    parts = list(answer_parts)
    args = list(tool_args) if tool_args is not None else None

    async def stream_function(messages: list[ModelMessage], info: object) -> Any:
        if fail_before_run is not None:
            raise fail_before_run
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
