"""Association agent: select genuinely related documents from candidates.

Toolless and structured-output by design (v1 scope): the orchestrating
service gathers deterministic candidates (vector similarity + tag overlap)
before the run and supplies them in the user prompt, so the agent only
curates — it never searches and it can only echo ids it was given. The
selection is a validated pydantic type (`AssociationsOutput`, the agent's
`output_type`), so a malformed response retries instead of reaching the
service.

Layering: imports an `agents/` peer and nothing else domain-side — never
`services/` or anything FastAPI (see directory-structure.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

from app.agents.qa import load_prompt

_ASSOCIATION_INSTRUCTIONS = load_prompt("association.md")


@dataclass(slots=True)
class AssociationDeps:
    """Source-document context for one run; the agent itself is reusable."""

    title: str
    tags: list[str]


@dataclass(slots=True)
class AssociationCandidate:
    """One deterministic candidate as rendered into the prompt.

    Built by the orchestrating service from repository rows; the agent never
    queries, so this is the only candidate shape it knows.
    """

    document_id: UUID
    title: str
    tags: list[str]
    signal: str


class AssociationPick(BaseModel):
    """The model's selection of one candidate document."""

    document_id: UUID
    reason: str
    strength: str | None = None


class AssociationsOutput(BaseModel):
    """Structured output of one association run: the selected candidates."""

    associations: list[AssociationPick]


def render_association_prompt(
    deps: AssociationDeps, excerpt: str, candidates: Sequence[AssociationCandidate]
) -> str:
    """Render one association request: source header, excerpt, candidates.

    Each candidate carries its exact `document_id` — the handle the prompt
    tells the model to copy back verbatim — plus the deterministic signal
    that surfaced it, so the selection stays grounded in the pre-LLM data.
    """
    lines = [
        f"# Document: {deps.title}",
        f"Tags: {', '.join(deps.tags) or 'none'}",
        "",
        "Excerpt of the document's content follows.",
        excerpt,
        "",
        "# Candidate documents",
    ]
    for number, candidate in enumerate(candidates, start=1):
        lines.append(
            f"[{number}] id={candidate.document_id} — {candidate.title} "
            f"(tags: {', '.join(candidate.tags) or 'none'}; "
            f"signal: {candidate.signal})"
        )
    lines.append("")
    lines.append(
        "Select the genuinely related candidates from the list above and return their exact ids."
    )
    return "\n".join(lines)


def build_association_agent(model: Model) -> Agent[AssociationDeps, AssociationsOutput]:
    """Construct the reusable association agent around an injected model.

    Tests pass a `FunctionModel`; production passes the Settings-built model
    from `llm/models.py` — model construction never happens here.
    """
    return Agent(
        model,
        deps_type=AssociationDeps,
        instructions=_ASSOCIATION_INSTRUCTIONS,
        output_type=AssociationsOutput,
    )
