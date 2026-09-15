"""Authorization-matrix tests (Stage 6): the initial tenant RBAC.

Two layers, both offline by default:

- Pure matrix tests: `ensure_allowed` is the single RBAC decision point; the
  parametrized matrix below pins every role x permission cell.
- API-level tests (db-marked): a micro-app wires the REAL scope dependencies
  from `app.api.deps` with the OIDC path configured (a local RSA keypair
  stands in for the IdP; memberships are resolved server-side), proving the
  matrix is enforced at the route boundary — 401 for bad authn, 403 for
  authenticated-but-unauthorized, 404 (never 403) for cross-tenant ids. One
  real-app smoke pins the documents router wiring; the compatibility path
  (OIDC unconfigured) is pinned by an explicit test here plus every other
  API test module.

No real IdP, tokens, or user data; the manual Keycloak verification recipe
lives in docs/identity-tenants.md.
"""

from __future__ import annotations

import time
import typing
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import (
    ChatScope,
    DocumentWriteScope,
    OperationApplyScope,
    OperationCreateScope,
    SessionWriteScope,
    TenantScope,
)
from app.auth.dependencies import get_token_verifier
from app.auth.rbac import Permission, ensure_allowed
from app.auth.verifier import TokenVerifier
from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import ForbiddenError, register_exception_handlers
from app.models.tenant import (
    DEFAULT_TENANT_ID,
    MembershipRole,
    MembershipStatus,
    Tenant,
    TenantMembership,
    User,
    UserStatus,
)
from app.schemas.document import DocumentCreate
from app.services.document import DocumentService

ISSUER = "https://idp.example.test/realms/kb"
AUDIENCE = "knowledge-base-server"
KID = "rbac-test-key"

type RsaKeys = tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]

# One seeded subject per scenario; "none" has a user row but no membership.
SUBJECTS: dict[str, str] = {
    **{role.value: f"sub-{role.value}" for role in MembershipRole},
    "disabled_membership": "sub-disabled-membership",
    "none": "sub-no-membership",
}


# --- IdP stand-in (local RSA keypair, no network) ---


@pytest.fixture(scope="module")
def rsa_keys() -> RsaKeys:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


class FakeJWKClient:
    """Stands in for `PyJWKClient`: always resolves the local public key."""

    def __init__(self, public_key: object) -> None:
        self._key = public_key

    def get_signing_key_from_jwt(self, token: str) -> typing.Any:
        return SimpleNamespace(key=self._key)


@pytest.fixture
def verifier(rsa_keys: RsaKeys) -> TokenVerifier:
    return TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_client=FakeJWKClient(rsa_keys[1]),
        leeway_seconds=30,
    )


def mint(rsa_keys: RsaKeys, sub: str) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "iat": now,
        "exp": now + 300,
        "typ": "Bearer",
    }
    return jwt.encode(payload, rsa_keys[0], algorithm="RS256", headers={"kid": KID})


@pytest.fixture(autouse=True)
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("KB_OIDC_AUDIENCE", AUDIENCE)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- pure matrix (offline; the single decision point) ---


_EXPECTED_MATRIX: dict[MembershipRole, frozenset[Permission]] = {
    MembershipRole.TENANT_ADMIN: frozenset(Permission),
    MembershipRole.EDITOR: frozenset(
        {
            Permission.DOCUMENT_READ,
            Permission.DOCUMENT_WRITE,
            Permission.SESSION_READ,
            Permission.SESSION_WRITE,
            Permission.CHAT_CREATE,
            Permission.OPERATION_READ,
            Permission.OPERATION_CREATE,
            Permission.OPERATION_APPLY,
        }
    ),
    MembershipRole.MEMBER: frozenset(
        {
            Permission.DOCUMENT_READ,
            Permission.SESSION_READ,
            Permission.CHAT_CREATE,
            Permission.OPERATION_READ,
            Permission.OPERATION_CREATE,
        }
    ),
    MembershipRole.VIEWER: frozenset(
        {
            Permission.DOCUMENT_READ,
            Permission.SESSION_READ,
            Permission.OPERATION_READ,
        }
    ),
    MembershipRole.SERVICE_ACCOUNT: frozenset(),
}


@pytest.mark.parametrize("role", list(MembershipRole))
@pytest.mark.parametrize("permission", list(Permission))
def test_rbac_matrix_cell(role: MembershipRole, permission: Permission) -> None:
    if permission in _EXPECTED_MATRIX[role]:
        ensure_allowed(role, permission)  # must not raise
    else:
        with pytest.raises(ForbiddenError):
            ensure_allowed(role, permission)


