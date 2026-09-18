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


def get_chat_model(settings: Settings, model_name: str | None = None) -> Model:
    """Build the Pydantic AI chat model from Settings (OpenAI-compatible).

    `model_name` (the startup-resolved effective name, threaded from
    `api/deps.py`) wins; the Settings fallbacks keep direct construction
    (tests, tooling) working — pydantic-ai requires a concrete name.
    """
    resolved = model_name or settings.CHAT_MODEL or settings.DEFAULT_CHAT_MODEL
    # The client is injected rather than letting the provider build its own so
    # timeouts/retries are configured in exactly one place — this module, with
    # values from Settings (CHAT_REQUEST_TIMEOUT / CHAT_MAX_RETRIES; generous
    # timeout, low retries — sized for long non-streaming structured-output
    # calls, see config.py).
    client = AsyncOpenAI(
        base_url=settings.CHAT_BASE_URL,
        api_key=settings.CHAT_API_KEY.get_secret_value(),
        timeout=settings.CHAT_REQUEST_TIMEOUT,
        max_retries=settings.CHAT_MAX_RETRIES,
        # The SDK's "OpenAI/Python x.y" UA is blocked by some relay providers'
        # WAFs (403 "Your request was blocked"); send a neutral one instead.
        default_headers={"User-Agent": "knowledge-base-server"},
    )
    return OpenAIChatModel(
        resolved,
        provider=OpenAIProvider(openai_client=client),
    )
