"""Summarize agent: plain-text summary of one supplied document.

Toolless by design (v1 scope): the content to summarize arrives in the user
prompt, so the same reusable agent serves both paths the orchestrating
service picks — a single pass for short documents, or section-by-section
passes plus a combine pass for long ones. Per-request state (document title
and tags) flows through `SummarizeDeps`; the agent object stays stateless.

Layering: imports an `agents/` peer and nothing else domain-side — never
`services/` or anything FastAPI (see directory-structure.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai import Agent
from pydantic_ai.models import Model

from app.agents.qa import load_prompt

_SUMMARIZE_INSTRUCTIONS = load_prompt("summarize.md")


@dataclass(slots=True)
class SummarizeDeps:
    """Document context for one summarize run; the agent itself is reusable."""

    title: str
    tags: list[str]


def render_document_prompt(
    deps: SummarizeDeps,
    body: str,
    *,
    section: tuple[int, int] | None = None,
    max_tokens: int,
) -> str:
    """Render one summarize request: document header plus the content itself.

    `section=(i, n)` marks a map pass over one chunk of a longer document —
    the model then summarizes that section alone instead of the whole body.
    """
    lines = [f"# Document: {deps.title}", f"Tags: {', '.join(deps.tags) or 'none'}"]
    if section is not None:
        index, total = section
        lines.append(f"Section {index} of {total} of this document follows.")
        lines.append(f"Summarize this section within {max_tokens} tokens.")
    else:
        lines.append(f"Summarize the content below within {max_tokens} tokens.")
    return "\n".join(lines) + "\n\n" + body


def render_reduce_prompt(deps: SummarizeDeps, summaries: Sequence[str], *, max_tokens: int) -> str:
    """Render the combine request over numbered section summaries."""
    numbered = "\n\n".join(f"{index}. {summary}" for index, summary in enumerate(summaries, 1))
    return (
        f"# Document: {deps.title}\n"
        f"Tags: {', '.join(deps.tags) or 'none'}\n\n"
        "This document was too long to summarize at once, so it was summarized "
        "section by section. The numbered section summaries follow. Combine them "
        "into one coherent summary of the whole document within "
        f"{max_tokens} tokens: resolve overlaps and repeated mentions, keep "
        "every distinct entity, decision, and outcome, drop section-to-section "
        "transitions, and compress harder if the section summaries were verbose.\n\n"
        f"{numbered}"
    )


def build_summarize_agent(model: Model) -> Agent[SummarizeDeps, str]:
    """Construct the reusable summarize agent around an injected model.

    Tests pass a `FunctionModel`; production passes the Settings-built model
    from `llm/models.py` — model construction never happens here.
    """
    return Agent(model, deps_type=SummarizeDeps, instructions=_SUMMARIZE_INSTRUCTIONS)
