"""Chat session/message ORM models — persistent multi-turn conversations.

Sessions are the deletion unit: messages carry no soft-delete of their own
and follow session visibility (queries join live sessions).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.utils.ids import uuid7


class MessageRole(StrEnum):
    """Author of one stored chat message."""

    USER = "user"
    ASSISTANT = "assistant"


class ChatSession(Base):
    """One conversation: a titled sequence of chat messages."""

    __tablename__ = "chat_sessions"
    __table_args__ = (
        # Keyset pagination cursor support: ORDER BY updated_at DESC, id DESC.
        Index("ix_chat_sessions_updated_at_id", text("updated_at DESC, id DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="New chat")
    # Reserved for multi-user: schema is ready, auth is not (project convention).
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Rolling summary of turns evicted from the history token window
    # (migration 0007): derived, additional state — ChatMessage rows are never
    # rewritten. NULL/empty = nothing folded yet; maintained best-effort
    # post-answer by the chat service.
    rolling_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Incremental watermark: id of the newest message already folded into
    # `rolling_summary` (uuid7, time-ordered, so id order == message order).
    summarized_through_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatMessage(Base):
    """One persisted turn message; visibility follows its session."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        # History reads: everything of one session, chronological.
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(
        # values_callable: persist the lowercase *values* ("user"), not the
        # member names ("USER") — must match the enum created by migration 0004.
        SAEnum(
            MessageRole,
            name="chat_message_role",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Agent run that produced an assistant message (None for user messages).
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Sources the run's answer cited (migration 0008): the full SearchHit list
    # in retrieval order, as JSON — derived, additional state so a follow-up
    # turn can re-emit and re-cite the previous run's numbered blocks.
    # NULL = nothing carried (user messages, failed runs, pre-feature rows).
    sources: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
