"""Persisted DSL drafts generated from planning scenarios."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AIPlanningDraft(Base):
    """Stored case draft generated from a planning scenario."""

    __tablename__ = "ai_planning_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("ai_planning_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    scenario_key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="generated")
    dsl_generation_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("dsl_generation_runs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    dsl_case_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    normalization_notes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), server_default=func.now(), onupdate=func.now(), nullable=False)
