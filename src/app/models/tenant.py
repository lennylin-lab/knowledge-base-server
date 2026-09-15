"""Tenant identity ORM models (Stage 4 of the multi-tenant rollout).

`tenants`, `users`, and `tenant_memberships`. Identity keys are immutable:
a user's identity is the IdP `sub` (`users.subject`, unique), a tenant's
stable id is its slug (`tenants.slug`, unique). Email is display/recovery
data and is deliberately NOT an identity key. Resource tables get their
non-null `tenant_id` in the Stage 5 migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.utils.ids import uuid7

# The deterministic default tenant the Stage 4 migration (0010) backfills —
# the same fixed UUID, repeated here so application code (the tenant-scope
# dependency, test seeds) and the migration can never drift. The slug comes
# from Settings (TENANT_DEFAULT_SLUG, default "default"); this id is stable.
DEFAULT_TENANT_ID = uuid.UUID("3d1f0a56-7c9e-5b41-9a2d-6f8e1c0b7a10")


class TenantStatus(StrEnum):
    """Lifecycle of a tenant; suspended tenants authenticate no one."""

    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserStatus(StrEnum):
    """Lifecycle of a user record; disabled users resolve no membership."""

    ACTIVE = "active"
    DISABLED = "disabled"


class MembershipRole(StrEnum):
    """Initial tenant RBAC matrix (design.md); enforced from Stage 6."""

    TENANT_ADMIN = "tenant_admin"
    EDITOR = "editor"
    MEMBER = "member"
    VIEWER = "viewer"
    SERVICE_ACCOUNT = "service_account"


class MembershipStatus(StrEnum):
    """Lifecycle of one (tenant, user) membership."""

    ACTIVE = "active"
    DISABLED = "disabled"


def _enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Persist the lowercase values ("active"), not member names ("ACTIVE")
    — must match the enums created by migration 0010."""
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class Tenant(Base):
    """One tenant: the unit of resource isolation."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # Stable, immutable tenant identifier (URL-safe); the natural key.
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        _enum(TenantStatus, "tenant_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("slug", name="ux_tenants_slug"),)


class User(Base):
    """One identity from the IdP; `subject` is the immutable OIDC `sub`."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # Immutable provider subject identifier — THE identity key. Never the
    # email: subjects never change, addresses do.
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    # Display/recovery data only; NULL allowed, uniqueness not enforced.
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        _enum(UserStatus, "user_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("subject", name="ux_users_subject"),)


class TenantMembership(Base):
    """One user's role in one tenant; the authorization source for tenant
    scope (design.md data flow: verified principal -> membership -> guard)."""

    __tablename__ = "tenant_memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MembershipRole] = mapped_column(
        _enum(MembershipRole, "membership_role"), nullable=False
    )
    status: Mapped[MembershipStatus] = mapped_column(
        _enum(MembershipStatus, "membership_status"),
        nullable=False,
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # One membership per (tenant, user); the tenant prefix also indexes
        # tenant-scoped lookups.
        UniqueConstraint("tenant_id", "user_id", name="ux_tenant_memberships_tenant_user"),
        # Membership resolution by user (login: which tenants can I enter?).
        Index("ix_tenant_memberships_user_id", "user_id"),
    )
