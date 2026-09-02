"""Writing request DTO; the stream reuses the chat SSE event payloads.

The response events (`run_started`/`sources`/`answer_delta`/`done`/`error`)
are chat's models, imported from `schemas/chat.py` — one event vocabulary,
one wire contract, no duplication here.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

DRAFT_MAX_CHARS = 50_000


class WritingRequest(BaseModel):
    """One writing-assistance turn: draft text plus optional instruction."""

    draft: str = Field(
        min_length=1, max_length=DRAFT_MAX_CHARS, description="Draft text to assist with"
    )
    instruction: str | None = Field(
        default=None,
        description=(
            "What to do with the draft (continue/rewrite/expand/critique); "
            "default: continue and improve"
        ),
    )
    limit: int = Field(default=8, ge=1, le=20, description="Max chunks per retrieval")