def test_viewer_chat_creation_disabled() -> None:
    """design.md "read/create if enabled": chat creation for viewers is
    disabled by default — pinned so enabling it becomes a deliberate change."""
    with pytest.raises(ForbiddenError):
        ensure_allowed(MembershipRole.VIEWER, Permission.CHAT_CREATE)


# --- seeded identity world (db) ---


@pytest.fixture
async def seeded_identities(db_session: AsyncSession) -> None:
    """One active user + membership per role in the default tenant, a disabled
    membership, and a membership-less user; tokens address them via SUBJECTS."""
    for key, subject in SUBJECTS.items():
        user = User(subject=subject, status=UserStatus.ACTIVE)
        db_session.add(user)
        await db_session.flush()  # assign user.id before the FK reference
        if key in {m.value for m in MembershipRole}:
            db_session.add(
                TenantMembership(
                    tenant_id=DEFAULT_TENANT_ID,
                    user_id=user.id,
                    role=MembershipRole(key),
                    status=(
                        MembershipStatus.DISABLED
                        if key == "disabled_membership"
                        else MembershipStatus.ACTIVE
                    ),
                )
            )
    await db_session.commit()


def build_micro_app(verifier: TokenVerifier) -> FastAPI:
    """The REAL scope dependencies on minimal routes: each route is one
    permission cell; a route that passes its scope dependency answers 200.
    The built-in docs/OpenAPI routes are disabled so `/docs` is unambiguous."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    register_exception_handlers(app)
    app.dependency_overrides[get_token_verifier] = lambda: verifier

    @app.get("/docs")
    async def read_docs(tenant: TenantScope) -> dict:
        return {"tenant": str(tenant)}

    @app.post("/docs")
    async def write_docs(tenant: DocumentWriteScope) -> dict:
        return {"tenant": str(tenant)}

    @app.delete("/sessions")
    async def delete_session(tenant: SessionWriteScope) -> dict:
        return {"tenant": str(tenant)}

    @app.post("/chat")
    async def chat(tenant: ChatScope) -> dict:
        return {"tenant": str(tenant)}

    @app.post("/operations")
    async def create_operation(tenant: OperationCreateScope) -> dict:
        return {"tenant": str(tenant)}

    @app.post("/operations/apply")
    async def apply_operation(tenant: OperationApplyScope) -> dict:
        return {"tenant": str(tenant)}

    return app


# (path, method, permission) probed for every role below.
PROBES: list[tuple[str, str, Permission]] = [
    ("/docs", "GET", Permission.DOCUMENT_READ),
    ("/docs", "POST", Permission.DOCUMENT_WRITE),
    ("/sessions", "DELETE", Permission.SESSION_WRITE),
    ("/chat", "POST", Permission.CHAT_CREATE),
    ("/operations", "POST", Permission.OPERATION_CREATE),
    ("/operations/apply", "POST", Permission.OPERATION_APPLY),
]


async def request_json(
    app: FastAPI,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json: dict[str, object] | None = None,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        return await ac.request(method, path, headers=headers, json=json)


@pytest.fixture
def micro_app(verifier: TokenVerifier, db_engine: object) -> FastAPI:
    """The micro-app with `get_db` bound to the disposable test database."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)  # type: ignore[arg-type]

    async def override_get_db() -> typing.AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    probe_app = build_micro_app(verifier)
    probe_app.dependency_overrides[get_db] = override_get_db
    return probe_app


# --- API matrix (OIDC path configured) ---


@pytest.mark.db
@pytest.mark.parametrize("path,method,permission", PROBES)
async def test_admin_passes_every_probed_route(
    rsa_keys: RsaKeys,
    micro_app: FastAPI,
    seeded_identities: None,
    path: str,
    method: str,
    permission: Permission,
) -> None:
    response = await request_json(
        micro_app, method, path, token=mint(rsa_keys, SUBJECTS["tenant_admin"])
    )
    assert response.status_code == 200


@pytest.mark.db
@pytest.mark.parametrize("path,method,permission", PROBES)
async def test_matrix_enforced_for_every_role_and_route(
    rsa_keys: RsaKeys,
    micro_app: FastAPI,
    seeded_identities: None,
    path: str,
    method: str,
    permission: Permission,
) -> None:
    """Every non-admin role x probed route: 200 exactly when the matrix cell
    allows it, else the generic 403 envelope (code `forbidden`)."""
    for role in (MembershipRole.EDITOR, MembershipRole.MEMBER, MembershipRole.VIEWER):
        response = await request_json(
            micro_app, method, path, token=mint(rsa_keys, SUBJECTS[role.value])
        )
        if permission in _EXPECTED_MATRIX[role]:
            assert response.status_code == 200, (role, permission, response.text)
        else:
            assert response.status_code == 403, (role, permission, response.text)
            assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.db
