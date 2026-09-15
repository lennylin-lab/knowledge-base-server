"""ORM models. Importing this package registers every table on Base.metadata
(what Alembic autogen compares against)."""

from __future__ import annotations

from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.document import Document, IndexStatus
from app.models.document_chunk import EMBEDDING_DIM, DocumentChunk
from app.models.operation import AgentOperation, DocumentRevision, OperationState
from app.models.tenant import (
    MembershipRole,
    MembershipStatus,
    Tenant,
    TenantMembership,
    TenantStatus,
    User,
    UserStatus,
)

__all__ = [
    "EMBEDDING_DIM",
    "AgentOperation",
    "ChatMessage",
    "ChatSession",
    "Document",
    "DocumentChunk",
    "DocumentRevision",
    "IndexStatus",
    "MembershipRole",
    "MembershipStatus",
    "MessageRole",
    "OperationState",
    "Tenant",
    "TenantMembership",
    "TenantStatus",
    "User",
    "UserStatus",
]
