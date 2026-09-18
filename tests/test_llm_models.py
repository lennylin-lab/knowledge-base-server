"""Tests for the chat model factory (llm/models.py)."""

from __future__ import annotations

from openai import AsyncOpenAI
from pydantic import SecretStr

from app.core.config import Settings
from app.llm.models import get_chat_model


def _settings(**fields: object) -> Settings:
    # Non-empty key: the SDK refuses to construct without credentials.
    fields.setdefault("CHAT_API_KEY", SecretStr("test-key"))
    return Settings(_env_file=None, **fields)  # type: ignore[arg-type]


def _client_of(model: object) -> AsyncOpenAI:
    # OpenAIChatModel -> provider -> injected client (single construction site).
    provider = model.provider  # type: ignore[attr-defined]
    return provider.client  # type: ignore[attr-defined]


def test_chat_model_uses_settings_timeout_and_retries() -> None:
    """Timeout/retries come from Settings, not hardcoded literals."""
    settings = _settings(
        CHAT_REQUEST_TIMEOUT=123.5,
        CHAT_MAX_RETRIES=7,
        CHAT_MODEL="test-model",
    )

    client = _client_of(get_chat_model(settings))

    assert client.timeout == 123.5
    assert client.max_retries == 7


def test_chat_model_defaults_are_sized_for_structured_output() -> None:
    """Defaults: generous timeout (>= 300s), low retries."""
    client = _client_of(get_chat_model(_settings(CHAT_MODEL="test-model")))

    assert client.timeout >= 300.0
    assert client.max_retries <= 1
