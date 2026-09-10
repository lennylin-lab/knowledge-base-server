"""Rewrite agent: standalone-question reformulation for multi-turn follow-ups.

Toolless by design: the agent only reformulates text — it reads the recent
conversation (passed as `message_history` by the orchestrating service) and
returns one self-contained retrieval-facing question, or the question
unchanged when it already is one. `deps_type` is None — no retriever, no
per-run state.

Layering: imports pydantic-ai and the shared prompt loader only — never
`services/` or anything FastAPI (see directory-structure.md).
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models import Model

from app.agents.qa import load_prompt

_REWRITE_INSTRUCTIONS = load_prompt("rewrite.md")


def build_rewrite_agent(model: Model) -> Agent[None, str]:
    """Construct the reusable rewrite agent around an injected model.

    Tests pass a `FunctionModel`; production passes the Settings-built model
    from `llm/models.py` — model construction never happens here. Whether a
    rewrite agent exists at all is the caller's toggle (a disabled deployment
    builds none and never pays the extra call).
    """
    return Agent(model, instructions=_REWRITE_INSTRUCTIONS)
