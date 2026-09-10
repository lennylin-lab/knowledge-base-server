"""Conversation-summary agent: incremental rolling summary of evicted turns.

Toolless by design: the existing summary and the turns that just scrolled
out of the chat history window arrive in the user prompt (rendered by
`render_fold_prompt`), and the agent returns ONE updated summary folding the
new turns into the old. The document summarizer (`agents/summarize.py`) is
title/tags/section-shaped and unsuitable for conversations, hence this
dedicated agent. `deps_type` is None — no retriever, no per-run state.

Layering: imports pydantic-ai and the shared prompt loader only — never
`services/`, `models/`, or anything FastAPI (see directory-structure.md).
The fold input is plain text pairs so the orchestrating service does the
ORM-row-to-text mapping.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai import Agent
from pydantic_ai.models import Model

from app.agents.qa import load_prompt

_CONVERSATION_SUMMARY_INSTRUCTIONS = load_prompt("conversation_summary.md")

# Placeholder the model sees when nothing has been folded yet, so an empty
# summary reads as an explicit "start fresh" rather than a blank section.
_NO_SUMMARY = "(none yet)"


def render_fold_prompt(
    existing_summary: str | None,
    folded_turns: Sequence[tuple[str, str]],
    *,
    max_tokens: int,
) -> str:
    """Render one fold request: the current summary, the evicted turns, and
    the length bound the updated summary must respect.

    `folded_turns` is oldest-first `(user_text, assistant_text)` pairs — the
    complete turns between the watermark and the retained window. The bound
    is stated in the request (not baked into the instructions) so the
    Settings value reaches the model without a prompt-file edit.
    """
    turns = "\n\n".join(f"User: {user}\nAssistant: {assistant}" for user, assistant in folded_turns)
    return (
        "# Existing summary\n"
        f"{existing_summary.strip() if existing_summary else _NO_SUMMARY}\n\n"
        "# Newly evicted turns (oldest first)\n"
        f"{turns}\n\n"
        "Fold the newly evicted turns into the existing summary and output the "
        f"single updated summary. Keep it within {max_tokens} tokens."
    )


def build_conversation_summary_agent(model: Model) -> Agent[None, str]:
    """Construct the reusable conversation-summary agent around an injected model.

    Tests pass a `FunctionModel`; production passes the Settings-built model
    from `llm/models.py` — model construction never happens here. Whether a
    summary agent exists at all is the caller's toggle (a disabled deployment
    builds none and never pays the extra call).
    """
    return Agent(model, instructions=_CONVERSATION_SUMMARY_INSTRUCTIONS)
