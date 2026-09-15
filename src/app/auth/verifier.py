"""OIDC/JWT token verification — the only JWT-parsing module in `src/`.

Provider-neutral, Keycloak-compatible: standard OIDC discovery + JWKS +
standard claims (`iss`, `aud`, `exp`, `nbf`, `sub`, `typ`/`token_use`), no
IdP SDK. Every failure — malformed, bad signature, wrong issuer/audience,
expired, wrong token type, unreachable JWKS — maps to ONE non-leaky
`AuthenticationError` (generic 401); the failure class goes to logs only.

JWKS caching is bounded: `PyJWKClient` caches keys with a TTL (Settings:
`OIDC_JWKS_CACHE_TTL_S`) and every live HTTP fetch has a timeout
(`OIDC_HTTP_TIMEOUT_S`), so a slow IdP can never hang a request unboundedly.
"""

from __future__ import annotations

import asyncio
import typing

import httpx
import jwt
import structlog
from jwt import PyJWK, PyJWKClient, PyJWKClientError

from app.auth.principal import AuthMethod, Principal
from app.core.config import Settings
from app.core.exceptions import AuthenticationError

logger = structlog.get_logger(__name__)

# Access-token shapes we accept for the `typ` header/claim. Keycloak issues
# `typ: "Bearer"` on access tokens and `typ: "JWT"` (or OIDC `at+jwt`) in the
# wild; ID tokens self-identify so they can be rejected explicitly.
_ALLOWED_TOKEN_TYPES = {"bearer", "at+jwt", "jwt"}


class _SigningKeyProvider(typing.Protocol):
    """The one method of `PyJWKClient` the verifier needs (test seam)."""

    def get_signing_key_from_jwt(self, token: str) -> typing.Any: ...


class TokenVerifier:
    """Verifies bearer access tokens and produces typed principals.

    Construct via `from_settings` (production) or directly with an injected
    `jwks_client` / `http_client` (offline tests with a local signing key and
    a mocked discovery/JWKS endpoint).
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str = "",
        algorithms: typing.Sequence[str] = ("RS256",),
        jwks_url: str = "",
        leeway_seconds: int = 30,
        jwks_cache_ttl_s: int = 300,
        http_timeout_s: float = 5.0,
        jwks_client: _SigningKeyProvider | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not issuer:
            raise ValueError("TokenVerifier requires a non-empty issuer")
        self._issuer = issuer
        self._audience = audience
        self._algorithms = list(algorithms)
        self._jwks_url = jwks_url
        self._leeway = leeway_seconds
        self._jwks_cache_ttl_s = jwks_cache_ttl_s
        self._timeout = http_timeout_s
        self._jwks_client: _SigningKeyProvider | None = jwks_client
        self._owns_http_client = http_client is None
        self._http_client = http_client
        self._discovered_jwks_uri: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenVerifier:
        """Build from `Settings` (issuer must be configured — the caller,
        `get_token_verifier`, gates the unconfigured deployment)."""
        return cls(
            issuer=settings.OIDC_ISSUER,
            audience=settings.OIDC_AUDIENCE,
            algorithms=settings.OIDC_ALGORITHMS,
            jwks_url=settings.OIDC_JWKS_URL,
            leeway_seconds=settings.OIDC_LEEWAY_SECONDS,
            jwks_cache_ttl_s=settings.OIDC_JWKS_CACHE_TTL_S,
            http_timeout_s=settings.OIDC_HTTP_TIMEOUT_S,
        )

    async def aclose(self) -> None:
        """Close the owned discovery client (no-op when one was injected)."""
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def verify(self, token: str) -> Principal:
        """Verify one access token; any failure raises `AuthenticationError`."""
        try:
            key = await self._get_signing_key(token)
            claims = await asyncio.to_thread(
                self._decode,
                token,
                key,
            )
        except AuthenticationError:
            raise
        except (jwt.PyJWTError, PyJWKClientError, ValueError) as exc:
            # ValueError: structurally malformed tokens escape PyJWKClient's
            # own typing as a bare ValueError.
            self._reject("jwt_rejected", error_class=type(exc).__name__)
        return self._principal_from_claims(claims)

    # --- internals ---

    def _decode(self, token: str, key: PyJWK) -> dict[str, object]:
        return jwt.decode(
            token,
            key=key.key,
            algorithms=self._algorithms,
            issuer=self._issuer,
            audience=self._audience or None,
            leeway=self._leeway,
            # Required claims: a token without any of these is rejected.
            options={"require": ["exp", "iss", "aud", "sub"]},
        )

    async def _get_signing_key(self, token: str) -> PyJWK:
        client = self._jwks_client or await self._ensure_jwks_client()
        try:
            return await asyncio.to_thread(client.get_signing_key_from_jwt, token)
        except (PyJWKClientError, jwt.PyJWTError, ValueError) as exc:
            # Includes "no matching key for kid" and unreachable JWKS.
            self._reject("jwks_key_unavailable", error_class=type(exc).__name__)

    async def _ensure_jwks_client(self) -> _SigningKeyProvider:
        """Build the cached JWKS client once, discovering the URI if needed."""
        if self._jwks_client is None:
            uri = self._jwks_url or await self._discover_jwks_uri()
            self._jwks_client = PyJWKClient(
                uri,
                cache_keys=True,
                # Bounded refresh: keys re-fetch at most this often, so a
                # rotated signing key takes effect within the TTL window.
                lifespan=self._jwks_cache_ttl_s,
                timeout=self._timeout,
            )
        return self._jwks_client

    async def _discover_jwks_uri(self) -> str:
        if self._discovered_jwks_uri is None:
            url = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
            client = self._http_client or httpx.AsyncClient(timeout=self._timeout)
            try:
                response = await client.get(url)
                response.raise_for_status()
                uri = typing.cast("str", response.json().get("jwks_uri", ""))
            except (httpx.HTTPError, ValueError) as exc:
                self._reject("oidc_discovery_failed", error_class=type(exc).__name__)
            if not uri:
                self._reject("oidc_discovery_failed", error_class="MissingJwksUri")
            self._discovered_jwks_uri = uri
        return self._discovered_jwks_uri

    def _principal_from_claims(self, claims: dict[str, object]) -> Principal:
        token_type = str(claims.get("typ", claims.get("token_use", "")) or "").lower()
        # `token_use` is an AWS Cognito-style claim; anything that
        # self-identifies as a non-access token is rejected.
        if claims.get("token_use") not in (None, "access"):
            self._reject("wrong_token_type", token_use=str(claims["token_use"]))
        if token_type and token_type not in _ALLOWED_TOKEN_TYPES:
            self._reject("wrong_token_type", typ=token_type)
        scopes: tuple[str, ...] = ()
        raw_scope = claims.get("scope", claims.get("scp"))
        if isinstance(raw_scope, str):
            scopes = tuple(s for s in raw_scope.split() if s)
        elif isinstance(raw_scope, (list, tuple)):
            scopes = tuple(str(s) for s in raw_scope)
        return Principal(
            subject_id=str(claims["sub"]),
            auth_method=AuthMethod.OIDC,
            scopes=scopes,
            claims=claims,
        )

    def _reject(self, reason: str, **context: object) -> typing.NoReturn:
        """Log the failure class (never the token or raw claims) and raise
        the one generic 401."""
        logger.warning("auth_rejected", reason=reason, **context)
        raise AuthenticationError("Authentication required")
