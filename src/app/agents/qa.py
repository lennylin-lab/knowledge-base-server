"""QAAgent: knowledge-grounded answering over the hybrid retriever.

The agent object is reusable across requests; all per-run state (retriever
wiring, result limit, collected sources) flows through `ChatDeps` via the
framework's deps mechanism — never through module or global state.

Layering: this module imports `rag/` and `schemas/` only — never `services/`
or anything FastAPI (see directory-structure.md).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool

from app.rag.retriever import RetrievedChunk, Retriever
from app.schemas.search import SearchHit

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Empty-result marker: the model must see an explicit "nothing found" rather
# than an empty string, which some providers read as a truncated tool result.
_NO_RESULTS = "No knowledge-base results were found for this query."


def load_prompt(name: str) -> str:
    """Read a prompt template from `agents/prompts/` (versioned assets)."""
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


_QA_INSTRUCTIONS = load_prompt("qa.md")


def to_search_hit(chunk: RetrievedChunk) -> SearchHit:
    """Map one hydrated retrieval chunk onto the wire schema."""
    return SearchHit(
        document_id=chunk.key.document_id,
        document_title=chunk.document_title,
        document_tags=list(chunk.document_tags),
        chunk_index=chunk.key.chunk_index,
        content=chunk.content,
        score=chunk.score,
        es_rank=chunk.es_rank,
        vector_rank=chunk.vector_rank,
    )


class SourceCollector:
    """Per-run accumulation of retrieval batches, with an append hook.

    `on_append` is set by the orchestrating service so it can flush `sources`
    stream events as soon as a tool call returns — without the agent knowing
    anything about streaming. External (MCP) tool calls are counted too, so
    the run's `tool_calls` total covers every tool the agent invoked.
    """

    def __init__(self, on_append: Callable[[list[SearchHit]], None] | None = None) -> None:
        self._batches: list[list[SearchHit]] = []
        self._external_calls = 0
        self.on_append = on_append

    def append(self, items: list[SearchHit]) -> None:
        self._batches.append(items)
        if self.on_append is not None:
            self.on_append(items)

    def record_external_tool_call(self) -> int:
        """Count one external (non-retrieval) tool call; returns the new total."""
        self._external_calls += 1
        return self._external_calls

    @property
    def tool_calls(self) -> int:
        """Completed tool calls in this run: retrieval batches + external calls."""
        return len(self._batches) + self._external_calls

    @property
    def total_hits(self) -> int:
        """Hits collected across all batches so far in this run."""
        return sum(len(batch) for batch in self._batches)


@dataclass(slots=True)
class ChatDeps:
    """Per-request agent dependencies; the agent itself stays stateless.

    The natural seam for later multi-turn state (history, session ids) — the
    agent's signature never changes.
    """

    retriever: Retriever
    limit: int
    collector: SourceCollector

    def record_external_tool_call(self) -> None:
        """Satisfies `mcp.tools.McpCallObserver` structurally (no import needed).

        Wrappers built by `mcp/tools.py` duck-type this so external tool calls
        land in the run's `tool_calls` total without `mcp/` importing `agents/`.
        """
        self.collector.record_external_tool_call()


def format_context_blocks(hits: list[SearchHit], *, start: int = 1) -> str:
    """Render retrieval results as numbered context blocks for the model.

    The bracketed numbers are the citation handles the prompt tells the model
    to reuse; the same batches reach the client as `sources` events, so
    bracket numbers in the answer line up with the streamed source list.
    Numbering is run-global (`start` continues across tool calls) because
    clients concatenate `sources` events — restarting at `[1]` per batch
    would make citations ambiguous.
    """
    if not hits:
        return _NO_RESULTS
    return "\n\n".join(
        f"[{number}] {hit.document_title} "
        f"(chunk {hit.chunk_index}; tags: {', '.join(hit.document_tags) or 'none'})\n"
        f"{hit.content}"
        for number, hit in enumerate(hits, start=start)
    )


async def search_knowledge(ctx: RunContext[ChatDeps], query: str) -> str:
    """Search the knowledge base for chunks relevant to a query.

    Use the most distinctive terms of the question. Returns numbered context
    blocks (`[1] title ... content`) whose bracketed numbers are the citation
    handles for the answer, or an explicit no-results marker.
    """
    outcome = await ctx.deps.retriever.retrieve(query, limit=ctx.deps.limit)
    hits = [to_search_hit(item) for item in outcome.items]
    # Offset before appending: batch N numbers past everything already
    # collected, so citations stay unique for the whole run.
    start = ctx.deps.collector.total_hits + 1
    ctx.deps.collector.append(hits)
    return format_context_blocks(hits, start=start)


def build_qa_agent(
    model: Model, extra_tools: Sequence[Tool[ChatDeps]] = ()
) -> Agent[ChatDeps, str]:
    """Construct the reusable QA agent around an injected model.

    Tests pass a `FunctionModel`; production passes the Settings-built model
    from `llm/models.py` — model construction never happens here.

    `extra_tools` (wrapped MCP tools in production) register after
    `search_knowledge`; the empty default keeps the agent's toolset — and its
    behavior — byte-identical to the pre-MCP agent.
    """
    return Agent(
        model,
        deps_type=ChatDeps,
        instructions=_QA_INSTRUCTIONS,
        tools=[search_knowledge, *extra_tools],
    )