async def test_service_account_membership_role_rejected(
    rsa_keys: RsaKeys, micro_app: FastAPI, seeded_identities: None
) -> None:
    """A membership carrying the service_account role grants no user route."""
    response = await request_json(
        micro_app, "GET", "/docs", token=mint(rsa_keys, SUBJECTS["service_account"])
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.db
async def test_missing_token_is_401_when_oidc_configured(
    rsa_keys: RsaKeys, micro_app: FastAPI, seeded_identities: None
) -> None:
    response = await request_json(micro_app, "GET", "/docs")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.db
async def test_unknown_subject_forbidden(
    rsa_keys: RsaKeys, micro_app: FastAPI, seeded_identities: None
) -> None:
    """Verified token, but the subject was never provisioned server-side."""
    response = await request_json(micro_app, "GET", "/docs", token=mint(rsa_keys, "sub-ghost"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.db
async def test_membershipless_user_forbidden(
    rsa_keys: RsaKeys, micro_app: FastAPI, seeded_identities: None
) -> None:
    response = await request_json(micro_app, "GET", "/docs", token=mint(rsa_keys, SUBJECTS["none"]))
    assert response.status_code == 403


@pytest.mark.db
async def test_disabled_membership_forbidden(
    rsa_keys: RsaKeys, micro_app: FastAPI, seeded_identities: None
) -> None:
    response = await request_json(
        micro_app,
        "GET",
        "/docs",
        token=mint(rsa_keys, SUBJECTS["disabled_membership"]),
    )
    assert response.status_code == 403


@pytest.mark.db
async def test_service_account_key_never_authenticates_user_routes(
    rsa_keys: RsaKeys, micro_app: FastAPI, seeded_identities: None
) -> None:
    """The Gateway service key is not a user identity: on OIDC-configured
    user routes it is just an unparseable token -> generic 401."""
    response = await request_json(micro_app, "GET", "/docs", token="internal-service-key")
    assert response.status_code == 401


# --- real-app wiring smoke + cross-tenant / compatibility policy ---


@pytest.mark.db
async def test_documents_router_enforces_write_scope(
    rsa_keys: RsaKeys,
    app: FastAPI,
    verifier: TokenVerifier,
    db_engine,
    seeded_identities: None,
) -> None:
    """On the REAL documents router: viewer reads (200), member create is 403,
    editor create passes (201), and a cross-tenant id stays a non-leaky 404
    (never 403) for an authorized editor."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db() -> typing.AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_token_verifier] = lambda: verifier

    # A second tenant with one document (FK requires the tenant row).
    async with factory() as session:
        session.add(Tenant(slug="other", name="Other tenant"))
        await session.commit()
    async with factory() as session:
        other_tenant = (
            await session.execute(select(Tenant).where(Tenant.slug == "other"))
        ).scalar_one()
        foreign_doc = await DocumentService(session).create_document(
            DocumentCreate(content="# Other tenant\n\nsecret"), tenant_id=other_tenant.id
        )

    editor = mint(rsa_keys, SUBJECTS["editor"])
    member = mint(rsa_keys, SUBJECTS["member"])
    viewer = mint(rsa_keys, SUBJECTS["viewer"])

    viewer_list = await request_json(app, "GET", "/api/v1/documents", token=viewer)
    assert viewer_list.status_code == 200

    member_create = await request_json(
        app, "POST", "/api/v1/documents", token=member, json={"content": "# x"}
    )
    assert member_create.status_code == 403
    assert member_create.json()["error"]["code"] == "forbidden"

    editor_create = await request_json(
        app, "POST", "/api/v1/documents", token=editor, json={"content": "# ok\n\ntext"}
    )
    assert editor_create.status_code == 201

    foreign_get = await request_json(
        app, "GET", f"/api/v1/documents/{foreign_doc.id}", token=editor
    )
    assert foreign_get.status_code == 404
    assert foreign_get.json()["error"]["code"] == "not_found"


@pytest.mark.db
async def test_compatibility_path_kept_when_oidc_unconfigured(
    db_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """design.md decision: the default-tenant compatibility path stays for the
    MVP — with no issuer configured every route scope passes unauthenticated
    (this is what keeps every existing offline API test green)."""
    monkeypatch.delenv("KB_OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    try:
        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        async def override_get_db() -> typing.AsyncIterator[AsyncSession]:
            async with factory() as session:
                yield session

        probe_app = build_micro_app(TokenVerifier(issuer="unused", audience="unused"))
        probe_app.dependency_overrides[get_db] = override_get_db
        for path, method, _permission in PROBES:
            response = await request_json(probe_app, method, path)
            assert response.status_code == 200, (path, method, response.text)
    finally:
        get_settings.cache_clear()
