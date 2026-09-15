"""Tenant RBAC: the initial role matrix and its single enforcement point.

The matrix is the design.md one, expressed as role -> allowed permissions.
`ensure_allowed` is the ONLY place a role/permission decision is made, so the
API dependencies and any future service-side guard can never drift apart.

Policy notes (explicit choices, pinned by tests):

- Cross-tenant access is NOT this module's concern: a principal never reaches
  a permission check for a tenant it has no membership in (the scope resolver
  rejects first with 403; cross-tenant resource ids stay non-leaky 404s).
- Gateway model policy is a separate Gateway-side grant and is deliberately
  NOT represented here — resource roles never imply model access.
- `service_account` principals grant no user-resource permission at all; the
  service-account path exists solely for internal routes wired explicitly.
"""

from __future__ import annotations

from enum import StrEnum

from app.core.exceptions import ForbiddenError
from app.models.tenant import MembershipRole


class Permission(StrEnum):
    """One coarse operation class on tenant resources."""

    DOCUMENT_READ = "document_read"
    DOCUMENT_WRITE = "document_write"
    SESSION_READ = "session_read"
    SESSION_WRITE = "session_write"
    CHAT_CREATE = "chat_create"
    OPERATION_READ = "operation_read"
    OPERATION_CREATE = "operation_create"
    OPERATION_APPLY = "operation_apply"
    MEMBERSHIP_MANAGE = "membership_manage"


_READ_ALL = frozenset(
    {
        Permission.DOCUMENT_READ,
        Permission.SESSION_READ,
        Permission.OPERATION_READ,
    }
)

# The initial matrix (design.md "RBAC Matrix (initial)"). Membership/admin
# routes do not exist yet, but the matrix already pins who would hold them.
_ROLE_MATRIX: dict[MembershipRole, frozenset[Permission]] = {
    MembershipRole.TENANT_ADMIN: frozenset(Permission),
    MembershipRole.EDITOR: _READ_ALL
    | {
        Permission.DOCUMENT_WRITE,
        Permission.SESSION_WRITE,
        Permission.CHAT_CREATE,
        Permission.OPERATION_CREATE,
        Permission.OPERATION_APPLY,
    },
    MembershipRole.MEMBER: _READ_ALL
    | {
        Permission.CHAT_CREATE,
        Permission.OPERATION_CREATE,
    },
    # Viewer chat creation ("read/create if enabled") is disabled by default:
    # no route grants it, and the tests pin that choice.
    MembershipRole.VIEWER: _READ_ALL,
    # Service accounts authenticate internal routes only; never user resources.
    MembershipRole.SERVICE_ACCOUNT: frozenset(),
}

# Roles admitted when the deployment runs the default-tenant compatibility
# path (OIDC unconfigured): the single-user MVP actor is de-facto admin.
COMPATIBILITY_ROLES: frozenset[MembershipRole] = frozenset(
    {
        MembershipRole.TENANT_ADMIN,
        MembershipRole.EDITOR,
        MembershipRole.MEMBER,
        MembershipRole.VIEWER,
    }
)


def ensure_allowed(role: MembershipRole, permission: Permission) -> None:
    """Raise 403 unless `role` holds `permission` — the one RBAC decision."""
    if permission not in _ROLE_MATRIX.get(role, frozenset()):
        raise ForbiddenError("You do not have permission to perform this action")
