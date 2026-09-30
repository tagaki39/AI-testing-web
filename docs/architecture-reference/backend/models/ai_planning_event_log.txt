"""SSE event log for AI planning sessions — enables event replay on refresh."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AIPlanningEventLog(Base):
    """Persisted SSE event for a planning session.

    Each event yielded during an AI planning stream (chat / drafts / execute)
    is logged here so that a client that refreshes mid-stream can replay
    missed events via the ``GET /sessions/{id}/events`` endpoint.
    """

    __tablename__ = "ai_planning_event_logs"
    __table_args__ = (
        Index("ix_event_log_session_seq", "session_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("ai_planning_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("ai_planning_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(), server_default=func.now(), nullable=False, index=True,
    )
