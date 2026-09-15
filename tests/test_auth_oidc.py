"""Offline OIDC verification tests (Stage 3).

No real IdP, no network: a local RSA keypair stands in for the IdP signing
key, the verifier gets a fake signing-key provider backed by the matching
public key, and OIDC discovery is exercised against an httpx MockTransport.
Keycloak-shaped claims (`iss`, `aud`, `sub`, `typ: "Bearer"`, `scope`).
"""

from __future__ import annotations

import base64
import json
import time
import typing
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

from app.auth.dependencies import (
    get_current_principal,
    get_service_account_principal,
    get_token_verifier,
)
from app.auth.principal import AuthMethod
from app.auth.verifier import TokenVerifier
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers

ISSUER = "https://idp.example.test/realms/kb"
AUDIENCE = "knowledge-base-server"
KID = "test-signing-key"

type RsaKeys = tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def rsa_keys() -> RsaKeys:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def jwks(rsa_keys: RsaKeys) -> dict[str, object]:
    _, public_key = rsa_keys
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk.update(kid=KID, use="sig", alg="RS256")
    return {"keys": [jwk]}


class FakeJWKClient:
    """Stands in for `PyJWKClient`: always resolves the local public key.

    A token signed by a *different* key still fails signature checking —
    exactly the wrong-key / rotated-out-key case.
    """

    def __init__(self, public_key: object) -> None:
        self._key = public_key

    def get_signing_key_from_jwt(self, token: str) -> typing.Any:
        return SimpleNamespace(key=self._key)


class MissingKeyJWKClient(FakeJWKClient):
    """Simulates an unknown `kid` / stale JWKS cache."""

    def get_signing_key_from_jwt(self, token: str) -> typing.Any:
        raise jwt.PyJWKClientError("Unable to find a signing key that matches")


def make_verifier(rsa_keys: RsaKeys, *, key_client: typing.Any | None = None) -> TokenVerifier:
    client = key_client if key_client is not None else FakeJWKClient(rsa_keys[1])
    return TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_client=client,
        leeway_seconds=30,
    )


def mint(
    rsa_keys: RsaKeys,
    *,
    signing_key: rsa.RSAPrivateKey | None = None,
    claims: dict[str, object] | None = None,
    include_sub: bool = True,
) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "typ": "Bearer",
        "scope": "openid kb.read",
        "preferred_username": "alice",  # display data, never an identity key
    }
    if include_sub:
        payload["sub"] = "user-sub-123"
    payload.update(claims or {})
    return jwt.encode(
        payload, (signing_key or rsa_keys[0]), algorithm="RS256", headers={"kid": KID}
    )


