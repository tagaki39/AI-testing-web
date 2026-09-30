"""Persisted AI planning sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.project import Project


class AIPlanningSession(Base):
    """Conversation state for AI-assisted test planning."""

    __tablename__ = "ai_planning_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False)
    case_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("test_cases.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="collecting")
    requirements_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    missing_slots_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.now(), onupdate=func.now(), nullable=False)

    projects: Mapped[list["Project"]] = relationship(
        "Project", secondary="session_projects", back_populates="sessions", lazy="selectin",
    )
