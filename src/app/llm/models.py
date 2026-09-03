"""Chat model factory — provider plumbing only, no domain logic.

This is the ONLY place chat models are constructed; retries and timeouts live
in the SDK client built here (mirroring `llm/embeddings.py`), and every value
comes from Settings.
"""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.core.config import Settings

# Same SDK-level budget as the embeddings provider: retries run inside the SDK
# before any error can surface to the service (see quality-guidelines.md).
_REQUEST_TIMEOUT = 60.0
_MAX_RETRIES = 2


def get_chat_model(settings: Settings) -> Model:
    """Build the Pydantic AI chat model from Settings (OpenAI-compatible)."""
    # The client is injected rather than letting the provider build its own so
    # timeouts/retries are configured in exactly one place — this module.
    client = AsyncOpenAI(
        base_url=settings.CHAT_BASE_URL,
        api_key=settings.CHAT_API_KEY.get_secret_value(),
        timeout=_REQUEST_TIMEOUT,
        max_retries=_MAX_RETRIES,
        # The SDK's "OpenAI/Python x.y" UA is blocked by some relay providers'
        # WAFs (403 "Your request was blocked"); send a neutral one instead.
        default_headers={"User-Agent": "knowledge-base-server"},
    )
    return OpenAIChatModel(
        settings.CHAT_MODEL,
        provider=OpenAIProvider(openai_client=client),
    )
