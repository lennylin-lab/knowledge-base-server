"""Provider-error classification for retry decisions.

The openai SDK's exception shapes are known here and nowhere else outside
`llm/` (error-handling.md: SDK *types* may be imported for mapping; SDK
clients never are). By the time a provider failure reaches callers it is an
`LLMProviderError` whose `__cause__` chain still carries the SDK exception,
so authentication-shaped failures stay distinguishable from outages.
"""

from __future__ import annotations

import openai

from app.core.exceptions import LLMProviderError

# Auth-shaped provider failures: a wrong key or missing permission will not
# fix itself, so retrying would only burn the attempt budget.
_PERMANENT_PROVIDER_CAUSES: tuple[type[BaseException], ...] = (
    openai.AuthenticationError,
    openai.PermissionDeniedError,
)


def is_permanent_provider_error(exc: BaseException) -> bool:
    """True for provider failures a retry cannot fix (401/403-shaped)."""
    return isinstance(exc, LLMProviderError) and isinstance(
        exc.__cause__, _PERMANENT_PROVIDER_CAUSES
    )
