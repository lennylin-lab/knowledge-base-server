"""Repository tests for tenant identity (Stage 4, `db`-marked).

Uses the standard `db_session` fixture (schema via `create_all` — mirrors
the migration-tested shapes). Pins the resolution contract: subject ->
user, slug/id -> tenant, (tenant, user) -> membership, user -> memberships.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import MembershipRole, Tenant, TenantMembership, User
from app.repositories.tenant import TenantMembershipRepository, TenantRepository, UserRepository

pytestmark = pytest.mark.db


async def seed(session: AsyncSession) -> tuple[Tenant, User, User]:
    tenant = Tenant(slug="acme", name="Acme Inc")
    alice = User(subject="sub-alice", email="alice@example.test", display_name="Alice")
    bob = User(subject="sub-bob")
    session.add_all([tenant, alice, bob])
    await session.commit()
    return tenant, alice, bob


async def test_get_user_by_subject(db_session: AsyncSession) -> None:
    tenant, alice, bob = await seed(db_session)
    repo = UserRepository(db_session)
    found = await repo.get_by_subject("sub-alice")
    assert found is not None
    assert found.id == alice.id
    assert found.email == "alice@example.test"
    assert await repo.get_by_subject("sub-nobody") is None

    # bob proves email-nullability; subject is the only identity key.
    bob_row = await repo.get_by_subject("sub-bob")
    assert bob_row is not None and bob_row.id == bob.id
    del tenant


async def test_get_tenant_by_slug_and_id(db_session: AsyncSession) -> None:
    tenant, _alice, _bob = await seed(db_session)
    repo = TenantRepository(db_session)
    found = await repo.get_by_slug("acme")
    assert found is not None and found.id == tenant.id
    assert await repo.get_by_slug("missing") is None
    by_id = await repo.get_by_id(tenant.id)
    assert by_id is not None and by_id.slug == "acme"
    assert await repo.get_by_id(uuid.uuid4()) is None


async def test_membership_resolution(db_session: AsyncSession) -> None:
    tenant, alice, bob = await seed(db_session)
    membership = TenantMembership(tenant_id=tenant.id, user_id=alice.id, role=MembershipRole.EDITOR)
    db_session.add(membership)
    await db_session.commit()

    repo = TenantMembershipRepository(db_session)
    found = await repo.get_membership(tenant.id, alice.id)
    assert found is not None
    assert found.role == MembershipRole.EDITOR
    assert found.status.value == "active"
    # No membership for a user outside the tenant: no fallback, just None.
    assert await repo.get_membership(tenant.id, bob.id) is None
    assert await repo.get_membership(uuid.uuid4(), alice.id) is None


async def test_list_memberships_for_user(db_session: AsyncSession) -> None:
    tenant, alice, _bob = await seed(db_session)
    other = Tenant(slug="globex", name="Globex")
    db_session.add(other)
    await db_session.flush()
    db_session.add_all(
        [
            TenantMembership(tenant_id=tenant.id, user_id=alice.id, role=MembershipRole.MEMBER),
            TenantMembership(
                tenant_id=other.id, user_id=alice.id, role=MembershipRole.TENANT_ADMIN
            ),
        ]
    )
    await db_session.commit()

    repo = TenantMembershipRepository(db_session)
    memberships = await repo.list_for_user(alice.id)
    assert {m.tenant_id for m in memberships} == {tenant.id, other.id}
    assert await repo.list_for_user(uuid.uuid4()) == []
