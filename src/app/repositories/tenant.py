"""Repositories for tenant identity: subject lookup and membership
resolution (Stage 4).

Scope rule: every method resolves ONE subject/tenant/membership by an
explicit key. There is deliberately NO "list all tenants / all users"
method — no request-reachable path may enumerate across tenants (design.md:
no default all-tenants repository fallback).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant, TenantMembership, User


class UserRepository:
    """Resolves users by their immutable IdP subject (or surrogate id)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_subject(self, subject: str) -> User | None:
        """The login-time identity lookup: OIDC `sub` -> user row."""
        stmt = select(User).where(User.subject == subject)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, user_id: UUID) -> User | None:
        stmt = select(User).where(User.id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class TenantRepository:
    """Resolves a tenant by slug (natural key) or id."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.id == tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()


class TenantMembershipRepository:
    """Resolves (tenant, user) membership: the authorization source for
    tenant scope."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_membership(self, tenant_id: UUID, user_id: UUID) -> TenantMembership | None:
        """The principal -> (tenant, role) resolution point."""
        stmt = select(TenantMembership).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.user_id == user_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(self, user_id: UUID) -> list[TenantMembership]:
        """All memberships of ONE user — bounded by that user; never a
        cross-tenant enumeration."""
        stmt = select(TenantMembership).where(TenantMembership.user_id == user_id)
        return list((await self._session.execute(stmt)).scalars().all())
