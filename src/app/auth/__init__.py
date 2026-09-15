"""Request authentication boundary.

JWT/OIDC verification lives ONLY here (plus `core/config.py` settings);
services and repositories never see tokens, claims, or JWKS — they receive
the typed `Principal` produced at the API boundary.
"""

from __future__ import annotations

from app.auth.dependencies import (
    get_current_principal,
    get_service_account_principal,
    get_token_verifier,
)
from app.auth.principal import AuthMethod, Principal
from app.auth.verifier import TokenVerifier

__all__ = [
    "AuthMethod",
    "Principal",
    "TokenVerifier",
    "get_current_principal",
    "get_service_account_principal",
    "get_token_verifier",
]
