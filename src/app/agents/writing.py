"""WritingAgent: retrieval-optional assistance for authoring documents.

The agent object is reusable across requests; all per-run state (retriever
wiring, result limit, collected sources) flows through `WritingDeps` via the
framework's deps mechanism — never through module or global state.

Unlike the QA agent, retrieval here is OPTIONAL: the model decides whether
the user's knowledge base would improve the work, and a run with zero tool
calls is valid and complete (the prompt and the tool description both say
so). Shared machinery (source collection, context-block formatting, prompt
loading, hit mapping) is imported from the QA peer instead of duplicated.

Layering: imports `rag/` and `agents/` peers only — never `services/` or
anything FastAPI (see directory-structure.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool

from app.agents.qa import (
    SourceCollector,
    format_context_blocks,
    load_prompt,
    to_search_hit,
)
from app.rag.retriever import Retriever

_WRITING_INSTRUCTIONS = load_prompt("writing.md")

# The instruction used when a request carries none (prompts/writing.md #7).
DEFAULT_INSTRUCTION = "Continue this draft and improve it."


@dataclass(slots=True)
class WritingDeps:
    """Per-request agent dependencies; the agent itself stays stateless.

    Structurally the QA `ChatDeps` shape (retriever, limit, source collector)
    so wrapped MCP tools duck-type it identically later; a distinct type
    keeps the two agents' deps free to diverge.
    """

    retriever: Retriever
    limit: int
    collector: SourceCollector
    # Tenant scope of the run (Stage 5): every retrieval tool call filters
    # both legs on it — the agent can never ground on another tenant's docs.
    tenant_id: UUID

    def record_external_tool_call(self) -> None:
        """Satisfies `mcp.tools.McpCallObserver` structurally (no import needed).

        Wrappers built by `mcp/tools.py` duck-type this so external tool calls
        land in the run's `tool_calls` total without `mcp/` importing `agents/`.
        """
        self.collector.record_external_tool_call()


def render_writing_prompt(draft: str, instruction: str | None = None) -> str:
    """Render one writing request: the draft verbatim plus the instruction.

    An absent (or empty) instruction falls back to the documented default —
    the prompt contract then tells the model what that means.
    """
    return f"# Draft\n\n{draft}\n\n# Instruction\n\n{instruction or DEFAULT_INSTRUCTION}"


async def search_knowledge(ctx: RunContext[WritingDeps], query: str) -> str:
    """Search the user's knowledge base for their own notes on a topic.

    Optional: call only when grounding the work in the user's notes would
    improve it; query with the topic's most distinctive terms, not the whole
    draft. Returns numbered context blocks (`[1] title ... content`) whose
    bracketed numbers are the citation handles for the reply, or an explicit
    no-results marker.
    """
    outcome = await ctx.deps.retriever.retrieve(
        query, limit=ctx.deps.limit, tenant_id=ctx.deps.tenant_id
    )
    hits = [to_search_hit(item) for item in outcome.items]
    # Offset before appending: batch N numbers past everything already
    # collected, so citations stay unique for the whole run (same rule as QA).
    start = ctx.deps.collector.total_hits + 1
    ctx.deps.collector.append(hits)
    return format_context_blocks(hits, start=start)


def build_writing_agent(
    model: Model, extra_tools: Sequence[Tool[WritingDeps]] = ()
) -> Agent[WritingDeps, str]:
    """Construct the reusable writing agent around an injected model.

    Tests pass a `FunctionModel`; production passes the Settings-built model
    from `llm/models.py` — model construction never happens here.

    `extra_tools` (wrapped MCP tools, wired in a later task) register after
    `search_knowledge`; the empty default keeps the agent's toolset — and its
    behavior — identical to the writing-only agent.
    """
    return Agent(
        model,
        deps_type=WritingDeps,
        instructions=_WRITING_INSTRUCTIONS,
        tools=[search_knowledge, *extra_tools],
    )


class DraftOutput(BaseModel):
    """Structured draft produced by the writing agent's output mode.

    `content` is full proposed document markdown (front matter included);
    `title` is optional and only consulted when the content's front matter
    carries no title of its own. Lives in `agents/` (not schemas/) because it
    is the agent's declared output_type — the same layer as the agent itself.
    """

    content: str
    title: str | None = None


def build_draft_agent(model: Model) -> Agent[WritingDeps, DraftOutput]:
    """Construct the structured-output draft agent around an injected model.

    Same toolset and instructions as `build_writing_agent` (retrieval stays
    optional), but the run returns a validated `DraftOutput` instead of free
    text — the structured shape the operation service persists as a draft.
    Tests pass a `FunctionModel` with a matching output schema; production
    passes the Settings-built model from `llm/models.py`.
    """
    return Agent(
        model,
        deps_type=WritingDeps,
        instructions=_WRITING_INSTRUCTIONS,
        tools=[search_knowledge],
        output_type=DraftOutput,
    )