@pytest.fixture(autouse=True)
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the OIDC gate for the boundary dependency (opt-in by design:
    with an unconfigured issuer the dependency denies everything)."""
    monkeypatch.setenv("KB_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KB_OIDC_AUDIENCE", AUDIENCE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_app(verifier: TokenVerifier) -> FastAPI:
    """A minimal app wiring both auth dependencies the way routers would."""
    app = FastAPI()
    register_exception_handlers(app)
    app.dependency_overrides[get_token_verifier] = lambda: verifier

    @app.get("/me")
    async def me(
        principal: typing.Annotated[object, Depends(get_current_principal)],
    ) -> dict:
        return {"subject": principal.subject_id, "method": str(principal.auth_method)}

    @app.get("/internal")
    async def internal(
        principal: typing.Annotated[object, Depends(get_service_account_principal)],
    ) -> dict:
        return {"subject": principal.subject_id, "method": str(principal.auth_method)}

    return app


async def get_json(app: FastAPI, path: str, *, token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        return await ac.get(path, headers=headers)


# --- valid-token path ---


async def test_valid_keycloak_shaped_token_yields_principal(rsa_keys: RsaKeys) -> None:
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=mint(rsa_keys))
    assert response.status_code == 200
    assert response.json() == {"subject": "user-sub-123", "method": str(AuthMethod.OIDC)}


async def test_expired_within_leeway_accepted(rsa_keys: RsaKeys) -> None:
    token = mint(rsa_keys, claims={"exp": int(time.time()) - 10})
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    assert response.status_code == 200


# --- rejection paths (all one generic 401 envelope) ---


async def test_expired_beyond_leeway_rejected(rsa_keys: RsaKeys) -> None:
    token = mint(rsa_keys, claims={"exp": int(time.time()) - 120})
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_wrong_issuer_rejected(rsa_keys: RsaKeys) -> None:
    token = mint(rsa_keys, claims={"iss": "https://evil.example.test/realms/kb"})
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    assert response.status_code == 401


async def test_wrong_audience_rejected(rsa_keys: RsaKeys) -> None:
    token = mint(rsa_keys, claims={"aud": "some-other-client"})
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    assert response.status_code == 401


async def test_bad_signature_rejected(rsa_keys: RsaKeys) -> None:
    # Signed by a key that is NOT in the JWKS (rotated-out key).
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = mint(rsa_keys, signing_key=other_key)
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    assert response.status_code == 401


async def test_unknown_kid_rejected(rsa_keys: RsaKeys) -> None:
    token = mint(rsa_keys)
    verifier = make_verifier(rsa_keys, key_client=MissingKeyJWKClient(rsa_keys[1]))
    response = await get_json(build_app(verifier), "/me", token=token)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "claims",
    [
        {"typ": "JWT", "token_use": "id"},
        {"typ": "id_token"},
        {"token_use": "id"},
    ],
)
async def test_non_access_token_type_rejected(rsa_keys: RsaKeys, claims: dict[str, object]) -> None:
    token = mint(rsa_keys, claims=claims)
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    assert response.status_code == 401


async def test_missing_sub_rejected(rsa_keys: RsaKeys) -> None:
    token = mint(rsa_keys, include_sub=False)
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    assert response.status_code == 401


async def test_missing_authorization_header_rejected(rsa_keys: RsaKeys) -> None:
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me")
    assert response.status_code == 401


async def test_non_bearer_authorization_header_rejected(rsa_keys: RsaKeys) -> None:
    app = build_app(make_verifier(rsa_keys))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


async def test_malformed_token_rejected(rsa_keys: RsaKeys) -> None:
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token="not-a-jwt")
    assert response.status_code == 401


async def test_error_envelope_is_generic(rsa_keys: RsaKeys) -> None:
    """No verification reason (issuer/signature/expiry/...) may leak."""
    token = mint(rsa_keys, claims={"iss": "https://evil.example.test/realms/kb"})
    response = await get_json(build_app(make_verifier(rsa_keys)), "/me", token=token)
    body = response.json()["error"]
    assert body["message"] == "Authentication required"
    assert body["details"] == {}


# --- OIDC discovery (provider-neutral path) ---


async def test_jwks_uri_discovered_from_issuer(
    rsa_keys: RsaKeys,
    jwks: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no explicit JWKS URL the discovery document is consulted once and
    its `jwks_uri` feeds the (bounded-cache) key client."""
    discovery_doc = {
        "issuer": ISSUER,
        "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    }
    captured_uris: list[str] = []

    class RecordingJWKClient:
        def __init__(self, uri: str, **kwargs: object) -> None:
            captured_uris.append(uri)

        def get_signing_key_from_jwt(self, token: str) -> typing.Any:
            return SimpleNamespace(key=rsa_keys[1])

    monkeypatch.setattr("app.auth.verifier.PyJWKClient", RecordingJWKClient)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ISSUER + "/.well-known/openid-configuration"
        return httpx.Response(200, json=discovery_doc)

    verifier = TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        principal = await verifier.verify(mint(rsa_keys))
    finally:
        await verifier.aclose()
    assert principal.subject_id == "user-sub-123"
    assert captured_uris == [f"{ISSUER}/protocol/openid-connect/certs"]


# --- service-account path ---


@pytest.fixture
def service_account_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SERVICE_ACCOUNT_KEY", "internal-test-key")
    monkeypatch.setenv("KB_SERVICE_ACCOUNT_SUBJECT", "svc-kb-internal")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_service_account_key_accepted(service_account_env: None, rsa_keys: RsaKeys) -> None:
    response = await get_json(
        build_app(make_verifier(rsa_keys)), "/internal", token="internal-test-key"
    )
    assert response.status_code == 200
    assert response.json() == {
        "subject": "svc-kb-internal",
        "method": str(AuthMethod.SERVICE_ACCOUNT),
    }


async def test_wrong_service_account_key_rejected(
    service_account_env: None, rsa_keys: RsaKeys
) -> None:
    response = await get_json(build_app(make_verifier(rsa_keys)), "/internal", token="wrong-key")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_unconfigured_service_account_rejected(
    rsa_keys: RsaKeys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KB_SERVICE_ACCOUNT_KEY", raising=False)
    get_settings.cache_clear()
    try:
        response = await get_json(build_app(make_verifier(rsa_keys)), "/internal", token="anything")
    finally:
        get_settings.cache_clear()
    assert response.status_code == 401
