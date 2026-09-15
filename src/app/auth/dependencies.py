"""FastAPI authentication dependencies.

The API boundary is the only place JWT/bearer handling happens: routers (and
later, services via an injected principal) see the typed `Principal` only.
Both dependencies fail with ONE generic 401 envelope (`AuthenticationError`)
— never a reason, never a token echo.

Two explicit paths (design.md):

- `get_current_principal` — OIDC bearer verification (opt-in: requires
  `KB_OIDC_ISSUER`; an unconfigured deployment rejects with 401 rather than
  silently letting traffic through).
- `get_service_account_principal` — the explicit service-account path for
  internal calls (health/operations). Compared against the configured
  `KB_SERVICE_ACCOUNT_KEY` in constant time; never a user identity.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Annotated

import structlog
from fastapi import Depends, Header

from app.auth.principal import AuthMethod, Principal
from app.auth.verifier import TokenVerifier
from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_token_verifier() -> TokenVerifier | None:
    """One verifier (one JWKS cache) per process, like the shared ES client.

    `None` means the OIDC boundary is unconfigured (`KB_OIDC_ISSUER` empty):
    callers must treat that as deny (generic 401), never skip verification.
    `lru_cache` never memoizes raised exceptions, so enabling OIDC is purely
    a Settings change (no code path swap); the None return keeps dependencies
    that merely share this provider (e.g. the tenant-scope resolver on its
    compatibility path) safe on unconfigured deployments.
    """
    if not get_settings().OIDC_ISSUER:
        return None
    return TokenVerifier.from_settings(get_settings())


def require_bearer(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> str:
    """Extract the bearer credential; anything else is one generic 401."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise AuthenticationError("Authentication required")
    token = authorization[len("Bearer ") :].strip()
    if not token:
        raise AuthenticationError("Authentication required")
    return token


async def get_current_principal(
    token: Annotated[str, Depends(require_bearer)],
    verifier: Annotated[TokenVerifier | None, Depends(get_token_verifier)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    """Verify the OIDC bearer token and return the typed principal."""
    if not settings.OIDC_ISSUER or verifier is None:
        # Auth boundary explicitly configured off: deny rather than admit.
        logger.warning("auth_unconfigured", path_oidc=True)
        raise AuthenticationError("Authentication required")
    return await verifier.verify(token)


async def get_service_account_principal(
    token: Annotated[str, Depends(require_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Principal:
    """The explicit service-account path for internal calls.

    Accepts exactly the configured service key (constant-time compare, no
    key material in logs or errors). The resulting principal grants nothing
    beyond routes explicitly wired to this dependency.
    """
    expected = settings.SERVICE_ACCOUNT_KEY.get_secret_value()
    if not expected or not secrets.compare_digest(token, expected):
        logger.warning("auth_rejected", reason="service_account_key_mismatch")
        raise AuthenticationError("Authentication required")
    return Principal(
        subject_id=settings.SERVICE_ACCOUNT_SUBJECT,
        auth_method=AuthMethod.SERVICE_ACCOUNT,
    )
