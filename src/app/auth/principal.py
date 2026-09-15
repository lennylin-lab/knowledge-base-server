"""Typed request principal — the only identity shape that crosses the API
boundary into services (design.md "Principal Model").

`tenant_id`/`membership_role` are populated only from verified claims plus a
server-side membership lookup; client headers (`X-User-ID`, `X-Tenant-ID`,
role headers) are never consulted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class AuthMethod(StrEnum):
    """How the principal was authenticated at the boundary."""

    OIDC = "oidc"
    SERVICE_ACCOUNT = "service_account"


@dataclass(frozen=True)
class Principal:
    """A verified caller identity.

    `subject_id` is the immutable identity key: the OIDC `sub` for users, the
    configured service-account subject for internal calls. `preferred_username`
    and email claims are display data and are deliberately not carried.
    """

    subject_id: str
    auth_method: AuthMethod
    # Tenant scope is resolved by membership lookup (Stage 5 wiring); None
    # until then, and never taken from an unsigned header.
    tenant_id: UUID | None = None
    membership_role: str | None = None
    # Coarse verified token scopes when the IdP includes them.
    scopes: tuple[str, ...] = ()
    # Verified claims kept for claim mapping (e.g. tenant claim) — never
    # logged (logging-guidelines: identity claims are sensitive).
    claims: Mapping[str, object] = field(default_factory=dict)
